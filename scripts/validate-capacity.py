#!/usr/bin/env python3
"""Validate aggregate Docker Swarm resources against the reviewed host budget."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import sys
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = PROJECT_DIR / "config/capacity.yml"
DEFAULT_REDIS_CONFIG = PROJECT_DIR / "stacks/workloads/config/redis.conf"
DEFAULT_STACKS = {
    "edge": PROJECT_DIR / ".build/edge/stack.yml",
    "workloads": PROJECT_DIR / ".build/workloads/stack.yml",
    "observability": PROJECT_DIR / ".build/observability/stack.yml",
}
STACK_IDS = frozenset(DEFAULT_STACKS)
IDENTIFIER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
CPU_RE = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{2}")
MEMORY_RE = re.compile(r"([1-9][0-9]*)M")
APPLICATION_MEMORY_RE = re.compile(r"([1-9][0-9]*)([MG])")
TMPFS_MEMORY_RE = re.compile(r"/dev/shm:size=([1-9][0-9]*)([mg]),mode=1777")
REDIS_MEMORY_RE = re.compile(r"([1-9][0-9]*)mb")
RATIO_RE = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{2}")
MIB = 1024 * 1024


class CapacityError(RuntimeError):
    """The rendered resource plan or host capacity is unsafe."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    explicit_keys: set[Any] = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in explicit_keys
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found an unhashable key: {error}",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        explicit_keys.add(key)

    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def load_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise CapacityError(f"cannot load YAML {path}: {error}") from error


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CapacityError(f"cannot load text file {path}: {error}") from error


