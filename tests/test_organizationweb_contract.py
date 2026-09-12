"""Deployment boundaries of the additive OrganizationWeb stack."""

from pathlib import Path
import importlib.util
import json
import secrets
import subprocess
import tempfile
import time
import unittest
import uuid
import sys
import types
import copy
from unittest import mock

import jinja2
import yaml

from ansible_task_harness import AnsibleTaskAssertions


ROOT = Path(__file__).resolve().parents[1]


def load_image_channels_map():
    """Return the reviewed per-stack channel map the stack templates render."""
    spec = importlib.util.spec_from_file_location(
        "validate_image_channels", ROOT / "scripts/validate-image-channels.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_channel_map(ROOT)["services"]


class OrganizationWebContractTests(unittest.TestCase):
    def test_preflight_covers_installation_parents_and_file_targets_without_following_links(self):
        tasks = yaml.safe_load((ROOT / "ansible/roles/organizationweb/tasks/main.yml").read_text())
        stats = [task for task in tasks if "ansible.builtin.stat" in task]
        paths = {
            item["path"]
            for task in stats for item in task.get("loop", [])
            if isinstance(item, dict)
        }
        self.assertTrue({
            "/opt", "/opt/dockerswarm", "/opt/dockerswarm/organizationweb",
            "/opt/dockerswarm/organizationweb/stack.yml",
            "/opt/dockerswarm/organizationweb/validate-swarm-deployment.py",
        }.issubset(paths))
        self.assertTrue(all(task["ansible.builtin.stat"]["follow"] is False for task in stats))

    def test_postgres_runs_as_70_without_capabilities_and_preserves_its_database(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        service = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))["services"]["postgres"]
        name = "organizationweb-postgres-test-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="organizationweb-pg-", dir=ROOT / ".build") as temporary:
            directory = Path(temporary)
            data = directory / "data"
            data.mkdir(mode=0o700)
            for key, value in {"db_username": "organization", "db_password": secrets.token_hex(32)}.items():
                path = directory / key
                path.write_text(value)
                path.chmod(0o400)
            self.fixture_helper(directory, service["image"], "chown", [
                "70:70", "/fixture/data", "/fixture/db_username", "/fixture/db_password",
            ])
            command = [
                "docker", "run", "--detach", "--name", name,
                "--user", service["user"], "--cap-drop", "ALL",
                "--memory", "384m", "--memory-reservation", "192m",
                "--volume", f"{data}:/var/lib/postgresql/data",
            ]
            for secret in service["secrets"]:
                command.extend(["--volume", f"{directory / secret['source']}:/run/secrets/{secret['target']}:ro"])
            for key, value in service["environment"].items():
                command.extend(["--env", f"{key}={value}"])
            command.append(service["image"])
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
                self.wait_for_health(name, service)
                query = ["docker", "exec", name, "psql", "-U", "organization", "-d", "organization", "-Atc"]
                created = subprocess.run(query + ["CREATE TABLE retained(id integer); INSERT INTO retained VALUES (19)"], capture_output=True, text=True)
                self.assertEqual(created.returncode, 0, created.stderr)
                subprocess.run(["docker", "restart", name], check=True, capture_output=True, text=True)
                self.wait_for_health(name, service)
                read = subprocess.run(query + ["SELECT id FROM retained"], capture_output=True, text=True)
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertEqual(read.stdout.strip(), "19")
            finally:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, check=True)
                self.fixture_helper(directory, service["image"], "rm", ["-rf", "/fixture/data"])

    def fixture_helper(self, directory, image, executable, arguments):
        self.assertEqual(directory.parent.resolve(), (ROOT / ".build").resolve())
        self.assertFalse(directory.is_symlink())
        self.assertTrue(directory.name.startswith(("organizationweb-pg-", "organizationweb-rabbit-")))
        self.assertFalse((directory / "data").is_symlink())
        subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only",
             "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
             "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
             "--volume", f"{directory}:/fixture", "--entrypoint", executable,
             image, *arguments],
            capture_output=True, text=True, check=True,
        )

    def wait_for_health(self, name, service):
        deadline = time.monotonic() + 90
        while True:
            result = subprocess.run(
                ["docker", "exec", name, *service["healthcheck"]["test"][1:]],
                capture_output=True, text=True, check=False, timeout=15,
            )
            if result.returncode == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rabbit_runs_as_100_without_capabilities_and_preserves_its_vhost(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        service = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))["services"]["rabbitmq"]
        name = "organizationweb-rabbit-test-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="organizationweb-rabbit-", dir=ROOT / ".build") as temporary:
            directory = Path(temporary)
            data = directory / "data"
            data.mkdir(mode=0o700)
            config = directory / "rabbitmq.conf"
            config.write_text(
                "default_user = organization\n"
                f"default_pass = {secrets.token_hex(32)}\n"
                "default_vhost = organization\n"
            )
            config.chmod(0o400)
            self.fixture_helper(directory, service["image"], "chown", [
                "100:101", "/fixture/data", "/fixture/rabbitmq.conf",
            ])
            command = [
                "docker", "run", "--detach", "--name", name,
                "--hostname", service["hostname"],
                "--user", service["user"], "--cap-drop", "ALL",
                "--memory", "384m", "--memory-reservation", "192m",
                "--volume", f"{data}:/var/lib/rabbitmq",
                "--volume", f"{config}:/run/secrets/rabbitmq.conf:ro",
            ]
            for key, value in service["environment"].items():
                command.extend(["--env", f"{key}={value}"])
            command.append(service["image"])
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
                self.wait_for_health(name, service)
                control = ["docker", "exec", name, "rabbitmqctl", "-q"]
                configured = subprocess.run(control + ["list_vhosts", "name"], capture_output=True, text=True)
                self.assertEqual(configured.returncode, 0, configured.stderr)
                self.assertIn("organization", configured.stdout.splitlines())
                created = subprocess.run(control + ["add_vhost", "retained-fixture"], capture_output=True, text=True)
                self.assertEqual(created.returncode, 0, created.stderr)
                subprocess.run(["docker", "restart", name], check=True, capture_output=True, text=True)
                self.wait_for_health(name, service)
                read = subprocess.run(control + ["list_vhosts", "name"], capture_output=True, text=True)
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertIn("retained-fixture", read.stdout.splitlines())
            finally:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, check=True)
                self.fixture_helper(directory, service["image"], "rm", ["-rf", "/fixture/data"])

    def test_published_application_images_identify_the_catalog_release(self):
        app = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())["organizationweb"]
        for name in ("backend", "web"):
            with self.subTest(image=name):
                subprocess.run(
                    ["docker", "pull", app["images"][name]],
                    text=True, capture_output=True, check=True,
                )
                completed = subprocess.run(
                    ["docker", "image", "inspect", app["images"][name]],
                    text=True, capture_output=True, check=True,
                )
                inspected = json.loads(completed.stdout)[0]
                self.assertEqual(
                    inspected["Config"]["Labels"]["org.opencontainers.image.revision"],
                    app["release"],
                )

    def test_edge_adds_one_router_and_network_preserving_all_legacy_routes(self):
        subprocess.run(
            [str(Path(sys.executable).parent / "ansible-playbook"),
             "--inventory", "ansible/inventory/local/hosts.yml",
             "ansible/playbooks/render-edge.yml"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        dynamic = yaml.safe_load((ROOT / ".build/edge/dynamic.yml").read_text())
        edge = yaml.safe_load((ROOT / ".build/edge/stack.yml").read_text())
        legacy = yaml.safe_load((ROOT / "config/platform.yml").read_text())["platform_edge_networks"]
        self.assertEqual(len(legacy), 8)
        self.assertEqual(
            set(dynamic["http"]["routers"]),
            {*legacy, "edge-health", "edge-ping-internal", "organizationweb"},
        )
        router = dynamic["http"]["routers"]["organizationweb"]
        self.assertEqual(router["rule"], "Host(`organizacion.apptolast.com`)")
        self.assertEqual(router["entryPoints"], ["websecure"])
        self.assertEqual(router["tls"]["certResolver"], "letsencrypt")
        self.assertEqual(
            dynamic["http"]["services"]["organizationweb"]["loadBalancer"]["servers"],
            [{"url": "http://organizationweb_web:8080"}],
        )
        self.assertEqual(
            set(edge["networks"]),
            {*("edge-" + name for name in legacy), "edge-monitoring", "edge-organizationweb"},
        )

    def test_operation_lock_accepts_only_the_new_versioned_playbook_identity(self):
        spec = importlib.util.spec_from_file_location(
            "operation_lock", ROOT / "scripts/ansible-operation-lock.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.require_metadata(
            "a" * 64, "b" * 40, "c" * 64,
            "organizationweb", "production", "check", "review-controller",
        )
        wrapper = (ROOT / "scripts/deploy-ansible.sh").read_text()
        self.assertIn("|organizationweb|", wrapper)
        self.assertIn('"${PROJECT_DIR}/config/capacity-profiles.yml"', wrapper)
        self.assertIn('"${PROJECT_DIR}/config/organizationweb.yml"', wrapper)
        gate = (ROOT / "scripts/validate-iac.sh").read_text()
        self.assertIn("scripts/validate-organizationweb.py", gate)
        self.assertIn("scripts/validate-capacity-profiles.py", gate)
        with self.assertRaises(module.OperationLockError):
            module.require_metadata(
                "a" * 64, "b" * 40, "c" * 64,
                "organizationweb-extra", "production", "check", "review-controller",
            )

    def test_deployment_is_locked_and_capacity_checked_before_application_role(self):
        play = yaml.safe_load((ROOT / "ansible/playbooks/organizationweb.yml").read_text())[0]
        self.assertEqual(
            [item["role"] for item in play["roles"]],
            ["operation_lock_guard", "capacity_preflight", "organizationweb", "deployment_metadata"],
        )
        role = yaml.safe_load((ROOT / "ansible/roles/organizationweb/tasks/main.yml").read_text())
        deploy = next(task for task in role if task.get("ansible.builtin.import_tasks") == "deploy.yml")
        self.assertEqual(deploy["when"], "not ansible_check_mode")
        deployed = (ROOT / "ansible/roles/organizationweb/tasks/deploy.yml").read_text()
        self.assertIn("org.opencontainers.image.revision", deployed)
        self.assertIn("organizationweb.release", deployed)
        preflight = (ROOT / "ansible/roles/organizationweb/tasks/main.yml").read_text()
        self.assertIn("organizationweb_node_ids.stdout_lines | length == 1", preflight)
        self.assertIn(".Spec.Labels['platform.workloads'] == 'true'", preflight)

    def test_catalog_rejects_a_data_root_inside_the_legacy_generation(self):
        spec = importlib.util.spec_from_file_location(
            "organizationweb_validator", ROOT / "scripts/validate-organizationweb.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        variables["organizationweb"]["data_root"] = "/srv/dockerswarm/runtime"
        with self.assertRaisesRegex(ValueError, "data_root"):
            module.validate_catalog(variables)

    def test_render_keeps_data_services_private_and_legacy_catalog_separate(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        stack = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))
        services = stack["services"]
        self.assertEqual(set(services), {"web", "backend", "postgres", "rabbitmq"})
        self.assertEqual(set(services["web"]["networks"]), {"edge", "application"})
        self.assertEqual(
            set(services["backend"]["networks"]),
            {"application", "database", "messaging"},
        )
        self.assertEqual(set(services["postgres"]["networks"]), {"database"})
        self.assertEqual(set(services["rabbitmq"]["networks"]), {"messaging"})
        self.assertEqual(stack["networks"]["edge"]["name"], "apptolast-edge-organizationweb")
        self.assertTrue(stack["networks"]["edge"]["external"])
        channel_map = load_image_channels_map()["organizationweb"]
        for name, service in services.items():
            self.assertNotIn("ports", service)
            # The rendered image is the reviewed channel entry: a channel
            # `repo:tag` or a hold `repo[:tag]@sha256:...`, never a bare repo.
            self.assertEqual(service["image"], channel_map[name]["reference"])
            self.assertRegex(
                service["image"],
                r"^[a-z0-9][a-z0-9._/-]*"
                r"(:[A-Za-z0-9_][A-Za-z0-9_.-]*)?(@sha256:[a-f0-9]{64})?$",
            )
            self.assertRegex(service["image"], r"[:@]")
            self.assertEqual(
                service["deploy"]["labels"]["apptolast.autoupdate"],
                channel_map[name]["label"],
            )
            self.assertEqual(service["deploy"]["replicas"], 1)
        self.assertEqual(
            services["backend"]["environment"]["SPRING_CONFIG_IMPORT"],
            "configtree:/run/secrets/",
        )
        self.assertEqual(
            {secret["target"] for secret in services["backend"]["secrets"]},
            {
                "DB_USERNAME", "DB_PASSWORD", "APP_AUTH_USERNAME",
                "APP_AUTH_PASSWORD", "RABBITMQ_USERNAME", "RABBITMQ_PASSWORD",
            },
        )
        self.assertNotIn("approved_services", variables)
        self.assertEqual(stack.get("configs"), {})

    def test_publisher_uses_the_application_vhost_property(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        stack = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))
        self.assertEqual(
            stack["services"]["backend"]["environment"].get("RABBITMQ_VHOST"),
            "organization",
        )

    def test_web_healthcheck_uses_the_published_unprivileged_port(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        stack = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))
        self.assertEqual(
            stack["services"]["web"]["healthcheck"]["test"][-1],
            "http://127.0.0.1:8080/healthz",
        )

    def test_backend_binds_secure_session_policy_to_the_public_origin(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        stack = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))
        self.assertEqual(
            stack["services"]["backend"]["environment"].get("APP_PUBLIC_ORIGIN"),
            "https://organizacion.apptolast.com",
        )

    def test_published_web_executes_its_actual_healthcheck_with_hardening(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        web = yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map()))["services"]["web"]
        name = "organizationweb-health-test-" + uuid.uuid4().hex
        command = [
            "docker", "run", "--detach", "--name", name,
            "--user", web["user"], "--read-only", "--cap-drop", "ALL",
            "--add-host", "backend:127.0.0.1",
        ]
        for mount in web["volumes"]:
            command.extend([
                "--mount", "type=tmpfs,destination=" + mount["target"]
                + ",tmpfs-size=" + str(mount["tmpfs"]["size"]),
            ])
        command.append(web["image"])
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            deadline = time.monotonic() + 20
            while True:
                probe = subprocess.run(
                    ["docker", "exec", name, *web["healthcheck"]["test"][1:]],
                    capture_output=True, text=True, check=False,
                )
                if probe.returncode == 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            self.assertEqual(probe.returncode, 0, probe.stderr)
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True, text=True, check=False,
            )

    def test_docker_accepts_the_stack_without_ignored_options(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        completed = subprocess.run(
            ["docker", "stack", "config", "--compose-file", "-"],
            input=template.render(**variables, image_channels_map=load_image_channels_map()),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Ignoring", completed.stderr)
        normalized = yaml.safe_load(completed.stdout)
        self.assertTrue(normalized["services"]["backend"]["read_only"])
        self.assertEqual(
            normalized["services"]["backend"]["deploy"]["resources"]["limits"]["memory"],
            "805306368",
        )


def load_channel_module():
    spec = importlib.util.spec_from_file_location(
        "validate_image_channels_orgweb", ROOT / "scripts/validate-image-channels.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OrganizationWebChannelGateTests(AnsibleTaskAssertions, unittest.TestCase):
    """The OrganizationWeb channel and live image gates, fed synthetic data."""

    MAIN = "ansible/roles/organizationweb/tasks/main.yml"
    DEPLOY = "ansible/roles/organizationweb/tasks/deploy.yml"
    LIVE = "Require every live image to match its reviewed channel entry"
    PUBLISHED = "Require the published Linux amd64 images"
    PRECONDITION = "Require every live application hold to run its reviewed identity"
    RESOLVED = "sha256:" + ("1" * 64)
    FOREIGN = "sha256:" + ("3" * 64)
    NAMES = ("backend", "postgres", "rabbitmq", "web")

    @classmethod
    def setUpClass(cls):
        module = load_channel_module()
        document = module.load_unique_yaml(ROOT / "config/image-channels.yml")
        baselines = module.load_baselines(ROOT)
        cls.catalog = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())[
            "organizationweb"
        ]

        def entry(name, reference):
            raw = next(
                item for item in document["image_channel_services"]
                if (item["stack"], item["service"]) == ("organizationweb", name)
            )
            return module.derive_entry(dict(raw, reference=reference), baselines)

        # The gates are exercised on the exact baseline holds, whatever the
        # reviewed map holds today, plus an explicit backend channel.
        cls.entries = {
            name: entry(name, cls.catalog["images"][name]) for name in cls.NAMES
        }
        cls.backend_channel = entry(
            "backend", "docker.io/ocholoko888/organizationweb-api:latest"
        )

    @staticmethod
    def inspect(image, label=""):
        return json.dumps([{"Spec": {
            "Labels": {"com.docker.stack.image": label},
            "TaskTemplate": {"ContainerSpec": {"Image": image}},
        }}])

    def test_exactly_the_four_channel_entries_are_required(self):
        task = "Require exactly the four OrganizationWeb channel entries"
        self.assert_task_accepts(
            self.MAIN, task, {"image_channels_map": {"organizationweb": self.entries}}
        )
        three = {name: self.entries[name] for name in ("backend", "postgres", "web")}
        self.assert_task_rejects(
            self.MAIN, task, {"image_channels_map": {"organizationweb": three}},
            "OrganizationWeb channel entries differ from the stack contract.",
        )

    def test_every_channel_is_bound_to_its_catalog_image(self):
        task = "Bind every OrganizationWeb channel to its catalog image"
        self.assert_task_accepts(
            self.MAIN, task, {"image_channels_map": {"organizationweb": self.entries}}
        )
        swapped = dict(self.entries)
        swapped["postgres"] = dict(
            self.entries["postgres"],
            baseline={"catalog": "organizationweb", "component": "web"},
        )
        self.assert_task_rejects(
            self.MAIN, task, {"image_channels_map": {"organizationweb": swapped}},
            "is not bound to its reviewed OrganizationWeb catalog image",
        )

    def live_image(self, entry):
        if entry["mode"] == "hold":
            return entry["spec_exact"]
        return entry["repository_familiar"] + ":" + entry["tag"] + "@" + self.RESOLVED

    def live(self, entries, **images):
        return {
            "image_channels_map": {"organizationweb": entries},
            "organizationweb_service_inspections": {"results": [
                {"item": name, "stdout": self.inspect(images.get(name, self.live_image(entries[name])))}
                for name in self.NAMES
            ]},
        }

    def test_live_gate_accepts_reviewed_holds_and_channel_digests(self):
        self.assert_task_accepts(self.DEPLOY, self.LIVE, self.live(self.entries))
        channel = dict(self.entries, backend=self.backend_channel)
        self.assert_task_accepts(self.DEPLOY, self.LIVE, self.live(channel))

    def test_live_gate_rejects_foreign_or_unanchored_images(self):
        channel = dict(self.entries, backend=self.backend_channel)
        loose = dict(
            self.entries,
            backend=dict(
                self.backend_channel, mode="hold",
                spec_exact="ocholoko888/organizationweb-api:latest@" + self.FOREIGN,
            ),
        )
        postgres = self.entries["postgres"]["spec_exact"].split("@")[0]
        cases = {
            "hold on another digest": (self.entries, {"postgres": postgres + "@" + self.FOREIGN}),
            "channel of another repository": (
                channel, {"backend": "ocholoko888/organizationweb-web:latest@" + self.RESOLVED}
            ),
            "channel of another tag": (
                channel, {"backend": "ocholoko888/organizationweb-api:main@" + self.RESOLVED}
            ),
            "hold judged by its pattern": (
                loose, {"backend": "ocholoko888/organizationweb-api:latest@" + self.RESOLVED}
            ),
        }
        for case, (entries, images) in cases.items():
            with self.subTest(case=case):
                self.assert_task_rejects(
                    self.DEPLOY, self.LIVE, self.live(entries, **images),
                    "differs from its channel entry.",
                )

    def published(self, entries, **overrides):
        results = []
        for name in self.NAMES:
            image = self.live_image(entries[name])
            labels = (
                {"org.opencontainers.image.revision": self.catalog["release"]}
                if name in ("backend", "web") else {}
            )
            inspected = {
                "Os": "linux",
                "Architecture": "amd64",
                "RepoDigests": [
                    entries[name]["repository_familiar"] + "@" + image.split("@")[1]
                ],
                "Config": {"Labels": labels},
            }
            inspected.update(overrides.get(name, {}))
            results.append({
                "item": {"item": name, "stdout": self.inspect(image)},
                "stdout": json.dumps([inspected]),
            })
        return {
            "image_channels_map": {"organizationweb": entries},
            "organizationweb": self.catalog,
            "organizationweb_image_inspections": {"results": results},
        }

    def test_published_gate_accepts_the_release_and_any_channel_revision(self):
        self.assert_task_accepts(self.DEPLOY, self.PUBLISHED, self.published(self.entries))
        channel = dict(self.entries, backend=self.backend_channel)
        self.assert_task_accepts(
            self.DEPLOY, self.PUBLISHED,
            self.published(channel, backend={"Config": {"Labels": {
                "org.opencontainers.image.revision": "b" * 40
            }}}),
        )

    def test_published_gate_rejects_platform_digest_or_revision_drift(self):
        channel = dict(self.entries, backend=self.backend_channel)
        cases = {
            "arm64": (self.entries, {"postgres": {"Architecture": "arm64"}}),
            "digest not in RepoDigests": (
                self.entries,
                {"web": {"RepoDigests": ["ocholoko888/organizationweb-web@" + self.FOREIGN]}},
            ),
            "baseline hold on another revision": (
                self.entries,
                {"backend": {"Config": {"Labels": {"org.opencontainers.image.revision": "b" * 40}}}},
            ),
            "channel without a commit revision": (
                channel,
                {"backend": {"Config": {"Labels": {"org.opencontainers.image.revision": "main"}}}},
            ),
        }
        for case, (entries, overrides) in cases.items():
            with self.subTest(case=case):
                self.assert_task_rejects(
                    self.DEPLOY, self.PUBLISHED, self.published(entries, **overrides),
                    "Image identity or architecture differs from the reviewed release.",
                )

    def precondition(self, *results):
        return {
            "image_channels_map": {"organizationweb": self.entries},
            "organizationweb_services_before_deploy": {"results": list(results)},
        }

    def before(self, name, rc, image="", label="", stderr=""):
        return {
            "item": name, "rc": rc,
            "stdout": self.inspect(image, label) if rc == 0 else "[]",
            "stderr": stderr,
        }

    def test_precondition_refuses_a_hold_moved_outside_git(self):
        postgres = self.entries["postgres"]
        moved = postgres["spec_exact"].split("@")[0] + "@" + self.FOREIGN
        message = "runs an image outside its unchanged hold entry"
        self.assert_task_accepts(self.DEPLOY, self.PRECONDITION, self.precondition(
            self.before("postgres", 0, postgres["spec_exact"], postgres["reference"]),
            self.before("postgres", 0, moved, "postgres:17.10-alpine"),
            self.before("web", 1, stderr="Error: No such service: organizationweb_web"),
        ))
        self.assert_task_rejects(self.DEPLOY, self.PRECONDITION, self.precondition(
            self.before("postgres", 0, moved, postgres["reference"]),
        ), message)
        self.assert_task_rejects(self.DEPLOY, self.PRECONDITION, self.precondition(
            self.before("postgres", 1, stderr="permission denied while trying to connect"),
        ), message)


class OrganizationWebValidatorTests(unittest.TestCase):
    """scripts/validate-organizationweb.py channel binding and render checks."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "organizationweb_validator_channels", ROOT / "scripts/validate-organizationweb.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.document = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())

    def rendered(self):
        image_channels = self.module.load_image_channels(copy.deepcopy(self.document))
        stack = yaml.safe_load(self.module.render(self.document, image_channels))
        return stack, image_channels

    def test_reviewed_render_is_accepted(self):
        stack, image_channels = self.rendered()
        self.module.validate_render(stack, image_channels)

    def test_render_drift_is_rejected(self):
        def foreign_image(stack):
            stack["services"]["backend"]["image"] = (
                "docker.io/ocholoko888/organizationweb-api:main"
            )

        def missing_web(stack):
            del stack["services"]["web"]

        def flipped_label(stack):
            stack["services"]["backend"]["deploy"]["labels"]["apptolast.autoupdate"] = "true"

        cases = {
            "rendered image backend differs from its channel entry": foreign_image,
            "rendered OrganizationWeb services changed": missing_web,
            "rendered autoupdate label backend differs from its channel entry": flipped_label,
        }
        for message, mutate in cases.items():
            with self.subTest(message=message):
                stack, image_channels = self.rendered()
                mutate(stack)
                with self.assertRaisesRegex(ValueError, message):
                    self.module.validate_render(stack, image_channels)

    def test_catalog_that_no_longer_matches_its_channels_is_rejected(self):
        document = copy.deepcopy(self.document)
        images = document["organizationweb"]["images"]
        images["postgres"], images["rabbitmq"] = images["rabbitmq"], images["postgres"]
        with self.assertRaisesRegex(ValueError, "^image channel map: "):
            self.module.load_image_channels(document)

    def test_channel_set_and_catalog_binding_are_enforced(self):
        reviewed = load_image_channels_map()["organizationweb"]

        class StubChannelError(RuntimeError):
            pass

        def stub(entries):
            return types.SimpleNamespace(
                ChannelError=StubChannelError,
                load_channel_map=lambda root, **overrides: {
                    "services": {"organizationweb": entries}
                },
            )

        missing = copy.deepcopy(reviewed)
        del missing["web"]
        rebound = copy.deepcopy(reviewed)
        rebound["backend"]["baseline"] = {"catalog": "organizationweb", "component": "web"}
        cases = {
            "OrganizationWeb channel entries differ from the stack": missing,
            "channel backend is not bound to its catalog image": rebound,
        }
        for message, entries in cases.items():
            with self.subTest(message=message):
                with mock.patch.object(
                    self.module, "load_channel_module", return_value=stub(entries)
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        self.module.load_image_channels(copy.deepcopy(self.document))


if __name__ == "__main__":
    unittest.main()
