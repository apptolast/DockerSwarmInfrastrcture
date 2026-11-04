#!/usr/bin/env python3
"""Start the containers Docker could not attach to an adopted edge network.

An adopted edge network is a Swarm overlay that plain containers deployed
outside this repository join (docs/EDGE.md, «Arranque tras un reinicio»).
When the daemon starts, it may restore such a container before Swarm has
the network ready. The start then fails with "network <name> not found",
and Docker never retries it: "A restart policy only takes effect after a
container starts successfully"
(https://docs.docker.com/engine/containers/start-containers-automatically/).
That left monitor.apptolast.com at 502 from 2026-10-18 to 2026-11-04.

The edge role runs this from a oneshot unit every time docker.service
starts, and once on each apply. For each NETWORK it waits until the network
exists, then starts only the containers that meet all of these:

- they are attached to that network;
- they are not Swarm tasks (Swarm reschedules its own);
- they are stopped (exited or created);
- their restart policy is always or unless-stopped;
- their recorded start error is exactly that network missing.

A container stopped on purpose carries no such error, so it stays stopped.
The script prints the started names as JSON and exits 0, 1 when a network
never appears or a start fails, and 2 on invalid arguments.

Usage: restore-adopted-network-containers.py [--timeout SECONDS] NETWORK...
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

DOCKER = "/usr/bin/docker"
NETWORK = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
RESTARTING_POLICIES = frozenset({"always", "unless-stopped"})
STOPPED_STATES = ("exited", "created")
SWARM_TASK_LABEL = "com.docker.swarm.task.id"
POLL_SECONDS = 2.0
# Each Docker call is local; the bound only stops a wedged daemon from
# holding the unit until systemd's own TimeoutStartSec.
CALL_TIMEOUT_SECONDS = 60

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class UsageError(ValueError):
    """The arguments are invalid."""


class RestoreError(RuntimeError):
    """The daemon prevents a complete restore."""


def run_docker(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [DOCKER, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SECONDS,
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise UsageError(message)


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = _Parser(prog="restore-adopted-network-containers.py")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("networks", nargs="+")
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.timeout <= 3600:
        raise UsageError("the timeout must be between 1 and 3600 seconds")
    for network in arguments.networks:
        if not NETWORK.fullmatch(network):
            raise UsageError(f"invalid network name: {network!r}")
    if len(set(arguments.networks)) != len(arguments.networks):
        raise UsageError("each network may be named only once")
    return arguments


def wait_for_network(
    run: Runner,
    network: str,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    while True:
        inspected = run(["network", "inspect", "--format", "{{.Name}}", network])
        if inspected.returncode == 0:
            return
        if clock() >= deadline:
            raise RestoreError(f"network {network} did not appear in time")
        sleep(POLL_SECONDS)


def stopped_members(run: Runner, network: str) -> list[str]:
    arguments = ["ps", "--all", "--no-trunc", "--format", "{{.ID}}"]
    arguments += ["--filter", f"network={network}"]
    for state in STOPPED_STATES:
        arguments += ["--filter", f"status={state}"]
    listed = run(arguments)
    if listed.returncode != 0:
        raise RestoreError(f"cannot list the containers of network {network}")
    return sorted(set(listed.stdout.split()))


def needs_start(container: dict[str, Any], network: str) -> bool:
    labels = (container.get("Config") or {}).get("Labels") or {}
    if SWARM_TASK_LABEL in labels:
        return False
    state = container.get("State") or {}
    if state.get("Running") or state.get("Status") not in STOPPED_STATES:
        return False
    host_config = container.get("HostConfig") or {}
    policy = (host_config.get("RestartPolicy") or {}).get("Name")
    if policy not in RESTARTING_POLICIES:
        return False
    return f"network {network} not found" in (state.get("Error") or "")


def restore(
    run: Runner,
    networks: Sequence[str],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    started: list[str] = []
    failures: list[str] = []
    for network in networks:
        wait_for_network(run, network, deadline, clock, sleep)
        for container_id in stopped_members(run, network):
            inspected = run(["container", "inspect", container_id])
            if inspected.returncode != 0:
                # Removed between the listing and the inspection.
                continue
            container = json.loads(inspected.stdout)[0]
            if not needs_start(container, network):
                continue
            name = str(container.get("Name") or container_id).lstrip("/")
            if run(["start", container_id]).returncode == 0:
                started.append(name)
            else:
                failures.append(name)
    if failures:
        raise RestoreError("cannot start: " + ", ".join(sorted(failures)))
    return started


def main(argv: Sequence[str] | None = None, run: Runner = run_docker) -> int:
    try:
        arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    except UsageError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        deadline = time.monotonic() + arguments.timeout
        started = restore(run, arguments.networks, deadline)
    except RestoreError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (subprocess.TimeoutExpired, ValueError, IndexError, KeyError) as error:
        print(f"ERROR: unreadable Docker answer: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"started": started}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
