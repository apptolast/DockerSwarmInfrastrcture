from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = REPOSITORY_ROOT / ".build/observability"


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        name,
        REPOSITORY_ROOT / relative_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = load_script(
    "validate_observability",
    "scripts/validate-observability.py",
)
secret_installer = load_script(
    "install_observability_secrets",
    "scripts/install-observability-secrets.py",
)
db_provisioner = load_script(
    "provision_observability_db_users",
    "scripts/provision-observability-db-users.py",
)
docker_proxy = load_script(
    "docker_readonly_proxy",
    "scripts/docker-readonly-proxy.py",
)
local_tunnel = load_script(
    "observability_tunnel",
    "scripts/observability-tunnel.py",
)
deployment_validator = load_script(
    "validate_swarm_deployment_observability",
    "scripts/validate-swarm-deployment.py",
)


class ObservabilityStackContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = validator.load_yaml(BUILD_ROOT / "stack.yml")
        cls.services = validator.load_yaml(REPOSITORY_ROOT / "config/services.yml")
        cls.secrets = validator.load_yaml(
            REPOSITORY_ROOT / "stacks/observability/secrets.yml"
        )
        cls.platform = validator.load_yaml(REPOSITORY_ROOT / "config/platform.yml")

    def assert_rejected(self, candidate: dict) -> None:
        with self.assertRaises(validator.ContractError):
            validator.validate_stack(
                candidate,
                self.services,
                self.secrets,
                self.platform,
            )

    def test_exact_internal_stack_is_accepted(self) -> None:
        validator.validate_stack(
            self.stack,
            self.services,
            self.secrets,
            self.platform,
        )
        validator.validate_configs(
            self.stack,
            self.services,
            BUILD_ROOT / "config",
        )

    def test_published_port_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["grafana"]["ports"] = [
            {
                "target": 3000,
                "published": 3000,
                "protocol": "tcp",
                "mode": "host",
            }
        ]
        self.assert_rejected(candidate)

    def test_mutable_or_wrong_image_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["grafana"]["image"] = "grafana/grafana:latest"
        self.assert_rejected(candidate)

    def test_legacy_service_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["monitoring-dozzle"] = copy.deepcopy(
            candidate["services"]["grafana"]
        )
        self.assert_rejected(candidate)

    def test_missing_health_and_resource_contract_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["prometheus"].pop("healthcheck")
        candidate["services"]["prometheus"]["deploy"].pop("resources")
        self.assert_rejected(candidate)

    def test_generic_healthcheck_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["grafana"]["healthcheck"]["test"][
            -1
        ] = "http://127.0.0.1:3000/"
        self.assert_rejected(candidate)

    def test_plaintext_password_environment_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["grafana"]["environment"][
            "GF_SECURITY_ADMIN_PASSWORD"
        ] = "forbidden"
        self.assert_rejected(candidate)

    def test_unreviewed_docker_socket_consumer_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["grafana"].setdefault("volumes", []).append(
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock",
                "read_only": True,
            }
        )
        self.assert_rejected(candidate)

    def test_direct_socket_replacing_restricted_proxy_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        proxy_mount = candidate["services"]["alloy"]["volumes"][0]
        proxy_mount["source"] = "/var/run/docker.sock"
        proxy_mount["target"] = "/var/run/docker.sock"
        self.assert_rejected(candidate)

    def test_attachable_monitoring_network_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["networks"]["monitoring"]["attachable"] = True
        self.assert_rejected(candidate)

    def test_minecraft_monitoring_third_consumer_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["prometheus"]["networks"].append("minecraft-monitoring")
        self.assert_rejected(candidate)


