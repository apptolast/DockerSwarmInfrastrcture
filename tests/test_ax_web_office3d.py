#!/usr/bin/env python3
"""Runs the Node smoke test of the Oficina 3D scene (tests/office3d_smoke.js).

The scene needs a browser with WebGL, which CI does not have, so the test loads
the real trimmed Three.js and the real scene in a vm with a fake DOM and a fake
renderer. It catches a wrong API name, a bad order of declarations, a leaked
listener or a frame drawn after dispose, without a GPU.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests/office3d_smoke.js"


class Office3DSmoke(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_scene_runs_with_a_fake_renderer(self) -> None:
        done = subprocess.run(
            ["node", str(SMOKE)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=ROOT,
        )
        # A failure prints the minified source around the error: keep only
        # the short lines, where the message and the stack are.
        short = "\n".join(
            line
            for line in (done.stdout + done.stderr).splitlines()
            if 0 < len(line) < 300
        )
        self.assertEqual(done.returncode, 0, short[-2500:])
        self.assertIn("office3d smoke: ok", done.stdout)

    def test_the_smoke_test_exists_and_covers_dispose_and_no_webgl(self) -> None:
        text = SMOKE.read_text(encoding="utf-8")
        for needle in ("dispose", "setPaused", "webglcontextlost", "returns null", "reduceMotion"):
            self.assertIn(needle, text.replace("create must return null", "returns null"), needle)


if __name__ == "__main__":
    unittest.main()
