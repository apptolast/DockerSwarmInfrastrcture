"""Negative and integration tests for the single-node capacity gate."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts/validate-capacity.py"


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_capacity",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capacity = load_validator()


class CapacityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract_document = capacity.load_yaml(
            REPOSITORY_ROOT / "config/capacity.yml"
        )
        cls.redis_config_text = capacity.load_text(
            REPOSITORY_ROOT / "stacks/workloads/config/redis.conf"
        )
        cls.stack_documents = {
            stack_id: capacity.load_yaml(path)
            for stack_id, path in capacity.DEFAULT_STACKS.items()
        }

    def normalized_contract(self):
        return capacity.validate_contract(copy.deepcopy(self.contract_document))

    def test_repository_plan_matches_reviewed_totals_and_budget(self) -> None:
        contract = self.normalized_contract()
        totals = capacity.validate_stacks(
            contract,
            copy.deepcopy(self.stack_documents),
        )
        self.assertEqual(
            totals["aggregate"],
            {
                "reservations": {
                    "cpu_millicores": 3470,
                    "memory_mib": 7072,
                },
                "limits": {
                    "cpu_millicores": 17200,
                    "memory_mib": 12352,
                },
            },
        )

    def test_docker_normalizes_m_suffix_as_binary_mebibytes(self) -> None:
        for stack_id, path in capacity.DEFAULT_STACKS.items():
            normalized_text = subprocess.run(
                [
                    "docker",
                    "stack",
                    "config",
                    "--compose-file",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            normalized = yaml.safe_load(normalized_text)
            for service_name, service in self.stack_documents[stack_id][
                "services"
            ].items():
                rendered_resources = service["deploy"]["resources"]
                normalized_resources = normalized["services"][service_name]["deploy"][
                    "resources"
                ]
                for resource_class in ("reservations", "limits"):
                    expected_mib = capacity.parse_memory(
                        rendered_resources[resource_class]["memory"],
                        f"{stack_id}.{service_name}.{resource_class}",
                    )
                    self.assertEqual(
                        int(normalized_resources[resource_class]["memory"]),
                        expected_mib * capacity.MIB,
                    )

    def test_unreviewed_service_is_rejected(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        documents["edge"]["services"]["unreviewed"] = copy.deepcopy(
            documents["edge"]["services"]["traefik"]
        )
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

    def test_missing_explicit_resources_is_rejected(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        del documents["workloads"]["services"]["kropia"]["deploy"]["resources"]
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

    def test_noncanonical_memory_unit_is_rejected(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        resources = documents["edge"]["services"]["traefik"]["deploy"]["resources"]
        resources["limits"]["memory"] = "0.15625G"
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

    def test_memory_service_ratio_is_fail_closed(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        resources = documents["edge"]["services"]["traefik"]["deploy"]["resources"]
        resources["limits"]["memory"] = "161M"
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

    def test_memory_overcommit_cannot_be_enabled_without_schema_change(self) -> None:
        document = copy.deepcopy(self.contract_document)
        policy = document["capacity_contract"]["policy"]
        policy["aggregate_memory_limit_overcommit_ratio"] = "1.01"
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_contract(document)

    def test_host_and_application_safety_floors_cannot_be_weakened(self) -> None:
        mutations = (
            ("system_reserve", "memory_mib", 3071),
            ("operational_headroom", "memory_mib", 511),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                document = copy.deepcopy(self.contract_document)
                document["capacity_contract"][section][key] = value
                with self.assertRaises(capacity.CapacityError):
                    capacity.validate_contract(document)

        document = copy.deepcopy(self.contract_document)
        guards = document["capacity_contract"]["application_guards"]
        guards["minecraft"]["minimum_non_heap_memory_mib"] = 1023
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_contract(document)

        document = copy.deepcopy(self.contract_document)
        guards = document["capacity_contract"]["application_guards"]
        guards["redis"]["minimum_process_overhead_mib"] = 31
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_contract(document)

    def test_minecraft_keeps_one_gibibyte_outside_the_heap(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        environment = documents["workloads"]["services"]["minecraft"]["environment"]
        environment["MAX_MEMORY"] = "4G"
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)
        self.assertEqual(capacity.parse_application_memory("3G", "heap"), 3072)
        self.assertEqual(capacity.parse_memory("4096M", "limit"), 4096)

    def test_n8n_decompression_allowance_is_bounded_by_memory(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        environment = documents["workloads"]["services"]["n8n"]["environment"]
        environment["N8N_COMPRESSION_NODE_MAX_DECOMPRESSED_SIZE_BYTES"] = "536870912"
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

    def test_redis_maxmemory_preserves_process_overhead(self) -> None:
        document = copy.deepcopy(self.contract_document)
        redis_guard = document["capacity_contract"]["application_guards"]["redis"]
        redis_guard["reviewed_maxmemory_mib"] = 48
        contract = capacity.validate_contract(document)
        redis_config = self.redis_config_text.replace(
            "maxmemory 32mb",
            "maxmemory 48mb",
        )
        self.assertNotEqual(redis_config, self.redis_config_text)
        with self.assertRaisesRegex(
            capacity.CapacityError,
            "maxmemory plus reviewed process overhead",
        ):
            capacity.validate_stacks(
                contract,
                copy.deepcopy(self.stack_documents),
                redis_config,
            )

    def test_redis_config_must_match_reviewed_cache_policy(self) -> None:
        mutations = (
            ("maxmemory 32mb", "maxmemory 128mb"),
            ("maxmemory-policy allkeys-lru", "maxmemory-policy noeviction"),
            ('save ""', 'save "900 1"'),
            ("appendonly no", "appendonly yes"),
        )
        for original, replacement in mutations:
            with self.subTest(replacement=replacement):
                redis_config = self.redis_config_text.replace(original, replacement)
                self.assertNotEqual(redis_config, self.redis_config_text)
                with self.assertRaises(capacity.CapacityError):
                    capacity.validate_stacks(
                        self.normalized_contract(),
                        copy.deepcopy(self.stack_documents),
                        redis_config,
                    )

    def test_duplicate_guarded_redis_directive_is_rejected(self) -> None:
        redis_config = self.redis_config_text + "\nmaxmemory 32mb\n"
        with self.assertRaisesRegex(
            capacity.CapacityError,
            "declared more than once",
        ):
            capacity.validate_stacks(
                self.normalized_contract(),
                copy.deepcopy(self.stack_documents),
                redis_config,
            )

    def test_redis_config_cannot_be_bypassed_or_include_an_override(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        documents["workloads"]["services"]["redis-coordinator"]["command"] = [
            "redis-server",
            "--maxmemory",
            "128mb",
        ]
        with self.assertRaisesRegex(
            capacity.CapacityError,
            "reviewed versioned configuration",
        ):
            capacity.validate_stacks(self.normalized_contract(), documents)

        redis_config = self.redis_config_text + "\ninclude /tmp/override.conf\n"
        with self.assertRaisesRegex(capacity.CapacityError, "include directives"):
            capacity.validate_stacks(
                self.normalized_contract(),
                copy.deepcopy(self.stack_documents),
                redis_config,
            )

    def test_selenium_tmpfs_and_optional_features_are_bounded(self) -> None:
        documents = copy.deepcopy(self.stack_documents)
        selenium = documents["workloads"]["services"]["selenium"]
        selenium["tmpfs"] = ["/dev/shm:size=2g,mode=1777"]
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

        documents = copy.deepcopy(self.stack_documents)
        environment = documents["workloads"]["services"]["selenium"]["environment"]
        environment["SE_START_VNC"] = "true"
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_stacks(self.normalized_contract(), documents)

    def test_reviewed_aggregate_must_equal_stack_totals(self) -> None:
        document = copy.deepcopy(self.contract_document)
        totals = document["capacity_contract"]["reviewed_totals"]
        totals["aggregate"]["limits"]["memory_mib"] += 1
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_contract(document)

    def test_aggregate_memory_limit_cannot_consume_reserved_headroom(self) -> None:
        contract = self.normalized_contract()
        aggregate = copy.deepcopy(contract["reviewed_totals"]["aggregate"])
        allocatable = (
            contract["host"]["minimum_memory_mib"]
            - contract["system_reserve"]["memory_mib"]
            - contract["operational_headroom"]["memory_mib"]
        )
        aggregate["limits"]["memory_mib"] = allocatable + 1
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_budget(contract, aggregate)

    def test_cpu_limit_cannot_exceed_reviewed_overcommit_budget(self) -> None:
        contract = self.normalized_contract()
        aggregate = copy.deepcopy(contract["reviewed_totals"]["aggregate"])
        allocatable = (
            contract["host"]["minimum_cpu_millicores"]
            - contract["system_reserve"]["cpu_millicores"]
        )
        aggregate["limits"]["cpu_millicores"] = (
            int(
                allocatable * contract["policy"]["aggregate_cpu_limit_overcommit_ratio"]
            )
            + 1
        )
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_budget(contract, aggregate)

    def test_smaller_host_and_unreviewed_swap_are_rejected(self) -> None:
        contract = self.normalized_contract()
        facts = {
            "cpu_millicores": 8000,
            "memory_mib": 15980,
            "swap_mib": 0,
        }
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_host(contract, facts)
        facts["memory_mib"] = 15981
        facts["swap_mib"] = 1024
        with self.assertRaises(capacity.CapacityError):
            capacity.validate_host(contract, facts)

    def test_duplicate_yaml_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.yml"
            path.write_text("root:\n  key: one\n  key: two\n", encoding="utf-8")
            with self.assertRaises(capacity.CapacityError):
                capacity.load_yaml(path)


if __name__ == "__main__":
    unittest.main()
