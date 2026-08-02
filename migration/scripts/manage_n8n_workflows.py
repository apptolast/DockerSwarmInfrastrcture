#!/usr/bin/env python3
"""Publish or roll back the exact audited n8n workflow inventory after cutover."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

HOST_LOCK_DIRECTORY = Path(__file__).resolve().parents[2] / "scripts"
if str(HOST_LOCK_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(HOST_LOCK_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

EXPECTED_STACK_SERVICES = {
    "kropia",
    "minecraft",
    "minecraft-stats",
    "n8n",
    "n8n-db",
    "n8n-runners",
    "openclaw",
    "passbolt",
    "passbolt-db",
    "portfolio-alberto",
    "portfolio-pablo",
    "redis-coordinator",
    "selenium",
    "shlink",
    "shlink-db",
}
PUBLIC_HEALTH_URL = "https://n8n.apptolast.com/healthz"
PUBLIC_HEALTH_HOST = "n8n.apptolast.com"
PUBLISH_CONFIRMATION = "I_HAVE_CONFIRMED_GOOGLE_OAUTH_CONSENT"
INVENTORY_FILENAME = "n8n-active-workflows.json"
QUARANTINE_FILENAME = "n8n-workflows-quarantine.json"
ROLLBACK_RECONCILE_PASSES = 3
CONTROL_CHARACTERS = "\x00\r\n\t"
OFFICIAL_NODE_TYPE_PREFIXES = (
    "n8n-nodes-base.",
    "@n8n/n8n-nodes-langchain.",
)
CURRENT_INTERNAL_HOSTS = {
    "localhost",
    "n8n",
    "n8n-db",
    "n8n-runners",
    "redis-coordinator",
    "selenium",
    "workloads_n8n",
    "workloads_n8n-db",
    "workloads_n8n-runners",
    "workloads_redis-coordinator",
    "workloads_selenium",
}
EXTERNAL_RUNNER_NODE_TYPES = {
    "n8n-nodes-base.code",
}
EXTERNAL_RUNNER_REACHABLE_INTERNAL_HOSTS = {
    "localhost",
    "n8n",
    "n8n-runners",
    "workloads_n8n",
    "workloads_n8n-runners",
}
ENVIRONMENT_ACCESS_PATTERNS = (
    re.compile(r"(?<![\w$])\$env(?:\s*(?:\.|\[))"),
    re.compile(r"(?<![\w$])process\s*(?:\.\s*env|\[\s*['\"]env['\"]\s*\])"),
    re.compile(r"(?<![\w$])Deno\s*\.\s*env"),
    re.compile(r"(?<![\w$])os\s*\.\s*(?:environ|getenv)"),
)
URL_PATTERN = re.compile(
    r"(?i)\b(?:https?|wss?|redis|rediss|postgres|postgresql)://[^\s'\"<>]+"
)
HOST_FIELD_NAMES = {
    "address",
    "databasehost",
    "hostname",
    "host",
    "redishost",
    "server",
}
CREDENTIAL_ENDPOINT_AUDIT_JS = r"""
const dns = require("node:dns").promises;
const fs = require("node:fs");
const net = require("node:net");

const allowedInternal = new Set([
  "localhost",
  "n8n",
  "n8n-db",
  "n8n-runners",
  "redis-coordinator",
  "selenium",
  "workloads_n8n",
  "workloads_n8n-db",
  "workloads_n8n-runners",
  "workloads_redis-coordinator",
  "workloads_selenium",
]);
const hostFields = new Set([
  "address",
  "databasehost",
  "host",
  "hostname",
  "redishost",
  "server",
]);
const urlSchemes = /^(?:https?|wss?|redis|rediss|postgres|postgresql):\/\//i;

