"""Static contract of the AX web panel source (images/ax-web).

The Go tests in images/ax-web check the behaviour; these checks pin what a
reviewer must see change on purpose: the only commands the panel starts,
that the agent credential never enters a task spec or the browser, the
security headers, and the pinned build inputs.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "images/ax-web"
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
AGENT_COMMAND = (
    "[]string{AgentWrapper, AgentClaude, "
    '"-p", "--restricted", "--strict-mcp-config",\n'
    '\t\t"--output-format", "stream-json", "--verbose", '
    '"--max-turns", strconv.Itoa(turns)}'
)
CLONE_CHECK = (
    '[]string{"git", "-C", WorkspacePath + "/" + RepoDir, '
    '"rev-parse", "--verify", "HEAD"}'
)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


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


class CommandContract(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = go_sources()
        self.manager = self.sources["internal/runs/manager.go"]

    def test_no_shell_and_no_local_processes(self) -> None:
        for name, source in self.sources.items():
            code = strip_comments(source)
            with self.subTest(name=name):
                self.assertNotIn('"os/exec"', code)
                self.assertNotIn("syscall.Exec", code)
                self.assertNotIn('"sh"', code)
                self.assertNotIn('"/bin/sh"', code)
                self.assertNotIn('"-c"', code)

    def test_only_two_fixed_commands_reach_the_sandbox(self) -> None:
        starts = [
            (name, match.start())
            for name, source in self.sources.items()
            for match in re.finditer(r"StartProcessRequest\{", source)
        ]
        self.assertEqual([name for name, _ in starts], ["internal/runs/manager.go"] * 2)
        self.assertIn("return " + AGENT_COMMAND, self.manager)
        self.assertIn("return " + CLONE_CHECK, self.manager)
        self.assertEqual(self.manager.count("Command: command,"), 1)
        self.assertEqual(self.manager.count("Command: argv,"), 1)
        self.assertEqual(self.manager.count("m.check(ctx, proc, CloneCheck())"), 1)
        # The prompt fallback appends after "--" and nothing else.
        self.assertIn('command = append(command, "--", spec.Prompt)', self.manager)

    def test_the_credential_travels_only_in_start_process(self) -> None:
        env_lines = [
            line.strip()
            for source in self.sources.values()
            for line in source.splitlines()
            if re.search(r"\bEnv:\s", line)
        ]
        self.assertEqual(
            env_lines,
            [
                # The only environment the panel ever sets.
                "Env:     map[string]string{TokenEnv: token},",
                # The browser's view of a task: names only.
                "Env:        []EnvView{},",
            ],
        )
        task_spec = block(self.manager, "Spec: &v1alpha1.TaskSpec{")
        self.assertNotIn("Env", task_spec)
        self.assertIn('TokenEnv        = "CLAUDE_CODE_OAUTH_TOKEN"', self.manager)
        # The clone check starts before the credential is read.
        self.assertLess(
            self.manager.index("m.check(ctx, proc, CloneCheck())"),
            self.manager.index("token, err := m.dep.ReadToken()"),
        )
        for name, source in self.sources.items():
            if name != "main.go":
                self.assertNotIn("TokenReader(cfg", source, name)

    def test_env_values_never_reach_the_browser(self) -> None:
        for name, source in self.sources.items():
            if name.startswith("internal/web/"):
                self.assertNotIn(".GetValue()", source, name)
                self.assertNotIn("protojson", source, name)
        views = self.sources["internal/web/views.go"]
        self.assertIn(
            "EnvView{Name: e.GetName(), Value: Hidden}",
            views,
        )
        self.assertIn('const Hidden = "[oculto]"', views)

    def test_every_json_response_is_a_view(self) -> None:
        server = self.sources["internal/web/server.go"]
        payloads = sorted(
            # A literal that spans lines is captured as its first line: the
            # status and the pending-delete answers, built from strings.
            set(re.findall(r"writeJSON\(w, [^,]+, (.*?)\)?\n", server))
        )
        self.assertEqual(
            payloads,
            sorted(
                {
                    "map[string]any{",
                    'map[string]any{"cancelling": true}',
                    'map[string]any{"deleted": true}',
                    'map[string]any{"gateways": out}',
                    'map[string]any{"run": nil}',
                    'map[string]any{"run": run.View()}',
                    'map[string]any{"workspaces": out}',
                    'map[string]string{"error": fe.Message, "field": fe.Field}',
                    'map[string]string{"error": msg}',
                    "body",
                    "run.View()",
                    "s.taskView(t)",
                }
            ),
        )

    def test_security_headers(self) -> None:
        server = self.sources["internal/web/server.go"]
        parts = re.search(r'\tCSP = "([^"]*)" \+\n\t\t"([^"]*)"\n', server)
        self.assertIsNotNone(parts)
        self.assertEqual(parts.group(1) + parts.group(2), CSP)
        self.assertIn('h.Set("Content-Security-Policy", CSP)', server)
        self.assertNotIn("Access-Control-Allow", server)
        for header in (
            '"X-Content-Type-Options", "nosniff"',
            '"Cache-Control", "no-store"',
            '"X-Frame-Options", "DENY"',
        ):
            self.assertIn(header, server)
        # State-changing requests: same origin, JSON and the panel header.
        self.assertIn('CSRFHeader = "X-AX-Web"', server)
        self.assertIn('site != "" && site != "same-origin"', server)
        self.assertIn('mt != "application/json"', server)

    def test_mutual_tls_is_mandatory(self) -> None:
        tls = self.sources["internal/web/tls.go"]
        self.assertIn("MinVersion:   tls.VersionTLS13", tls)
        self.assertIn("ClientAuth:   tls.RequireAndVerifyClientCert", tls)
        self.assertIn("x509.ExtKeyUsageClientAuth", tls)
        main = self.sources["main.go"]
        self.assertIn('srv.ListenAndServeTLS("", "")', main)
        self.assertEqual(main.count("ListenAndServe()"), 1)
        self.assertIn("health.ListenAndServe()", main)


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