class ObservabilityDeploymentIdentityTests(unittest.TestCase):
    EXTERNAL_NETWORK_INTERNAL = {
        "edge-monitoring": False,
        "n8n-monitoring": True,
        "workloads-n8n-backend": True,
        "workloads-n8n-coordination": True,
        "workloads-passbolt-backend": True,
        "workloads-shlink-backend": True,
        "minecraft-monitoring": True,
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = validator.load_yaml(BUILD_ROOT / "stack.yml")

    def deployment_fixture(
        self,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        dict[str, dict[str, str]],
        dict[str, dict[str, str]],
        dict[str, dict[str, object]],
    ]:
        verified_configs = {
            alias: {
                "id": f"config-id-{index}",
                "name": declaration["name"],
            }
            for index, (alias, declaration) in enumerate(self.stack["configs"].items())
        }
        verified_secrets = {
            alias: {
                "id": f"secret-id-{index}",
                "name": declaration["name"],
            }
            for index, (alias, declaration) in enumerate(self.stack["secrets"].items())
        }
        network_ids: dict[str, str] = {}
        inspected_networks: list[dict[str, object]] = []
        verified_external_networks: dict[str, dict[str, object]] = {}
        for index, (logical_name, declaration) in enumerate(
            self.stack["networks"].items()
        ):
            physical_name = declaration.get(
                "name",
                f"observability_{logical_name}",
            )
            network_id = f"network-id-{index}"
            network_ids[logical_name] = network_id
            internal = (
                self.EXTERNAL_NETWORK_INTERNAL[logical_name]
                if declaration.get("external") is True
                else declaration.get("internal", False)
            )
            inspected_networks.append(
                {
                    "Id": network_id,
                    "Name": physical_name,
                    "Driver": "overlay",
                    "Scope": "swarm",
                    "Attachable": False,
                    "Internal": internal,
                    "Options": {"encrypted": ""},
                }
            )
            if declaration.get("external") is True:
                verified_external_networks[logical_name] = {
                    "id": network_id,
                    "name": physical_name,
                    "internal": internal,
                }

        inspected_services: list[dict[str, object]] = []
        for logical_name, contract in self.stack["services"].items():
            configs = []
            for reference in contract.get("configs", []):
                alias = reference["source"]
                identity = verified_configs[alias]
                configs.append(
                    {
                        "ConfigID": identity["id"],
                        "ConfigName": identity["name"],
                        "File": {
                            "Name": reference.get("target", alias),
                            "UID": str(reference.get("uid", "0")),
                            "GID": str(reference.get("gid", "0")),
                            "Mode": reference.get("mode", 0o444),
                        },
                    }
                )
            secrets = []
            for reference in contract.get("secrets", []):
                alias = reference["source"]
                identity = verified_secrets[alias]
                secrets.append(
                    {
                        "SecretID": identity["id"],
                        "SecretName": identity["name"],
                        "File": {
                            "Name": reference.get("target", alias),
                            "UID": str(reference.get("uid", "0")),
                            "GID": str(reference.get("gid", "0")),
                            "Mode": reference.get("mode", 0o444),
                        },
                    }
                )
            inspected_services.append(
                {
                    "Spec": {
                        "Name": f"observability_{logical_name}",
                        "TaskTemplate": {
                            "ContainerSpec": {
                                "Configs": configs,
                                "Secrets": secrets,
                            },
                            "Networks": [
                                {"Target": network_ids[network]}
                                for network in contract.get("networks", [])
                            ],
                        },
                    }
                }
            )
        return (
            inspected_services,
            inspected_networks,
            verified_configs,
            verified_secrets,
            verified_external_networks,
        )

    def validate(
        self,
        inspected_services: list[dict[str, object]],
        inspected_networks: list[dict[str, object]],
        verified_configs: dict[str, dict[str, str]],
        verified_secrets: dict[str, dict[str, str]],
        verified_external_networks: dict[str, dict[str, object]],
    ) -> None:
        deployment_validator.validate_deployment(
            self.stack,
            "observability",
            inspected_services,
            inspected_networks,
            verified_configs,
            verified_secrets,
            verified_external_networks,
        )

    def test_exact_verified_observability_references_are_accepted(self) -> None:
        self.validate(*self.deployment_fixture())

    def test_rollback_to_stale_observability_config_is_rejected(self) -> None:
        fixture = list(self.deployment_fixture())
        services = copy.deepcopy(fixture[0])
        prometheus = next(
            item
            for item in services
            if item["Spec"]["Name"] == "observability_prometheus"
        )
        prometheus["Spec"]["TaskTemplate"]["ContainerSpec"]["Configs"][0][
            "ConfigID"
        ] = "stale-config-id"
        fixture[0] = services
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "Config references differ",
        ):
            self.validate(*fixture)

    def test_extra_observability_secret_or_network_is_rejected(self) -> None:
        fixture = list(self.deployment_fixture())
        services = copy.deepcopy(fixture[0])
        alertmanager = next(
            item
            for item in services
            if item["Spec"]["Name"] == "observability_alertmanager"
        )
        first_secret = next(iter(fixture[3].values()))
        alertmanager["Spec"]["TaskTemplate"]["ContainerSpec"]["Secrets"].append(
            {
                "SecretID": first_secret["id"],
                "SecretName": first_secret["name"],
                "File": {
                    "Name": "unexpected",
                    "UID": "0",
                    "GID": "0",
                    "Mode": 0o400,
                },
            }
        )
        fixture[0] = services
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "Secret references differ",
        ):
            self.validate(*fixture)

        fixture = list(self.deployment_fixture())
        services = copy.deepcopy(fixture[0])
        alertmanager = next(
            item
            for item in services
            if item["Spec"]["Name"] == "observability_alertmanager"
        )
        alertmanager["Spec"]["TaskTemplate"]["Networks"].append(
            {"Target": "network-id-unreviewed"}
        )
        fixture[0] = services
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "network references differ",
        ):
            self.validate(*fixture)

    def test_external_network_is_bound_to_verified_id_and_mode(self) -> None:
        fixture = list(self.deployment_fixture())
        verified_external = copy.deepcopy(fixture[4])
        verified_external["workloads-n8n-backend"]["id"] = "stale-network-id"
        fixture[4] = verified_external
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "verified identity",
        ):
            self.validate(*fixture)

        fixture = list(self.deployment_fixture())
        networks = copy.deepcopy(fixture[1])
        backend = next(
            item for item in networks if item["Name"] == "workloads_n8n-backend"
        )
        backend["Internal"] = False
        fixture[1] = networks
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "network differs",
        ):
            self.validate(*fixture)

    def test_observability_role_runs_generic_postdeploy_gate(self) -> None:
        render = (
            REPOSITORY_ROOT / "ansible/roles/observability/tasks/render.yml"
        ).read_text(encoding="utf-8")
        deploy = (
            REPOSITORY_ROOT / "ansible/roles/observability/tasks/deploy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("validate-swarm-deployment.py", render)
        self.assertIn("validate-swarm-deployment.py", deploy)
        self.assertIn("--verified-configs", deploy)
        self.assertIn("--verified-secrets", deploy)
        self.assertIn("--verified-external-networks", deploy)


class ObservabilitySecretTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = yaml.safe_load(
            (REPOSITORY_ROOT / "stacks/observability/secrets.yml").read_text(
                encoding="utf-8"
            )
        )

    def test_catalog_and_atomic_source_generation(self) -> None:
        version, entries = secret_installer.validate_contract(self.catalog)
        self.assertEqual(version, "v1")
        self.assertEqual(len(entries), 5)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            entry = entries[0]
            source = root / entry["source_file"]
            generated = secret_installer.atomic_generate_source(
                source,
                entry["generated_bytes"],
            )
            self.assertEqual(secret_installer.read_source(source), generated)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
            self.assertGreaterEqual(len(generated.rstrip(b"\n")), 32)

    def test_existing_secret_metadata_binds_source_fingerprint(self) -> None:
        version, entries = secret_installer.validate_contract(self.catalog)
        entry = entries[0]
        fingerprint = "a" * 64
        secret = {
            "Spec": {
                "Name": entry["external_name"],
                "Labels": {
                    "com.apptolast.managed-by": ("observability-secret-installer"),
                    "com.apptolast.secret-key": entry["key"],
                    "com.apptolast.secret-version": version,
                    "com.apptolast.source-sha256": fingerprint,
                },
            }
        }
        secret_installer.validate_secret_metadata(
            secret,
            entry,
            version,
            fingerprint,
        )
        secret["Spec"]["Labels"]["com.apptolast.source-sha256"] = "b" * 64
        with self.assertRaises(secret_installer.SecretInstallError):
            secret_installer.validate_secret_metadata(
                secret,
                entry,
                version,
                fingerprint,
            )

    def test_sql_builder_quotes_secret_and_tracks_rotation(self) -> None:
        sql = db_provisioner.build_sql(
            "apptolast_monitor",
            "n8n",
            "safe'password",
            "apptolast-observability:v1:fingerprint",
        )
        self.assertIn("safe''password", sql)
        self.assertIn("GRANT pg_monitor", sql)
        self.assertIn("COMMENT ON ROLE", sql)
        self.assertNotIn("safe'password", sql)


class ObservabilityAccessTests(unittest.TestCase):
    def test_access_helpers_use_only_local_loopback_and_ssh_stdio(self) -> None:
        remote = (
            REPOSITORY_ROOT / "scripts/observability-forward-remote.py"
        ).read_text(encoding="utf-8")
        local = (REPOSITORY_ROOT / "scripts/observability-tunnel.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("listener.bind(", remote)
        self.assertNotIn("listener.listen(", remote)
        self.assertIn('"--stdio"', remote)
        self.assertIn('listener.bind(("127.0.0.1", args.local_port))', local)
        self.assertNotIn('listener.bind(("0.0.0.0"', local)
        command = local_tunnel.ssh_command("admin@example.test", "grafana")
        self.assertIn("-T", command)
        self.assertIn("--stdio", command)
        self.assertNotIn("-L", command)

    def test_operational_helper_contract_is_accepted(self) -> None:
        validator.validate_operational_helpers(REPOSITORY_ROOT)


class DockerReadonlyProxyTests(unittest.TestCase):
    def test_required_alloy_get_requests_are_allowlisted(self) -> None:
        container_id = "a" * 64
        allowed = [
            "/_ping",
            "/version",
            "/v1.55/containers/json?all=0&filters=%7B%7D",
            "/v1.55/networks?filters=%7B%7D",
            f"/v1.55/containers/{container_id}/json?size=0",
            (
                f"/v1.55/containers/{container_id}/logs"
                "?follow=1&stdout=1&stderr=1&timestamps=1&since=0"
            ),
        ]
        for target in allowed:
            with self.subTest(target=target):
                docker_proxy.validate_request("GET", target)
        docker_proxy.validate_request("HEAD", "/_ping")

    def test_mutations_and_unneeded_reads_are_denied(self) -> None:
        container_id = "b" * 64
        denied = [
            ("POST", "/containers/create"),
            ("DELETE", f"/containers/{container_id}"),
            ("POST", f"/containers/{container_id}/exec"),
            ("GET", "/images/json"),
            ("GET", "/services"),
            ("GET", "/events"),
            ("GET", f"/containers/{container_id}/archive?path=/"),
            ("GET", "/containers/json?unknown=true"),
        ]
        for method, target in denied:
            with self.subTest(method=method, target=target):
                with self.assertRaises(docker_proxy.RequestError):
                    docker_proxy.validate_request(method, target)

    def test_request_bodies_upgrades_and_pipelining_are_denied(self) -> None:
        invalid_headers = [
            (b"GET /_ping HTTP/1.1\r\nHost: docker\r\n" b"Content-Length: 1\r\n\r\n"),
            (b"GET /_ping HTTP/1.1\r\nHost: docker\r\n" b"Upgrade: websocket\r\n\r\n"),
        ]
        for request in invalid_headers:
            with self.subTest(request=request):
                with self.assertRaises(docker_proxy.RequestError):
                    docker_proxy.parse_headers(request)
        forwarded, _ = docker_proxy.sanitized_request(
            b"GET /_ping HTTP/1.1\r\n"
            b"Host: ignored\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        self.assertIn(b"Host: docker\r\n", forwarded)
        self.assertIn(b"Connection: close\r\n", forwarded)
        self.assertNotIn(b"keep-alive", forwarded)


if __name__ == "__main__":
    unittest.main()