def expect_mapping(
    value: Any,
    expected_keys: set[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapacityError(f"{context} must be a mapping")
    actual_keys = set(value)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise CapacityError(f"{context} has {'; '.join(details)}")
    return value


def expect_int(
    value: Any,
    context: str,
    *,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CapacityError(f"{context} must be an integer >= {minimum}")
    return value


def expect_ratio(value: Any, context: str) -> Decimal:
    if not isinstance(value, str) or not RATIO_RE.fullmatch(value):
        raise CapacityError(f"{context} must be a quoted ratio with two decimals")
    try:
        ratio = Decimal(value)
    except InvalidOperation as error:
        raise CapacityError(f"{context} is not a decimal ratio") from error
    if ratio <= 0:
        raise CapacityError(f"{context} must be positive")
    return ratio


def expect_identifiers(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        raise CapacityError(f"{context} must be a list")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not IDENTIFIER_RE.fullmatch(item):
            raise CapacityError(
                f"{context}[{index}] is not a normalized service identifier"
            )
        result.append(item)
    if len(result) != len(set(result)):
        raise CapacityError(f"{context} contains duplicates")
    return result


def empty_resources() -> dict[str, dict[str, int]]:
    return {
        "reservations": {"cpu_millicores": 0, "memory_mib": 0},
        "limits": {"cpu_millicores": 0, "memory_mib": 0},
    }


def add_resources(
    target: dict[str, dict[str, int]],
    source: dict[str, dict[str, int]],
) -> None:
    for resource_class in ("reservations", "limits"):
        for resource_name in ("cpu_millicores", "memory_mib"):
            target[resource_class][resource_name] += source[resource_class][
                resource_name
            ]


def validate_resource_totals(
    value: Any,
    context: str,
) -> dict[str, dict[str, int]]:
    mapping = expect_mapping(value, {"reservations", "limits"}, context)
    result = empty_resources()
    for resource_class in ("reservations", "limits"):
        resources = expect_mapping(
            mapping[resource_class],
            {"cpu_millicores", "memory_mib"},
            f"{context}.{resource_class}",
        )
        result[resource_class] = {
            "cpu_millicores": expect_int(
                resources["cpu_millicores"],
                f"{context}.{resource_class}.cpu_millicores",
            ),
            "memory_mib": expect_int(
                resources["memory_mib"],
                f"{context}.{resource_class}.memory_mib",
            ),
        }
    return result


def validate_contract(document: Any) -> dict[str, Any]:
    outer = expect_mapping(document, {"capacity_contract"}, "capacity document")
    contract = expect_mapping(
        outer["capacity_contract"],
        {
            "schema_version",
            "reviewed_at",
            "topology",
            "host",
            "observed_source",
            "system_reserve",
            "operational_headroom",
            "policy",
            "application_guards",
            "stacks",
            "reviewed_totals",
        },
        "capacity_contract",
    )
    if contract["schema_version"] != 1:
        raise CapacityError("capacity_contract.schema_version must equal 1")
    if not isinstance(contract["reviewed_at"], str) or not re.fullmatch(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", contract["reviewed_at"]
    ):
        raise CapacityError("capacity_contract.reviewed_at must be an ISO date")

    topology = expect_mapping(
        contract["topology"],
        {"model", "eligible_nodes"},
        "capacity_contract.topology",
    )
    if topology["model"] != "single-node":
        raise CapacityError("only the reviewed single-node topology is supported")
    eligible_nodes = expect_int(
        topology["eligible_nodes"],
        "capacity_contract.topology.eligible_nodes",
        minimum=1,
    )
    if eligible_nodes != 1:
        raise CapacityError("schema v1 requires exactly one eligible node")

    host = expect_mapping(
        contract["host"],
        {
            "minimum_cpu_millicores",
            "minimum_memory_mib",
            "required_swap_mib",
        },
        "capacity_contract.host",
    )
    minimum_cpu = expect_int(
        host["minimum_cpu_millicores"],
        "capacity_contract.host.minimum_cpu_millicores",
        minimum=1,
    )
    minimum_memory = expect_int(
        host["minimum_memory_mib"],
        "capacity_contract.host.minimum_memory_mib",
        minimum=1,
    )
    required_swap = expect_int(
        host["required_swap_mib"],
        "capacity_contract.host.required_swap_mib",
    )

    observed = expect_mapping(
        contract["observed_source"],
        {"docker_memory_bytes", "docker_nano_cpus", "swap_bytes"},
        "capacity_contract.observed_source",
    )
    observed_memory = expect_int(
        observed["docker_memory_bytes"],
        "capacity_contract.observed_source.docker_memory_bytes",
        minimum=1,
    )
    observed_cpu = expect_int(
        observed["docker_nano_cpus"],
        "capacity_contract.observed_source.docker_nano_cpus",
        minimum=1,
    )
    observed_swap = expect_int(
        observed["swap_bytes"],
        "capacity_contract.observed_source.swap_bytes",
    )
    if observed_memory // MIB != minimum_memory:
        raise CapacityError(
            "minimum_memory_mib differs from the observed Docker memory floor"
        )
    if observed_cpu // 1_000_000 != minimum_cpu:
        raise CapacityError(
            "minimum_cpu_millicores differs from the observed Docker CPU value"
        )
    if observed_swap // MIB != required_swap:
        raise CapacityError("required_swap_mib differs from the observed swap floor")
    if required_swap != 0:
        raise CapacityError("schema v1 requires a swapless host")

    reserve = expect_mapping(
        contract["system_reserve"],
        {"cpu_millicores", "memory_mib"},
        "capacity_contract.system_reserve",
    )
    reserve_cpu = expect_int(
        reserve["cpu_millicores"],
        "capacity_contract.system_reserve.cpu_millicores",
        minimum=1,
    )
    reserve_memory = expect_int(
        reserve["memory_mib"],
        "capacity_contract.system_reserve.memory_mib",
        minimum=1,
    )
    if reserve_cpu < 1000 or reserve_memory < 3072:
        raise CapacityError(
            "schema v1 requires at least 1000m CPU and 3072 MiB for the host"
        )
    if reserve_cpu >= minimum_cpu or reserve_memory >= minimum_memory:
        raise CapacityError("system reserve consumes the complete host")

    operational_headroom = expect_mapping(
        contract["operational_headroom"],
        {"memory_mib"},
        "capacity_contract.operational_headroom",
    )
    headroom_memory = expect_int(
        operational_headroom["memory_mib"],
        "capacity_contract.operational_headroom.memory_mib",
        minimum=1,
    )
    if headroom_memory < 512:
        raise CapacityError("schema v1 requires at least 512 MiB operational headroom")
    if reserve_memory + headroom_memory >= minimum_memory:
        raise CapacityError(
            "system reserve and operational headroom consume the complete host"
        )

    policy = expect_mapping(
        contract["policy"],
        {
            "aggregate_memory_limit_overcommit_ratio",
            "aggregate_cpu_limit_overcommit_ratio",
            "service_memory_limit_to_reservation_ratio",
            "require_explicit_resources",
        },
        "capacity_contract.policy",
    )
    memory_overcommit = expect_ratio(
        policy["aggregate_memory_limit_overcommit_ratio"],
        "capacity_contract.policy.aggregate_memory_limit_overcommit_ratio",
    )
    cpu_overcommit = expect_ratio(
        policy["aggregate_cpu_limit_overcommit_ratio"],
        "capacity_contract.policy.aggregate_cpu_limit_overcommit_ratio",
    )
    service_memory_ratio = expect_ratio(
        policy["service_memory_limit_to_reservation_ratio"],
        "capacity_contract.policy.service_memory_limit_to_reservation_ratio",
    )
    if memory_overcommit != Decimal("1.00"):
        raise CapacityError("memory overcommit must remain disabled without swap")
    if cpu_overcommit > Decimal("2.50"):
        raise CapacityError("aggregate CPU overcommit may not exceed 2.50")
    if service_memory_ratio > Decimal("2.50"):
        raise CapacityError(
            "service memory limit-to-reservation ratio may not exceed 2.50"
        )
    if policy["require_explicit_resources"] is not True:
        raise CapacityError("every service must keep explicit resource controls")

    application_guards = expect_mapping(
        contract["application_guards"],
        {"minecraft", "n8n", "redis", "selenium"},
        "capacity_contract.application_guards",
    )
    minecraft_guard = expect_mapping(
        application_guards["minecraft"],
        {"minimum_non_heap_memory_mib"},
        "capacity_contract.application_guards.minecraft",
    )
    minecraft_non_heap = expect_int(
        minecraft_guard["minimum_non_heap_memory_mib"],
        "capacity_contract.application_guards.minecraft" ".minimum_non_heap_memory_mib",
        minimum=1,
    )
    if minecraft_non_heap < 1024:
        raise CapacityError(
            "Minecraft must preserve at least 1024 MiB outside its JVM heap"
        )
    n8n_guard = expect_mapping(
        application_guards["n8n"],
        {"maximum_decompressed_to_limit_ratio"},
        "capacity_contract.application_guards.n8n",
    )
    n8n_decompressed_ratio = expect_ratio(
        n8n_guard["maximum_decompressed_to_limit_ratio"],
        "capacity_contract.application_guards.n8n"
        ".maximum_decompressed_to_limit_ratio",
    )
    if n8n_decompressed_ratio > Decimal("0.25"):
        raise CapacityError(
            "n8n decompressed input may not exceed 0.25 of its memory limit"
        )
    redis_guard = expect_mapping(
        application_guards["redis"],
        {
            "reviewed_maxmemory_mib",
            "minimum_process_overhead_mib",
            "required_maxmemory_policy",
            "require_persistence_disabled",
        },
        "capacity_contract.application_guards.redis",
    )
    redis_maxmemory = expect_int(
        redis_guard["reviewed_maxmemory_mib"],
        "capacity_contract.application_guards.redis.reviewed_maxmemory_mib",
        minimum=1,
    )
    redis_overhead = expect_int(
        redis_guard["minimum_process_overhead_mib"],
        "capacity_contract.application_guards.redis.minimum_process_overhead_mib",
        minimum=1,
    )
    if redis_overhead < 32:
        raise CapacityError(
            "Redis must preserve at least 32 MiB for process memory outside maxmemory"
        )
    if redis_guard["required_maxmemory_policy"] != "allkeys-lru":
        raise CapacityError("schema v1 requires Redis allkeys-lru eviction")
    if redis_guard["require_persistence_disabled"] is not True:
        raise CapacityError("schema v1 requires Redis persistence to remain disabled")
    selenium_guard = expect_mapping(
        application_guards["selenium"],
        {
            "minimum_non_shm_memory_mib",
            "require_tracing_disabled",
            "require_vnc_disabled",
        },
        "capacity_contract.application_guards.selenium",
    )
    selenium_non_shm = expect_int(
        selenium_guard["minimum_non_shm_memory_mib"],
        "capacity_contract.application_guards.selenium" ".minimum_non_shm_memory_mib",
        minimum=1,
    )
    if selenium_non_shm < 512:
        raise CapacityError("Selenium must preserve at least 512 MiB outside /dev/shm")
    if (
        selenium_guard["require_tracing_disabled"] is not True
        or selenium_guard["require_vnc_disabled"] is not True
    ):
        raise CapacityError(
            "schema v1 requires Selenium tracing and VNC to remain disabled"
        )

    stacks = expect_mapping(
        contract["stacks"], set(STACK_IDS), "capacity_contract.stacks"
    )
    normalized_stacks: dict[str, dict[str, list[str]]] = {}
    for stack_id in sorted(STACK_IDS):
        stack = expect_mapping(
            stacks[stack_id],
            {"expected_services", "global_services"},
            f"capacity_contract.stacks.{stack_id}",
        )
        expected_services = expect_identifiers(
            stack["expected_services"],
            f"capacity_contract.stacks.{stack_id}.expected_services",
        )
        global_services = expect_identifiers(
            stack["global_services"],
            f"capacity_contract.stacks.{stack_id}.global_services",
        )
        if not expected_services:
            raise CapacityError(f"{stack_id} must contain at least one service")
        if not set(global_services).issubset(expected_services):
            raise CapacityError(
                f"{stack_id} global_services is not a subset of expected_services"
            )
        normalized_stacks[stack_id] = {
            "expected_services": expected_services,
            "global_services": global_services,
        }

    reviewed = expect_mapping(
        contract["reviewed_totals"],
        set(STACK_IDS) | {"aggregate"},
        "capacity_contract.reviewed_totals",
    )
    normalized_totals: dict[str, dict[str, dict[str, int]]] = {}
    calculated_aggregate = empty_resources()
    for stack_id in sorted(STACK_IDS):
        normalized_totals[stack_id] = validate_resource_totals(
            reviewed[stack_id],
            f"capacity_contract.reviewed_totals.{stack_id}",
        )
        add_resources(calculated_aggregate, normalized_totals[stack_id])
    normalized_totals["aggregate"] = validate_resource_totals(
        reviewed["aggregate"],
        "capacity_contract.reviewed_totals.aggregate",
    )
    if normalized_totals["aggregate"] != calculated_aggregate:
        raise CapacityError("reviewed aggregate differs from the stack totals")

    return {
        "topology": {"eligible_nodes": eligible_nodes},
        "host": {
            "minimum_cpu_millicores": minimum_cpu,
            "minimum_memory_mib": minimum_memory,
            "required_swap_mib": required_swap,
        },
        "system_reserve": {
            "cpu_millicores": reserve_cpu,
            "memory_mib": reserve_memory,
        },
        "operational_headroom": {"memory_mib": headroom_memory},
        "policy": {
            "aggregate_memory_limit_overcommit_ratio": memory_overcommit,
            "aggregate_cpu_limit_overcommit_ratio": cpu_overcommit,
            "service_memory_limit_to_reservation_ratio": service_memory_ratio,
        },
        "application_guards": {
            "minecraft": {
                "minimum_non_heap_memory_mib": minecraft_non_heap,
            },
            "n8n": {
                "maximum_decompressed_to_limit_ratio": n8n_decompressed_ratio,
            },
            "redis": {
                "reviewed_maxmemory_mib": redis_maxmemory,
                "minimum_process_overhead_mib": redis_overhead,
                "required_maxmemory_policy": "allkeys-lru",
                "require_persistence_disabled": True,
            },
            "selenium": {
                "minimum_non_shm_memory_mib": selenium_non_shm,
                "require_tracing_disabled": True,
                "require_vnc_disabled": True,
            },
        },
        "stacks": normalized_stacks,
        "reviewed_totals": normalized_totals,
    }


def parse_cpu(value: Any, context: str) -> int:
    if not isinstance(value, str) or not CPU_RE.fullmatch(value):
        raise CapacityError(f"{context} must be a quoted CPU value with two decimals")
    millicores = Decimal(value) * 1000
    if millicores <= 0 or millicores != millicores.to_integral_value():
        raise CapacityError(f"{context} is not an exact positive millicore value")
    return int(millicores)


def parse_memory(value: Any, context: str) -> int:
    if not isinstance(value, str):
        raise CapacityError(f"{context} must be a canonical MiB string")
    match = MEMORY_RE.fullmatch(value)
    if match is None:
        raise CapacityError(f"{context} must use the canonical positive M suffix")
    return int(match.group(1))


def parse_application_memory(value: Any, context: str) -> int:
    if not isinstance(value, str):
        raise CapacityError(f"{context} must be a canonical M or G string")
    match = APPLICATION_MEMORY_RE.fullmatch(value)
    if match is None:
        raise CapacityError(f"{context} must use a canonical positive M or G suffix")
    multiplier = 1024 if match.group(2) == "G" else 1
    return int(match.group(1)) * multiplier


def parse_redis_config(text: str) -> dict[str, str]:
    if not isinstance(text, str):
        raise CapacityError("Redis configuration must be text")
    guarded_directives = {
        "appendonly",
        "maxmemory",
        "maxmemory-policy",
        "save",
    }
    directives: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(maxsplit=1)
        directive = fields[0].lower()
        if directive == "include":
            raise CapacityError(
                "Redis include directives are not allowed by the reviewed contract"
            )
        if directive not in guarded_directives:
            continue
        if len(fields) != 2 or not fields[1].strip():
            raise CapacityError(
                f"Redis directive {directive} on line {line_number} has no value"
            )
        if directive in directives:
            raise CapacityError(
                f"Redis directive {directive} is declared more than once"
            )
        directives[directive] = fields[1].strip()
    missing = sorted(guarded_directives - set(directives))
    if missing:
        raise CapacityError(
            "Redis configuration lacks guarded directives: " + ", ".join(missing)
        )
    return directives


def validate_application_guards(
    contract: dict[str, Any],
    stack_documents: dict[str, Any],
    resource_plans: dict[str, dict[str, dict[str, dict[str, int]]]],
    redis_config_text: str,
) -> None:
    workloads = stack_documents["workloads"]["services"]
    plans = resource_plans["workloads"]

    minecraft_environment = workloads["minecraft"].get("environment")
    if not isinstance(minecraft_environment, dict):
        raise CapacityError("minecraft.environment must be a mapping")
    minecraft_memory_values = {
        variable: parse_application_memory(
            minecraft_environment.get(variable),
            f"minecraft.environment.{variable}",
        )
        for variable in ("INIT_MEMORY", "MAX_MEMORY", "MEMORY")
    }
    if len(set(minecraft_memory_values.values())) != 1:
        raise CapacityError(
            "Minecraft INIT_MEMORY, MAX_MEMORY and MEMORY must be identical"
        )
    minecraft_limit = plans["minecraft"]["limits"]["memory_mib"]
    minecraft_heap = minecraft_memory_values["MAX_MEMORY"]
    minimum_non_heap = contract["application_guards"]["minecraft"][
        "minimum_non_heap_memory_mib"
    ]
    if minecraft_limit - minecraft_heap < minimum_non_heap:
        raise CapacityError(
            "Minecraft hard limit does not preserve the reviewed non-heap margin"
        )

    n8n_environment = workloads["n8n"].get("environment")
    if not isinstance(n8n_environment, dict):
        raise CapacityError("n8n.environment must be a mapping")
    decompressed_value = n8n_environment.get(
        "N8N_COMPRESSION_NODE_MAX_DECOMPRESSED_SIZE_BYTES"
    )
    if not isinstance(decompressed_value, str) or not re.fullmatch(
        r"[1-9][0-9]*", decompressed_value
    ):
        raise CapacityError(
            "n8n decompressed size guard must be a quoted positive byte count"
        )
    decompressed_bytes = int(decompressed_value)
    if decompressed_bytes % MIB != 0:
        raise CapacityError("n8n decompressed size guard must be whole MiB")
    n8n_limit_bytes = plans["n8n"]["limits"]["memory_mib"] * MIB
    decompressed_ratio = Decimal(decompressed_bytes) / Decimal(n8n_limit_bytes)
    if (
        decompressed_ratio
        > contract["application_guards"]["n8n"]["maximum_decompressed_to_limit_ratio"]
    ):
        raise CapacityError(
            "n8n decompressed input allowance exceeds its reviewed limit ratio"
        )

    redis = workloads["redis-coordinator"]
    if redis.get("command") != [
        "redis-server",
        "/usr/local/etc/redis/redis.conf",
    ]:
        raise CapacityError(
            "Redis must start only from the reviewed versioned configuration"
        )
    redis_directives = parse_redis_config(redis_config_text)
    redis_maxmemory_match = REDIS_MEMORY_RE.fullmatch(redis_directives["maxmemory"])
    if redis_maxmemory_match is None:
        raise CapacityError("Redis maxmemory must use the canonical positive mb suffix")
    redis_maxmemory_mib = int(redis_maxmemory_match.group(1))
    redis_guard = contract["application_guards"]["redis"]
    if redis_maxmemory_mib != redis_guard["reviewed_maxmemory_mib"]:
        raise CapacityError("Redis maxmemory differs from the reviewed dataset budget")
    if redis_directives["maxmemory-policy"] != redis_guard["required_maxmemory_policy"]:
        raise CapacityError("Redis eviction policy differs from the reviewed policy")
    if redis_guard["require_persistence_disabled"] and (
        redis_directives["save"] != '""'
        or redis_directives["appendonly"].lower() != "no"
    ):
        raise CapacityError("Redis RDB and AOF persistence must remain disabled")
    redis_limit = plans["redis-coordinator"]["limits"]["memory_mib"]
    redis_overhead = redis_guard["minimum_process_overhead_mib"]
    if redis_maxmemory_mib + redis_overhead > redis_limit:
        raise CapacityError(
            "Redis maxmemory plus reviewed process overhead exceeds its hard limit"
        )

    selenium = workloads["selenium"]
    selenium_environment = selenium.get("environment")
    if not isinstance(selenium_environment, dict):
        raise CapacityError("selenium.environment must be a mapping")
    if (
        selenium_environment.get("SE_NODE_MAX_SESSIONS") != "1"
        or selenium_environment.get("SE_NODE_OVERRIDE_MAX_SESSIONS") != "false"
    ):
        raise CapacityError("Selenium must remain limited to one browser session")
    selenium_guard = contract["application_guards"]["selenium"]
    if (
        selenium_guard["require_tracing_disabled"]
        and selenium_environment.get("SE_ENABLE_TRACING") != "false"
    ):
        raise CapacityError("Selenium tracing must remain disabled")
    if (
        selenium_guard["require_vnc_disabled"]
        and selenium_environment.get("SE_START_VNC") != "false"
    ):
        raise CapacityError("Selenium VNC must remain disabled")

    tmpfs = selenium.get("tmpfs")
    if not isinstance(tmpfs, list) or len(tmpfs) != 1:
        raise CapacityError("Selenium must declare exactly one reviewed tmpfs")
    tmpfs_match = (
        TMPFS_MEMORY_RE.fullmatch(tmpfs[0]) if isinstance(tmpfs[0], str) else None
    )
    if tmpfs_match is None:
        raise CapacityError("Selenium /dev/shm tmpfs differs from reviewed syntax")
    tmpfs_multiplier = 1024 if tmpfs_match.group(2) == "g" else 1
    selenium_shm_mib = int(tmpfs_match.group(1)) * tmpfs_multiplier
    selenium_limit = plans["selenium"]["limits"]["memory_mib"]
    minimum_non_shm = selenium_guard["minimum_non_shm_memory_mib"]
    if selenium_limit - selenium_shm_mib < minimum_non_shm:
        raise CapacityError(
            "Selenium hard limit does not preserve the reviewed non-shm margin"
        )


def service_resources(
    stack_id: str,
    service_name: str,
    service: Any,
    global_services: set[str],
    eligible_nodes: int,
    maximum_memory_ratio: Decimal,
) -> dict[str, dict[str, int]]:
    context = f"{stack_id}.services.{service_name}"
    if not isinstance(service, dict):
        raise CapacityError(f"{context} must be a mapping")
    deploy = service.get("deploy")
    if not isinstance(deploy, dict):
        raise CapacityError(f"{context}.deploy must be a mapping")

    mode = deploy.get("mode")
    if service_name in global_services:
        if mode != "global" or "replicas" in deploy:
            raise CapacityError(
                f"{context} must use global mode without an explicit replica count"
            )
        instances = eligible_nodes
    else:
        if mode != "replicated" or deploy.get("replicas") != 1:
            raise CapacityError(
                f"{context} must use replicated mode with exactly one replica"
            )
        instances = 1

    resources = expect_mapping(
        deploy.get("resources"),
        {"limits", "reservations"},
        f"{context}.deploy.resources",
    )
    parsed = empty_resources()
    for resource_class in ("reservations", "limits"):
        values = expect_mapping(
            resources[resource_class],
            {"cpus", "memory"},
            f"{context}.deploy.resources.{resource_class}",
        )
        parsed[resource_class] = {
            "cpu_millicores": parse_cpu(
                values["cpus"],
                f"{context}.deploy.resources.{resource_class}.cpus",
            )
            * instances,
            "memory_mib": parse_memory(
                values["memory"],
                f"{context}.deploy.resources.{resource_class}.memory",
            )
            * instances,
        }

    for resource_name in ("cpu_millicores", "memory_mib"):
        if parsed["reservations"][resource_name] > parsed["limits"][resource_name]:
            raise CapacityError(
                f"{context} reserves more {resource_name} than its hard limit"
            )
    memory_ratio = Decimal(parsed["limits"]["memory_mib"]) / Decimal(
        parsed["reservations"]["memory_mib"]
    )
    if memory_ratio > maximum_memory_ratio:
        raise CapacityError(
            f"{context} memory limit/reservation ratio {memory_ratio} "
            f"exceeds {maximum_memory_ratio}"
        )
    return parsed


def validate_stacks(
    contract: dict[str, Any],
    stack_documents: dict[str, Any],
    redis_config_text: str | None = None,
) -> dict[str, dict[str, dict[str, int]]]:
    if set(stack_documents) != STACK_IDS:
        raise CapacityError("exactly edge, workloads and observability are required")

    totals: dict[str, dict[str, dict[str, int]]] = {}
    resource_plans: dict[
        str,
        dict[str, dict[str, dict[str, int]]],
    ] = {}
    aggregate = empty_resources()
    for stack_id in sorted(STACK_IDS):
        document = stack_documents[stack_id]
        if not isinstance(document, dict) or not isinstance(
            document.get("services"), dict
        ):
            raise CapacityError(f"{stack_id} stack must contain a services mapping")
        services = document["services"]
        stack_contract = contract["stacks"][stack_id]
        expected_services = set(stack_contract["expected_services"])
        if set(services) != expected_services:
            missing = sorted(expected_services - set(services))
            unexpected = sorted(set(services) - expected_services)
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            raise CapacityError(f"{stack_id} services differ: {'; '.join(details)}")

        stack_total = empty_resources()
        stack_plans: dict[str, dict[str, dict[str, int]]] = {}
        for service_name in sorted(expected_services):
            plan = service_resources(
                stack_id,
                service_name,
                services[service_name],
                set(stack_contract["global_services"]),
                contract["topology"]["eligible_nodes"],
                contract["policy"]["service_memory_limit_to_reservation_ratio"],
            )
            stack_plans[service_name] = plan
            add_resources(stack_total, plan)
        if stack_total != contract["reviewed_totals"][stack_id]:
            raise CapacityError(
                f"{stack_id} rendered totals differ from reviewed_totals: "
                f"{stack_total!r}"
            )
        totals[stack_id] = stack_total
        resource_plans[stack_id] = stack_plans
        add_resources(aggregate, stack_total)

    if redis_config_text is None:
        redis_config_text = load_text(DEFAULT_REDIS_CONFIG)
    validate_application_guards(
        contract,
        stack_documents,
        resource_plans,
        redis_config_text,
    )
    if aggregate != contract["reviewed_totals"]["aggregate"]:
        raise CapacityError("rendered aggregate differs from reviewed aggregate")
    totals["aggregate"] = aggregate
    validate_budget(contract, aggregate)
    return totals


def validate_budget(
    contract: dict[str, Any],
    aggregate: dict[str, dict[str, int]],
) -> None:
    allocatable_memory = (
        contract["host"]["minimum_memory_mib"]
        - contract["system_reserve"]["memory_mib"]
        - contract["operational_headroom"]["memory_mib"]
    )
    allocatable_cpu = (
        contract["host"]["minimum_cpu_millicores"]
        - contract["system_reserve"]["cpu_millicores"]
    )
    memory_limit_budget = int(
        Decimal(allocatable_memory)
        * contract["policy"]["aggregate_memory_limit_overcommit_ratio"]
    )
    cpu_limit_budget = int(
        Decimal(allocatable_cpu)
        * contract["policy"]["aggregate_cpu_limit_overcommit_ratio"]
    )

    if aggregate["reservations"]["memory_mib"] > allocatable_memory:
        raise CapacityError(
            "aggregate memory reservations consume reviewed host headroom"
        )
    if aggregate["limits"]["memory_mib"] > memory_limit_budget:
        raise CapacityError("aggregate memory limits consume swapless host headroom")
    if aggregate["reservations"]["cpu_millicores"] > allocatable_cpu:
        raise CapacityError("aggregate CPU reservations exceed allocatable CPU")
    if aggregate["limits"]["cpu_millicores"] > cpu_limit_budget:
        raise CapacityError("aggregate CPU limits exceed reviewed overcommit budget")


def host_facts() -> dict[str, int]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] in {"MemTotal:", "SwapTotal:"}:
                values[fields[0]] = int(fields[1])
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise CapacityError(f"cannot read host memory facts: {error}") from error
    if set(values) != {"MemTotal:", "SwapTotal:"}:
        raise CapacityError("host memory facts are incomplete")
    cpu_cores = os.cpu_count()
    if cpu_cores is None or cpu_cores < 1:
        raise CapacityError("cannot determine host CPU count")
    return {
        "cpu_millicores": cpu_cores * 1000,
        "memory_mib": values["MemTotal:"] // 1024,
        "swap_mib": values["SwapTotal:"] // 1024,
    }


def validate_host(contract: dict[str, Any], actual: dict[str, int]) -> None:
    expected_keys = {"cpu_millicores", "memory_mib", "swap_mib"}
    if set(actual) != expected_keys or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in actual.values()
    ):
        raise CapacityError("host facts are malformed")
    if actual["cpu_millicores"] < contract["host"]["minimum_cpu_millicores"]:
        raise CapacityError("host CPU is below the reviewed minimum")
    if actual["memory_mib"] < contract["host"]["minimum_memory_mib"]:
        raise CapacityError("host RAM is below the reviewed minimum")
    if actual["swap_mib"] != contract["host"]["required_swap_mib"]:
        raise CapacityError("host swap differs from the reviewed swapless policy")


def parse_stack_arguments(values: list[str] | None) -> dict[str, Path]:
    if values is None:
        return dict(DEFAULT_STACKS)
    result: dict[str, Path] = {}
    for value in values:
        stack_id, separator, raw_path = value.partition("=")
        if not separator or stack_id not in STACK_IDS or not raw_path:
            raise CapacityError("--stack must use edge|workloads|observability=PATH")
        if stack_id in result:
            raise CapacityError(f"duplicate --stack identity: {stack_id}")
        result[stack_id] = Path(raw_path)
    if set(result) != STACK_IDS:
        raise CapacityError("all three stack identities must be provided")
    return result


def format_report(
    contract: dict[str, Any],
    totals: dict[str, dict[str, dict[str, int]]],
) -> str:
    aggregate = totals["aggregate"]
    allocatable_memory = (
        contract["host"]["minimum_memory_mib"]
        - contract["system_reserve"]["memory_mib"]
        - contract["operational_headroom"]["memory_mib"]
    )
    allocatable_cpu = (
        contract["host"]["minimum_cpu_millicores"]
        - contract["system_reserve"]["cpu_millicores"]
    )
    return (
        "Capacity contract passed: "
        f"{aggregate['reservations']['memory_mib']} MiB reserved / "
        f"{aggregate['limits']['memory_mib']} MiB limited / "
        f"{allocatable_memory} MiB stack budget after reserve/headroom; "
        f"{aggregate['reservations']['cpu_millicores']}m CPU reserved / "
        f"{aggregate['limits']['cpu_millicores']}m CPU limited / "
        f"{allocatable_cpu}m CPU allocatable at "
        f"{contract['policy']['aggregate_cpu_limit_overcommit_ratio']}x."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--redis-config",
        type=Path,
        default=DEFAULT_REDIS_CONFIG,
        help="Redis configuration whose maxmemory must fit the container limit",
    )
    parser.add_argument(
        "--stack",
        action="append",
        help="rendered stack identity and path as STACK=PATH; repeat three times",
    )
    parser.add_argument(
        "--verify-host",
        action="store_true",
        help="also compare local /proc facts with the reviewed host minimum",
    )
    args = parser.parse_args(argv)
    try:
        contract = validate_contract(load_yaml(args.contract))
        stack_paths = parse_stack_arguments(args.stack)
        totals = validate_stacks(
            contract,
            {stack_id: load_yaml(path) for stack_id, path in stack_paths.items()},
            load_text(args.redis_config),
        )
        if args.verify_host:
            validate_host(contract, host_facts())
    except CapacityError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(format_report(contract, totals))
    if args.verify_host:
        print("Local host facts satisfy the reviewed capacity floor and swap policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