function normalizedField(value) {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function publicHostname(value) {
  const host = value.trim().replace(/\.$/, "").toLowerCase();
  if (!host || host.includes("{{") || host.includes("}}") || host.includes("$")) {
    return false;
  }
  if (allowedInternal.has(host)) return true;
  const ipv4 = host.match(/^(\d{1,3})(?:\.(\d{1,3})){3}$/);
  if (ipv4) {
    const octets = host.split(".").map(Number);
    if (octets.some((part) => part < 0 || part > 255)) return false;
    const [a, b] = octets;
    return !(
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      a >= 224
    );
  }
  if (host.includes(":")) {
    return !(
      host === "::" ||
      host === "::1" ||
      host.startsWith("fc") ||
      host.startsWith("fd") ||
      host.startsWith("fe8") ||
      host.startsWith("fe9") ||
      host.startsWith("fea") ||
      host.startsWith("feb")
    );
  }
  return host.includes(".");
}

function numericPort(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 65535
    ? parsed
    : null;
}

function walk(value, endpoints, counters) {
  if (Array.isArray(value)) {
    for (const child of value) walk(child, endpoints, counters);
    return;
  }
  if (value === null || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    const field = normalizedField(key);
    if (typeof child === "string") {
      if (hostFields.has(field) || field.endsWith("hostname")) {
        endpoints.push({
          host: child,
          port: numericPort(value.port),
        });
      }
      const isUrlField =
        field.endsWith("url") ||
        field.endsWith("uri") ||
        field.endsWith("endpoint");
      if (isUrlField && (child.includes("{{") || child.includes("$"))) {
        counters.validationViolations += 1;
      } else if (urlSchemes.test(child)) {
        try {
          const parsed = new URL(child);
          endpoints.push({
            host: parsed.hostname,
            port:
              numericPort(parsed.port) ||
              (["https:", "wss:", "rediss:"].includes(parsed.protocol)
                ? 443
                : ["postgres:", "postgresql:"].includes(parsed.protocol)
                  ? 5432
                  : parsed.protocol === "redis:"
                    ? 6379
                    : 80),
          });
        } catch {
          counters.validationViolations += 1;
        }
      }
    }
    walk(child, endpoints, counters);
  }
}

async function timedLookup(host) {
  let timeout;
  try {
    await Promise.race([
      dns.lookup(host),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("DNS timeout")), 5000);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

async function timedConnect(host, port) {
  await new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    socket.setTimeout(5000);
    socket.once("connect", () => {
      socket.destroy();
      resolve();
    });
    socket.once("timeout", () => {
      socket.destroy();
      reject(new Error("TCP timeout"));
    });
    socket.once("error", reject);
  });
}

async function main() {
  const credentials = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  if (!Array.isArray(credentials)) throw new Error("invalid credential export");
  const endpoints = [];
  const counters = { validationViolations: 0, probeViolations: 0 };
  for (const credential of credentials) {
    if (
      credential === null ||
      typeof credential !== "object" ||
      credential.data === null ||
      typeof credential.data !== "object"
    ) {
      counters.validationViolations += 1;
      continue;
    }
    walk(credential.data, endpoints, counters);
  }
  const unique = new Map();
  for (const endpoint of endpoints) {
    const host = String(endpoint.host || "").trim().toLowerCase();
    if (!publicHostname(host)) {
      counters.validationViolations += 1;
      continue;
    }
    const port = numericPort(endpoint.port);
    unique.set(`${host}:${port || ""}`, { host, port });
  }
  if (counters.validationViolations === 0) {
    await Promise.all(
      [...unique.values()].map(async (endpoint) => {
        try {
          await timedLookup(endpoint.host);
          if (endpoint.port !== null) {
            await timedConnect(endpoint.host, endpoint.port);
          }
        } catch {
          counters.probeViolations += 1;
        }
      }),
    );
  }
  const summary = {
    schemaVersion: 1,
    credentialCount: credentials.length,
    endpointCount: unique.size,
    validationViolationCount: counters.validationViolations,
    probeViolationCount: counters.probeViolations,
  };
  const output = JSON.stringify(summary);
  if (
    counters.validationViolations !== 0 ||
    counters.probeViolations !== 0
  ) {
    process.stderr.write(`${output}\n`);
    process.exit(2);
  }
  process.stdout.write(`${output}\n`);
}

main().catch(() => {
  process.stderr.write(
    JSON.stringify({
      schemaVersion: 1,
      credentialCount: 0,
      endpointCount: 0,
      validationViolationCount: 1,
      probeViolationCount: 0,
    }) + "\n",
  );
  process.exit(2);
});
""".strip()


class WorkflowActivationError(RuntimeError):
    """The audited n8n publication state cannot be established safely."""


class DeferredTransactionSignals:
    def __init__(self) -> None:
        self.signal_number: int | None = None

    def record(self, signal_number: int, _frame: object) -> None:
        if self.signal_number is None:
            self.signal_number = signal_number


@contextlib.contextmanager
def transactional_signal_handlers() -> Iterator[None]:
    previous_handlers: dict[int, signal.Handlers] = {}

    def interrupt_transaction(signal_number: int, _frame: object) -> None:
        raise WorkflowActivationError(
            f"received signal {signal_number}; transactional rollback is required"
        )

    try:
        previous_handlers = {
            signal_number: signal.signal(signal_number, interrupt_transaction)
            for signal_number in (
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
            )
        }
        yield
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


@contextlib.contextmanager
def deferred_transaction_signal_handlers() -> Iterator[DeferredTransactionSignals]:
    state = DeferredTransactionSignals()
    previous_handlers: dict[int, signal.Handlers] = {}
    try:
        previous_handlers = {
            signal_number: signal.signal(signal_number, state.record)
            for signal_number in (
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
            )
        }
        yield state
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        redact_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(argv),
                check=check,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise WorkflowActivationError(
                f"cannot execute required command: {argv[0]}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (
                "sensitive command output redacted"
                if redact_output
                else (exc.stderr or exc.stdout or "").strip()
            )
            raise WorkflowActivationError(
                f"command failed ({' '.join(argv[:3])}): {detail}"
            ) from exc


def validate_inventory(document: Any) -> list[dict[str, str]]:
    if not isinstance(document, list):
        raise WorkflowActivationError("workflow inventory must be a JSON list")
    identifiers: set[str] = set()
    validated: list[dict[str, str]] = []
    for item in document:
        if not isinstance(item, dict) or set(item) != {"id", "activeVersionId"}:
            raise WorkflowActivationError(
                "workflow inventory entries must contain only id and activeVersionId"
            )
        workflow_id = item["id"]
        version_id = item["activeVersionId"]
        if (
            not isinstance(workflow_id, str)
            or not workflow_id
            or len(workflow_id) > 255
            or not isinstance(version_id, str)
            or not version_id
            or len(version_id) > 255
            or any(
                character in workflow_id + version_id
                for character in CONTROL_CHARACTERS
            )
        ):
            raise WorkflowActivationError("workflow inventory identifier is invalid")
        if workflow_id in identifiers:
            raise WorkflowActivationError("workflow inventory contains duplicate IDs")
        identifiers.add(workflow_id)
        validated.append({"id": workflow_id, "activeVersionId": version_id})
    if validated != sorted(validated, key=lambda item: item["id"]):
        raise WorkflowActivationError("workflow inventory must be sorted by id")
    return validated


def load_inventory(path: Path) -> list[dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise WorkflowActivationError(f"workflow inventory is absent or unsafe: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowActivationError("cannot read workflow inventory") from exc
    return validate_inventory(document)


def load_expected_ipv4(platform_contract: Path) -> str:
    if platform_contract.is_symlink() or not platform_contract.is_file():
        raise WorkflowActivationError("platform contract is absent or unsafe")
    try:
        document = yaml.safe_load(platform_contract.read_text(encoding="utf-8"))
        value = document["platform_public_ipv4"]
        address = IPv4Address(value)
    except (
        OSError,
        UnicodeDecodeError,
        yaml.YAMLError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise WorkflowActivationError(
            "platform contract has no valid public IPv4"
        ) from exc
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise WorkflowActivationError("platform public IPv4 is unsafe")
    return str(address)


def publication_plan(
    current: list[dict[str, str]],
    expected: list[dict[str, str]],
) -> list[dict[str, str]]:
    current_by_id = {item["id"]: item["activeVersionId"] for item in current}
    expected_by_id = {item["id"]: item["activeVersionId"] for item in expected}
    if not set(current_by_id).issubset(expected_by_id):
        raise WorkflowActivationError(
            "an active workflow exists outside the audited inventory"
        )
    mismatched = {
        workflow_id
        for workflow_id, version_id in current_by_id.items()
        if expected_by_id[workflow_id] != version_id
    }
    if mismatched:
        raise WorkflowActivationError(
            "an audited workflow has an unexpected published version"
        )
    return [item for item in expected if item["id"] not in current_by_id]


def rollback_plan(
    current: list[dict[str, str]],
    expected: list[dict[str, str]],
) -> list[str]:
    expected_ids = {item["id"] for item in expected}
    current_ids = {item["id"] for item in current}
    if not current_ids.issubset(expected_ids):
        raise WorkflowActivationError(
            "rollback refuses active workflows outside the audited inventory"
        )
    return sorted(current_ids)


def parse_stack_replicas(output: str, stack_name: str) -> dict[str, str]:
    observed: dict[str, str] = {}
    prefix = f"{stack_name}_"
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].startswith(prefix):
            raise WorkflowActivationError("Docker returned invalid stack replica data")
        observed[parts[0][len(prefix) :]] = parts[1]
    if set(observed) != EXPECTED_STACK_SERVICES:
        raise WorkflowActivationError("workloads stack service set is incomplete")
    if any(replicas != "1/1" for replicas in observed.values()):
        raise WorkflowActivationError("workloads stack has unconverged replicas")
    return observed


def normalized_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WorkflowActivationError(
                "restored workflow nodes are not valid JSON"
            ) from exc
    if not isinstance(value, list) or any(not isinstance(node, dict) for node in value):
        raise WorkflowActivationError(
            "restored workflow nodes are not a JSON object list"
        )
    return value


def keyed_strings(
    value: Any,
    path: tuple[str, ...] = (),
):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise WorkflowActivationError(
                    "restored workflow contains a non-string object key"
                )
            yield from keyed_strings(child, path + (key,))
    elif isinstance(value, list):
        for child in value:
            yield from keyed_strings(child, path)
    elif isinstance(value, str):
        yield path, value


def validate_internal_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if not normalized or any(marker in normalized for marker in ("{{", "}}", "$")):
        return False
    if normalized in CURRENT_INTERNAL_HOSTS:
        return True
    try:
        address = IPv4Address(normalized)
    except ValueError:
        return "." in normalized
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    )


def embedded_hosts(path: tuple[str, ...], value: str):
    for match in URL_PATTERN.finditer(value):
        try:
            hostname = urlsplit(match.group(0)).hostname
        except ValueError:
            hostname = None
        if hostname is None:
            yield ""
        else:
            yield hostname
    if path:
        field = re.sub(r"[^a-z0-9]", "", path[-1].lower())
        if field in HOST_FIELD_NAMES or field.endswith("hostname"):
            candidate = value.strip()
            if "://" in candidate:
                try:
                    candidate = urlsplit(candidate).hostname or ""
                except ValueError:
                    candidate = ""
            elif ":" in candidate and candidate.count(":") == 1:
                candidate = candidate.split(":", 1)[0]
            yield candidate


def validate_workflow_security(document: Any) -> dict[str, int]:
    if not isinstance(document, list):
        raise WorkflowActivationError(
            "restored workflow security inventory must be a JSON list"
        )
    workflow_ids: set[str] = set()
    node_count = 0
    endpoint_count = 0
    environment_violations = 0
    package_violations = 0
    endpoint_violations = 0
    runner_network_violations = 0
    for workflow in document:
        if not isinstance(workflow, dict) or set(workflow) != {"id", "nodes"}:
            raise WorkflowActivationError(
                "restored workflow security inventory has invalid fields"
            )
        workflow_id = workflow["id"]
        if (
            not isinstance(workflow_id, str)
            or not workflow_id
            or workflow_id in workflow_ids
        ):
            raise WorkflowActivationError(
                "restored workflow security inventory has invalid IDs"
            )
        workflow_ids.add(workflow_id)
        nodes = normalized_nodes(workflow["nodes"])
        node_count += len(nodes)
        for node in nodes:
            node_type = node.get("type")
            if not isinstance(node_type, str) or not node_type.startswith(
                OFFICIAL_NODE_TYPE_PREFIXES
            ):
                package_violations += 1
            for path, value in keyed_strings(node):
                if any(
                    pattern.search(value) for pattern in ENVIRONMENT_ACCESS_PATTERNS
                ):
                    environment_violations += 1
                for hostname in embedded_hosts(path, value):
                    endpoint_count += 1
                    if not validate_internal_hostname(hostname):
                        endpoint_violations += 1
                    if (
                        node_type in EXTERNAL_RUNNER_NODE_TYPES
                        and hostname.rstrip(".").lower() in CURRENT_INTERNAL_HOSTS
                        and hostname.rstrip(".").lower()
                        not in EXTERNAL_RUNNER_REACHABLE_INTERNAL_HOSTS
                    ):
                        runner_network_violations += 1
    if environment_violations:
        raise WorkflowActivationError(
            "restored workflows depend on blocked environment-variable access"
        )
    if package_violations:
        raise WorkflowActivationError(
            "restored workflows depend on non-official package node types"
        )
    if endpoint_violations:
        raise WorkflowActivationError(
            "restored workflows contain legacy or unverifiable internal endpoints"
        )
    if runner_network_violations:
        raise WorkflowActivationError(
            "restored Code nodes target an internal endpoint outside the "
            "isolated external task runner network; use a reviewed "
            "main-process node or update the network contract explicitly"
        )
    return {
        "workflowCount": len(document),
        "nodeCount": node_count,
        "endpointCount": endpoint_count,
    }


def audit_restored_workflows(
    runner: CommandRunner,
    database_container: str,
) -> dict[str, int]:
    query = (
        "SELECT COALESCE(json_agg(json_build_object("
        "'id', id, 'nodes', nodes) ORDER BY id), "
        "'[]'::json)::text FROM workflow_entity;"
    )
    result = runner.run(
        [
            "/usr/bin/docker",
            "container",
            "exec",
            "--user=postgres",
            database_container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-A",
            "-t",
            "-U",
            "n8n",
            "-d",
            "n8n",
            "-c",
            query,
        ]
    )
    try:
        document = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise WorkflowActivationError(
            "n8n database returned invalid workflow security JSON"
        ) from exc
    return validate_workflow_security(document)


def validate_credential_audit_summary(document: Any) -> dict[str, int]:
    expected_keys = {
        "schemaVersion",
        "credentialCount",
        "endpointCount",
        "validationViolationCount",
        "probeViolationCount",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise WorkflowActivationError(
            "n8n credential endpoint audit returned an invalid summary"
        )
    if document.get("schemaVersion") != 1 or any(
        not isinstance(document.get(field), int) or document[field] < 0
        for field in expected_keys - {"schemaVersion"}
    ):
        raise WorkflowActivationError(
            "n8n credential endpoint audit summary is invalid"
        )
    if (
        document["validationViolationCount"] != 0
        or document["probeViolationCount"] != 0
    ):
        raise WorkflowActivationError(
            "n8n restored credentials contain legacy, unsafe or unreachable "
            "runtime endpoints"
        )
    return document


def audit_restored_credentials(
    runner: CommandRunner,
    n8n_container: str,
) -> dict[str, int]:
    shell = (
        "umask 077;"
        'audit_dir="$(mktemp -d '
        '/tmp/apptolast-n8n-credential-audit.XXXXXX)";'
        "cleanup() { "
        'rm -f -- "$audit_dir/credentials.json"; '
        'rmdir -- "$audit_dir" 2>/dev/null || true; '
        "};"
        "trap cleanup EXIT;"
        'export DB_POSTGRESDB_PASSWORD="$(cat '
        '/run/secrets/n8n_db_password)";'
        'export N8N_ENCRYPTION_KEY="$(cat '
        '/run/secrets/n8n_encryption_key)";'
        'export N8N_RUNNERS_AUTH_TOKEN="$(cat '
        '/run/secrets/n8n_runners_auth_token)";'
        "n8n export:credentials --all --decrypted "
        '--output="$audit_dir/credentials.json" >/dev/null;'
        'node -e "$1" "$audit_dir/credentials.json"'
    )
    result = runner.run(
        [
            "/usr/bin/docker",
            "container",
            "exec",
            "--user=1000:1000",
            n8n_container,
            "/bin/sh",
            "-ec",
            shell,
            "apptolast-n8n-credential-audit",
            CREDENTIAL_ENDPOINT_AUDIT_JS,
        ],
        redact_output=True,
    )
    try:
        document = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise WorkflowActivationError(
            "n8n credential endpoint audit returned invalid JSON"
        ) from exc
    return validate_credential_audit_summary(document)


def unique_running_container(
    runner: CommandRunner,
    service_name: str,
) -> str:
    result = runner.run(
        [
            "/usr/bin/docker",
            "container",
            "list",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label=com.docker.swarm.service.name={service_name}",
            "--filter",
            "status=running",
        ]
    )
    identifiers = result.stdout.splitlines()
    if len(identifiers) != 1 or re.fullmatch(r"[a-f0-9]{64}", identifiers[0]) is None:
        raise WorkflowActivationError(
            f"running container for {service_name} is not unique"
        )
    return identifiers[0]


def require_healthy_container(
    runner: CommandRunner,
    container_id: str,
) -> None:
    result = runner.run(["/usr/bin/docker", "container", "inspect", container_id])
    try:
        documents = json.loads(result.stdout)
        container = documents[0]
        running = container["State"]["Running"]
        health = container["State"]["Health"]["Status"]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise WorkflowActivationError("Docker health inspect is invalid") from exc
    if running is not True or health != "healthy":
        raise WorkflowActivationError("n8n task container is not healthy")


def local_n8n_smoke(runner: CommandRunner, container_id: str) -> None:
    probe = (
        "fetch('http://127.0.0.1:5678/healthz')"
        ".then(r=>process.exit(r.ok?0:1))"
        ".catch(()=>process.exit(1))"
    )
    runner.run(
        [
            "/usr/bin/docker",
            "container",
            "exec",
            "--user=1000:1000",
            container_id,
            "node",
            "-e",
            probe,
        ]
    )


def public_n8n_smoke(
    runner: CommandRunner,
    expected_ipv4: str,
) -> None:
    result = runner.run(
        [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "15",
            "--noproxy",
            "*",
            "--resolve",
            f"{PUBLIC_HEALTH_HOST}:443:{expected_ipv4}",
            "--output",
            "/dev/null",
            PUBLIC_HEALTH_URL,
        ]
    )
    if result.returncode != 0:
        raise WorkflowActivationError("public n8n target-bound smoke failed")


def stack_smoke(
    runner: CommandRunner,
    stack_name: str,
    expected_ipv4: str,
) -> tuple[str, str]:
    result = runner.run(
        [
            "/usr/bin/docker",
            "stack",
            "services",
            stack_name,
            "--format",
            "{{.Name}}\t{{.Replicas}}",
        ]
    )
    parse_stack_replicas(result.stdout, stack_name)
    n8n_container = unique_running_container(runner, f"{stack_name}_n8n")
    database_container = unique_running_container(runner, f"{stack_name}_n8n-db")
    require_healthy_container(runner, n8n_container)
    require_healthy_container(runner, database_container)
    audit_restored_workflows(runner, database_container)
    audit_restored_credentials(runner, n8n_container)
    local_n8n_smoke(runner, n8n_container)
    public_n8n_smoke(runner, expected_ipv4)
    return n8n_container, database_container


def current_inventory(
    runner: CommandRunner,
    database_container: str,
) -> list[dict[str, str]]:
    query = (
        "SELECT COALESCE(json_agg(json_build_object("
        "'id', id, 'activeVersionId', \"activeVersionId\") ORDER BY id), "
        "'[]'::json)::text FROM workflow_entity "
        'WHERE active IS TRUE OR "activeVersionId" IS NOT NULL;'
    )
    result = runner.run(
        [
            "/usr/bin/docker",
            "container",
            "exec",
            "--user=postgres",
            database_container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-A",
            "-t",
            "-U",
            "n8n",
            "-d",
            "n8n",
            "-c",
            query,
        ]
    )
    try:
        document = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise WorkflowActivationError(
            "n8n database returned invalid workflow inventory JSON"
        ) from exc
    return validate_inventory(document)


def n8n_cli(
    runner: CommandRunner,
    n8n_container: str,
    command: str,
    workflow_id: str,
    version_id: str | None = None,
) -> None:
    shell = (
        'export DB_POSTGRESDB_PASSWORD="$(cat '
        '/run/secrets/n8n_db_password)";'
        'export N8N_ENCRYPTION_KEY="$(cat '
        '/run/secrets/n8n_encryption_key)";'
        'export N8N_RUNNERS_AUTH_TOKEN="$(cat '
        '/run/secrets/n8n_runners_auth_token)";'
        'exec n8n "$@"'
    )
    cli_arguments = [command, f"--id={workflow_id}"]
    if version_id is not None:
        cli_arguments.append(f"--versionId={version_id}")
    runner.run(
        [
            "/usr/bin/docker",
            "container",
            "exec",
            "--user=1000:1000",
            n8n_container,
            "/bin/sh",
            "-ec",
            shell,
            "apptolast-n8n-cli",
            *cli_arguments,
        ]
    )


def force_restart_and_wait(
    runner: CommandRunner,
    stack_name: str,
    expected_ipv4: str,
    timeout: int,
) -> tuple[str, str]:
    runner.run(
        [
            "/usr/bin/docker",
            "service",
            "update",
            "--force",
            "--detach=false",
            f"{stack_name}_n8n",
        ]
    )
    deadline = time.monotonic() + timeout
    last_error: WorkflowActivationError | None = None
    while time.monotonic() < deadline:
        try:
            n8n_container = unique_running_container(
                runner,
                f"{stack_name}_n8n",
            )
            database_container = unique_running_container(
                runner,
                f"{stack_name}_n8n-db",
            )
            require_healthy_container(runner, n8n_container)
            require_healthy_container(runner, database_container)
            local_n8n_smoke(runner, n8n_container)
            public_n8n_smoke(runner, expected_ipv4)
            return n8n_container, database_container
        except WorkflowActivationError as exc:
            last_error = exc
            time.sleep(2)
    raise WorkflowActivationError(
        f"n8n did not become healthy after restart: {last_error}"
    )


def write_evidence(
    state_directory: Path,
    action: str,
    inventory_path: Path,
    inventory: list[dict[str, str]],
) -> Path:
    if state_directory.is_symlink() or not state_directory.is_dir():
        raise WorkflowActivationError("restore-state directory is unsafe")
    now = datetime.now(timezone.utc)
    filename = (
        f"n8n-workflows-{action}-"
        f"{now.strftime('%Y%m%dT%H%M%S')}-{now.microsecond:06d}Z.json"
    )
    destination = state_directory / filename
    document = {
        "schemaVersion": 1,
        "action": action,
        "completedAt": now.isoformat(),
        "inventorySha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
        "workflowCount": len(inventory),
        "activeInventory": inventory,
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_directory,
            prefix=f".{filename}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def write_quarantine_marker(
    state_directory: Path,
    inventory_path: Path,
    stack_name: str,
    observed: list[dict[str, str]] | None,
    cause: BaseException,
) -> Path:
    if state_directory.is_symlink() or not state_directory.is_dir():
        raise WorkflowActivationError("restore-state directory is unsafe")
    destination = state_directory / QUARANTINE_FILENAME
    if destination.exists() or destination.is_symlink():
        raise WorkflowActivationError(
            "n8n workflow quarantine already requires reviewed recovery"
        )
    now = datetime.now(timezone.utc)
    document = {
        "schemaVersion": 1,
        "action": "quarantine",
        "createdAt": now.isoformat(),
        "stack": stack_name,
        "service": f"{stack_name}_n8n",
        "inventorySha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
        "observedActiveInventory": observed,
        "causeType": type(cause).__name__,
        "requiresReviewedRecovery": True,
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_directory,
            prefix=f".{QUARANTINE_FILENAME}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        temporary.unlink()
        temporary = None
        state = destination.lstat()
        if (
            not stat.S_ISREG(state.st_mode)
            or stat.S_IMODE(state.st_mode) != 0o600
            or state.st_uid != os.geteuid()
            or state.st_nlink != 1
        ):
            raise WorkflowActivationError("n8n quarantine marker inode is unsafe")
        directory_descriptor = os.open(
            state_directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def quarantine_n8n_service(
    runner: CommandRunner,
    *,
    stack_name: str,
    inventory_path: Path,
    observed: list[dict[str, str]] | None,
    cause: BaseException,
) -> Path:
    marker = write_quarantine_marker(
        inventory_path.parent,
        inventory_path,
        stack_name,
        observed,
        cause,
    )
    service_name = f"{stack_name}_n8n"
    runner.run(
        [
            "/usr/bin/docker",
            "service",
            "scale",
            "--detach=false",
            f"{service_name}=0",
        ]
    )
    desired = runner.run(
        [
            "/usr/bin/docker",
            "service",
            "inspect",
            "--format",
            "{{.Spec.Mode.Replicated.Replicas}}",
            service_name,
        ]
    )
    if desired.stdout.strip() != "0":
        raise WorkflowActivationError(
            f"n8n quarantine marker exists but {service_name} is not scaled to zero"
        )
    running = runner.run(
        [
            "/usr/bin/docker",
            "service",
            "ps",
            "--filter",
            "desired-state=running",
            "--format",
            "{{.ID}}",
            service_name,
        ]
    )
    if running.stdout.strip():
        raise WorkflowActivationError(
            f"n8n quarantine marker exists but {service_name} still has running tasks"
        )
    return marker


def publish(
    runner: CommandRunner,
    *,
    stack_name: str,
    expected: list[dict[str, str]],
    inventory_path: Path,
    expected_ipv4: str,
    timeout: int,
) -> Path | None:
    n8n_container, database_container = stack_smoke(
        runner,
        stack_name,
        expected_ipv4,
    )
    current = current_inventory(runner, database_container)
    missing = publication_plan(current, expected)
    if not missing:
        print("n8n workflow inventory is already published exactly.")
        return None
    try:
        for item in missing:
            n8n_cli(
                runner,
                n8n_container,
                "publish:workflow",
                item["id"],
                item["activeVersionId"],
            )
        observed = current_inventory(runner, database_container)
        if observed != expected:
            raise WorkflowActivationError(
                "published workflow inventory differs before restart"
            )
        _, database_container = force_restart_and_wait(
            runner,
            stack_name,
            expected_ipv4,
            timeout,
        )
        observed = current_inventory(runner, database_container)
        if observed != expected:
            raise WorkflowActivationError(
                "published workflow inventory differs after restart"
            )
    except BaseException as publication_error:
        with deferred_transaction_signal_handlers() as deferred_signals:
            try:
                rollback_observed = rollback_transition(
                    runner,
                    stack_name=stack_name,
                    expected=expected,
                    expected_ipv4=expected_ipv4,
                    timeout=timeout,
                )
                rollback_evidence = write_evidence(
                    inventory_path.parent,
                    "automatic-unpublished",
                    inventory_path,
                    rollback_observed,
                )
            except BaseException as rollback_error:
                try:
                    database_container = unique_running_container(
                        runner,
                        f"{stack_name}_n8n-db",
                    )
                    quarantine_observed: list[dict[str, str]] | None = (
                        current_inventory(runner, database_container)
                    )
                except BaseException:
                    quarantine_observed = None
                try:
                    quarantine_evidence = quarantine_n8n_service(
                        runner,
                        stack_name=stack_name,
                        inventory_path=inventory_path,
                        observed=quarantine_observed,
                        cause=rollback_error,
                    )
                except BaseException as quarantine_error:
                    quarantine_marker = inventory_path.parent / QUARANTINE_FILENAME
                    raise WorkflowActivationError(
                        "publication and automatic rollback failed; a durable "
                        "quarantine marker may remain, but scale-to-zero was not "
                        "proven; "
                        f"marker={quarantine_marker}; "
                        f"rollbackType={type(rollback_error).__name__}; "
                        f"quarantineType={type(quarantine_error).__name__}"
                    ) from publication_error
                raise WorkflowActivationError(
                    "publication and automatic rollback failed; n8n was scaled "
                    "to zero and durable reviewed recovery is required; "
                    f"evidence={quarantine_evidence}"
                ) from publication_error
        signal_detail = (
            f"; deferredSignal={deferred_signals.signal_number}"
            if deferred_signals.signal_number is not None
            else ""
        )
        raise WorkflowActivationError(
            "publication failed; automatic exact rollback was verified; "
            f"evidence={rollback_evidence}{signal_detail}"
        ) from publication_error
    return write_evidence(
        inventory_path.parent,
        "published",
        inventory_path,
        observed,
    )


def rollback_transition(
    runner: CommandRunner,
    *,
    stack_name: str,
    expected: list[dict[str, str]],
    expected_ipv4: str,
    timeout: int,
) -> list[dict[str, str]]:
    n8n_container = unique_running_container(runner, f"{stack_name}_n8n")
    database_container = unique_running_container(runner, f"{stack_name}_n8n-db")
    observed = current_inventory(runner, database_container)
    command_failures = 0
    for _reconcile_pass in range(ROLLBACK_RECONCILE_PASSES):
        active_ids = rollback_plan(observed, expected)
        if not active_ids:
            break
        for workflow_id in active_ids:
            try:
                n8n_cli(
                    runner,
                    n8n_container,
                    "unpublish:workflow",
                    workflow_id,
                )
            except WorkflowActivationError:
                command_failures += 1
        observed = current_inventory(runner, database_container)
    if observed:
        raise WorkflowActivationError(
            "workflow inventory is not empty after bounded idempotent "
            f"reconciliation; passes={ROLLBACK_RECONCILE_PASSES}; "
            f"commandFailures={command_failures}"
        )
    _, database_container = force_restart_and_wait(
        runner,
        stack_name,
        expected_ipv4,
        timeout,
    )
    observed = current_inventory(runner, database_container)
    if observed:
        raise WorkflowActivationError(
            "workflow inventory is not empty after rollback restart"
        )
    return observed


def rollback(
    runner: CommandRunner,
    *,
    stack_name: str,
    expected: list[dict[str, str]],
    inventory_path: Path,
    expected_ipv4: str,
    timeout: int,
) -> Path | None:
    result = runner.run(
        [
            "/usr/bin/docker",
            "stack",
            "services",
            stack_name,
            "--format",
            "{{.Name}}\t{{.Replicas}}",
        ]
    )
    parse_stack_replicas(result.stdout, stack_name)
    database_container = unique_running_container(runner, f"{stack_name}_n8n-db")
    current = current_inventory(runner, database_container)
    if not rollback_plan(current, expected):
        print("n8n workflow inventory is already fully unpublished.")
        return None
    with deferred_transaction_signal_handlers() as deferred_signals:
        try:
            observed = rollback_transition(
                runner,
                stack_name=stack_name,
                expected=expected,
                expected_ipv4=expected_ipv4,
                timeout=timeout,
            )
            evidence = write_evidence(
                inventory_path.parent,
                "unpublished",
                inventory_path,
                observed,
            )
        except BaseException as rollback_error:
            try:
                database_container = unique_running_container(
                    runner,
                    f"{stack_name}_n8n-db",
                )
                quarantine_observed: list[dict[str, str]] | None = current_inventory(
                    runner,
                    database_container,
                )
            except BaseException:
                quarantine_observed = None
            try:
                quarantine_evidence = quarantine_n8n_service(
                    runner,
                    stack_name=stack_name,
                    inventory_path=inventory_path,
                    observed=quarantine_observed,
                    cause=rollback_error,
                )
            except BaseException as quarantine_error:
                quarantine_marker = inventory_path.parent / QUARANTINE_FILENAME
                raise WorkflowActivationError(
                    "exact rollback failed; a durable quarantine marker may "
                    "remain, but scale-to-zero was not proven; "
                    f"marker={quarantine_marker}; "
                    f"rollbackType={type(rollback_error).__name__}; "
                    f"quarantineType={type(quarantine_error).__name__}"
                ) from rollback_error
            raise WorkflowActivationError(
                "exact rollback failed; n8n was scaled to zero and durable "
                "reviewed recovery is required; "
                f"evidence={quarantine_evidence}"
            ) from rollback_error
    if deferred_signals.signal_number is not None:
        raise WorkflowActivationError(
            "exact rollback completed after a deferred signal; the empty "
            "inventory and restart were verified, but the operation marker "
            f"must remain; evidence={evidence}; "
            f"signal={deferred_signals.signal_number}"
        )
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("status", "publish", "rollback"),
    )
    parser.add_argument(
        "--services-root",
        type=Path,
        default=Path("/srv/dockerswarm/services"),
    )
    parser.add_argument("--stack", default="workloads")
    parser.add_argument(
        "--platform-contract",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "config/platform.yml",
    )
    parser.add_argument(
        "--confirm-google-oauth-consent",
        help=("required for publish; exact value: " f"{PUBLISH_CONFIRMATION}"),
    )
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        not args.services_root.is_absolute()
        or args.services_root == Path("/")
        or args.services_root.is_symlink()
        or not args.services_root.is_dir()
    ):
        print("ERROR: services root is absent or unsafe", file=sys.stderr)
        return 1
    if re.fullmatch(r"[a-z][a-z0-9-]*", args.stack) is None:
        print("ERROR: stack name is invalid", file=sys.stderr)
        return 1
    if args.timeout < 30 or args.timeout > 1800:
        print("ERROR: timeout must be between 30 and 1800 seconds", file=sys.stderr)
        return 1
    if args.action == "publish":
        if args.confirm_google_oauth_consent != PUBLISH_CONFIRMATION:
            print(
                "ERROR: explicit Google OAuth consent confirmation is required",
                file=sys.stderr,
            )
            return 1
        if os.geteuid() != 0:
            print("ERROR: publish must run as root", file=sys.stderr)
            return 1
    if args.action == "rollback" and os.geteuid() != 0:
        print("ERROR: rollback must run as root", file=sys.stderr)
        return 1
    if args.action in {"publish", "rollback"}:
        ensure_mutation_lock(
            f"n8n-workflows-{args.action}",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            caller=Path(__file__),
        )

    inventory_path = args.services_root / "restore-state" / INVENTORY_FILENAME
    try:
        quarantine_path = inventory_path.parent / QUARANTINE_FILENAME
        if quarantine_path.exists() or quarantine_path.is_symlink():
            raise WorkflowActivationError(
                "n8n workflow quarantine blocks continuation until reviewed recovery: "
                f"{quarantine_path}"
            )
        expected = load_inventory(inventory_path)
        expected_ipv4 = load_expected_ipv4(args.platform_contract)
        runner = CommandRunner()
        if args.action in {"publish", "rollback"}:
            with transactional_signal_handlers():
                if args.action == "publish":
                    evidence = publish(
                        runner,
                        stack_name=args.stack,
                        expected=expected,
                        inventory_path=inventory_path,
                        expected_ipv4=expected_ipv4,
                        timeout=args.timeout,
                    )
                else:
                    evidence = rollback(
                        runner,
                        stack_name=args.stack,
                        expected=expected,
                        inventory_path=inventory_path,
                        expected_ipv4=expected_ipv4,
                        timeout=args.timeout,
                    )
        else:
            _, database_container = stack_smoke(
                runner,
                args.stack,
                expected_ipv4,
            )
            current = current_inventory(runner, database_container)
            missing = publication_plan(current, expected)
            print(
                json.dumps(
                    {
                        "expectedCount": len(expected),
                        "activeCount": len(current),
                        "exactlyPublished": not missing and current == expected,
                        "activeInventory": current,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    except WorkflowActivationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if evidence is not None:
        print(f"OK: workflow transition verified; evidence: {evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
