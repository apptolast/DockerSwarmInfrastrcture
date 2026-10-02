"""Static contract of the AX web panel source (images/ax-web).

The Go tests in images/ax-web check the behaviour; these checks pin what a
reviewer must see change on purpose: no shell and no local process, the
only place a command reaches the sandbox from, that the credentials stay in
the office and the run manager and never enter a task spec or the browser,
the security headers, a static UI the CSP admits, and the pinned build
inputs.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "images/ax-web"
STATIC = APP / "internal/web/static"
WORKFLOW = ROOT / ".github/workflows/ax-web.yml"
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
# docker.io/library/golang:1.27.1, the AX lab's toolbox.
TOOLBOX = "docker.io/library/golang:1.27.1@sha256:" + (
    "3680233e3204827fbdc66088528ae6d4b3d034f51d03a99d454f6de034888244"
)
# Substrate's own base at 672533541dbf (.ko.yaml, line 15).
BASE = "gcr.io/distroless/static-debian13:latest@sha256:" + (
    "f2ea2709ac8db56323cbd7d014277f32cb572d9ea124b0076f7aafe5980678fe"
)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# The environment that carries an agent credential into StartProcess:
# Claude's token and Codex's session.
CREDENTIAL_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CODEX_AUTH_JSON_B64")


def go_sources(tests: bool = False) -> dict[str, str]:
    return {
        str(path.relative_to(APP)): path.read_text(encoding="utf-8")
        for path in sorted(APP.rglob("*.go"))
        if path.name.endswith("_test.go") == tests
    }


def block(source: str, opener: str) -> str:
    """The brace-balanced text that starts at the first `opener`."""
    start = source.index(opener)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced {opener}")


def strip_comments(source: str) -> str:
    return re.sub(r"//[^\n]*", "", source)


def under(name: str, *packages: str) -> bool:
    return any(name.startswith(f"internal/{package}/") for package in packages)


class CommandContract(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = go_sources()
        self.code = {name: strip_comments(text) for name, text in self.sources.items()}

    def test_no_shell_and_no_local_processes(self) -> None:
        for name, code in self.code.items():
            with self.subTest(name=name):
                self.assertNotIn('"os/exec"', code)
                self.assertNotIn("syscall.Exec", code)
                self.assertNotIn("syscall.ForkExec", code)
                for shell in ('"sh"', '"/bin/sh"', '"bash"', '"/bin/bash"'):
                    self.assertNotIn(shell, code)
                # "-c" is a flag of codex (-c key=value) and of git (-c
                # core.hooksPath), never a shell's: only where those argv
                # are built.
                if '"-c"' in code:
                    self.assertTrue(under(name, "harness", "runs", "fakeax"), name)

    def test_commands_reach_the_sandbox_only_from_the_run_manager(self) -> None:
        starts = [
            name
            for name, code in self.code.items()
            if "StartProcessRequest{" in code and not under(name, "fakeax")
        ]
        self.assertTrue(starts)
        for name in starts:
            self.assertTrue(under(name, "runs"), name)
        # The agent's argv comes only from harness.Command, built from a
        # validated Spec; the prompt goes on stdin, never in argv.
        callers = sorted(
            name
            for name, code in self.code.items()
            if re.search(r"\bharness\.Command\(", code)
        )
        self.assertTrue(callers)
        self.assertTrue(all(under(name, "runs") for name in callers), callers)
        harness = "\n".join(
            code for name, code in self.code.items() if under(name, "harness")
        )
        for literal in (
            '"ax-agent"',
            '"--strict-mcp-config"',
            '"--max-turns"',
            '"--restricted"',
            '"stream-json"',
            '"--json"',
            '"--ephemeral"',
        ):
            self.assertIn(literal, harness)
        for name, code in self.code.items():
            with self.subTest(name=name):
                self.assertNotIn("dangerously-skip-permissions", code)
        # Every git command runs inside the ephemeral checkout.
        runs = "\n".join(
            code for name, code in self.code.items() if under(name, "runs")
        )
        self.assertEqual(
            len(re.findall(r'\[\]string\{"git",', runs)),
            len(re.findall(r'\[\]string\{"git", "-C", ', runs)),
        )
        self.assertNotIn('"--upload-pack"', runs)
        self.assertNotIn("core.sshCommand", runs)

    def test_credentials_travel_only_in_start_process(self) -> None:
        # internal/fakeax and internal/demo are test doubles: the demo's
        # simulated executor reads the fake credential env it is handed, and
        # its fake AX serves sample tasks to the legacy views.
        for name, code in self.code.items():
            if under(name, "fakeax", "demo"):
                continue
            for env in CREDENTIAL_ENV:
                if f'"{env}"' in code:
                    with self.subTest(name=name, env=env):
                        self.assertTrue(under(name, "office", "runs"), name)
        # Environment is set only on StartProcess and in the browser's
        # names-only view of a task.
        env_lines = sorted(
            name
            for name, code in self.code.items()
            for line in code.splitlines()
            if re.search(r"\bEnv:\s", line) and not under(name, "fakeax", "demo")
        )
        self.assertTrue(env_lines)
        for name in env_lines:
            self.assertTrue(
                under(name, "runs") or name == "internal/web/views.go", name
            )
        tasks = [
            (name, block(code, "Spec: &v1alpha1.TaskSpec{"))
            for name, code in self.code.items()
            if "Spec: &v1alpha1.TaskSpec{" in code and not under(name, "fakeax", "demo")
        ]
        self.assertTrue(tasks)
        for name, spec in tasks:
            with self.subTest(name=name):
                self.assertTrue(under(name, "runs"), name)
                self.assertNotIn("Env", spec)
        # The GitHub token goes to api.github.com only, from the office.
        for name, code in self.code.items():
            if "Authorization" in code and not under(name, "fakeax", "demo", "web"):
                with self.subTest(name=name):
                    self.assertTrue(under(name, "office"), name)

    def test_env_values_never_reach_the_browser(self) -> None:
        for name, code in self.code.items():
            if name.startswith("internal/web/"):
                self.assertNotIn(".GetValue()", code, name)
                self.assertNotIn("protojson", code, name)
                for env in CREDENTIAL_ENV:
                    self.assertNotIn(env, code, name)
        views = self.sources["internal/web/views.go"]
        self.assertIn("EnvView{Name: e.GetName(), Value: Hidden}", views)
        self.assertIn('const Hidden = "[oculto]"', views)

    def test_security_headers(self) -> None:
        web = "\n".join(
            code for name, code in self.code.items() if name.startswith("internal/web/")
        )
        match = re.search(r'\bCSP\s*=\s*((?:"[^"]*"\s*\+?\s*)+)', web)
        self.assertIsNotNone(match)
        self.assertEqual("".join(re.findall(r'"([^"]*)"', match.group(1))), CSP)
        self.assertIn('h.Set("Content-Security-Policy", CSP)', web)
        self.assertNotIn("Access-Control-Allow", web)
        for header in (
            '"X-Content-Type-Options", "nosniff"',
            '"Cache-Control", "no-store"',
            '"X-Frame-Options", "DENY"',
        ):
            self.assertIn(header, web)
        # State-changing requests: same origin (or a reviewed extra one),
        # JSON and the panel header.
        self.assertIn('CSRFHeader = "X-AX-Web"', web)
        self.assertIn('site != "" && site != "same-origin"', web)
        self.assertIn('mt != "application/json"', web)

    def test_mutual_tls_is_mandatory(self) -> None:
        tls = self.sources["internal/web/tls.go"]
        self.assertIn("MinVersion:   tls.VersionTLS13", tls)
        self.assertIn("ClientAuth:   tls.RequireAndVerifyClientCert", tls)
        self.assertIn("x509.ExtKeyUsageClientAuth", tls)
        main = self.sources["main.go"]
        self.assertIn('srv.ListenAndServeTLS("", "")', main)
        self.assertEqual(main.count("ListenAndServe()"), 1)
        self.assertIn("health.ListenAndServe()", main)


class StaticUIContract(unittest.TestCase):
    """What the CSP admits: no inline code, nothing from another origin."""

    def test_the_page_loads_only_its_own_hashed_assets(self) -> None:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        for token in ("{{APP_CSS}}", "{{APP_JS}}"):
            self.assertEqual(page.count(token), 1, token)
        self.assertNotRegex(page, r"(?is)<script\b[^>]*>\s*[^<\s]")
        self.assertNotRegex(page, r"(?i)<style\b")
        self.assertNotRegex(page, r"(?i)\sstyle\s*=")
        self.assertNotRegex(page, r"(?i)\son[a-z]+\s*=")
        self.assertNotRegex(page, r'(?i)(?:src|href)\s*=\s*"(?:https?:)?//')
        self.assertFalse((STATIC / "app.js").exists())
        self.assertFalse((STATIC / "app.css").exists())
        self.assertTrue(sorted((STATIC / "js").glob("*.js")))
        self.assertTrue(sorted((STATIC / "css").glob("*.css")))
        self.assertTrue((STATIC / "favicon.svg").is_file())

    def test_scripts_and_styles_load_nothing_from_elsewhere(self) -> None:
        for path in sorted([*STATIC.rglob("*.js"), *STATIC.rglob("*.css")]):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(STATIC))):
                self.assertNotRegex(text, r"@import\s")
                self.assertNotRegex(text, r"(?i)url\(\s*['\"]?(?:https?:)?//")
                self.assertNotRegex(text, r"\b(?:importScripts|eval)\s*\(")
                self.assertNotRegex(text, r"\bnew\s+Function\s*\(")
                self.assertNotRegex(text, r"\bdocument\.write\s*\(")


class BuildContract(unittest.TestCase):
    def test_ko_configuration(self) -> None:
        ko = yaml.safe_load((APP / ".ko.yaml").read_text(encoding="utf-8"))
        self.assertEqual(
            ko,
            {
                "defaultBaseImage": BASE,
                "defaultPlatforms": ["linux/amd64"],
                "builds": [
                    {
                        "id": "ax-web",
                        "main": ".",
                        "env": ["CGO_ENABLED=0"],
                        "flags": ["-trimpath", "-buildvcs=false"],
                        "ldflags": ["-s", "-w", "-buildid="],
                    }
                ],
            },
        )

    def test_module_pins_the_lab_sources(self) -> None:
        lab = yaml.safe_load((ROOT / "config/ax-lab.yml").read_text(encoding="utf-8"))
        commit = lab["ax_lab"]["sources"]["ax"]["commit"]
        gomod = (APP / "go.mod").read_text(encoding="utf-8")
        self.assertIn("\ngo 1.27.1\n", gomod)
        self.assertRegex(
            gomod,
            r"\n\tgithub\.com/google/ax v0\.3\.1-0\.\d{14}-" + commit[:12] + r"\n",
        )
        self.assertIn(
            "\n\tgithub.com/agent-substrate/env "
            "v0.0.11-0.20260912052224-4468a200b170\n",
            gomod,
        )
        self.assertNotIn("replace", gomod)
        self.assertTrue((APP / "go.sum").is_file())

    def test_workflow(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertNotIn("secrets.", text)
        self.assertEqual(
            workflow["env"],
            {
                "GOTOOLCHAIN": "local",
                "KO_VERSION": "v0.19.1",
                "GOVULNCHECK_VERSION": "v1.8.0",
            },
        )
        uses = []
        for job in workflow["jobs"].values():
            self.assertEqual(job["container"], {"image": TOOLBOX})
            self.assertNotIn("permissions", job)
            for step in job["steps"]:
                if "uses" in step:
                    uses.append(step["uses"])
                    self.assertRegex(step["uses"], SHA_PIN)
                    if step["uses"].startswith("actions/checkout@"):
                        self.assertIs(step["with"]["persist-credentials"], False)
        self.assertEqual(len(uses), 3)
        runs = "\n".join(
            step.get("run", "")
            for job in workflow["jobs"].values()
            for step in job["steps"]
        )
        for command in (
            "go mod verify",
            "go vet ./...",
            "go test -race -count=1 ./...",
            "govulncheck@${GOVULNCHECK_VERSION}",
            'go install "github.com/google/ko@${KO_VERSION}"',
            "--push=false",
            '[ "${first}" != "${second}" ]',
        ):
            self.assertIn(command, runs)
        self.assertNotIn("docker push", runs)
        # Both builds run as the distroless nonroot uid, never as root.
        self.assertEqual(runs.count("ko build "), 2)
        self.assertEqual(runs.count("--image-user=65532 "), 2)
        for event in ("pull_request", "push"):
            self.assertEqual(
                workflow["on"][event]["paths"],
                [
                    "images/ax-web/**",
                    "config/ax-lab.yml",
                    ".github/workflows/ax-web.yml",
                ],
            )

    def test_sensitive_path_guard_covers_the_panel(self) -> None:
        guard = (ROOT / ".github/workflows/guard-sensitive-paths.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\n          ^images/ax-web/\n", guard)

    def test_no_credential_material_in_the_tree(self) -> None:
        for path in sorted(APP.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotRegex(text, r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
                self.assertNotRegex(text, r"\$2[aby]\$\d\d\$|\$apr1\$")
                self.assertNotRegex(text, r"sk-ant-[A-Za-z0-9_-]{8,}")


if __name__ == "__main__":
    unittest.main()
