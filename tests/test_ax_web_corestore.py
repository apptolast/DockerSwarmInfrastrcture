#!/usr/bin/env python3
"""Runs the Node smoke test of the API client and SSE store (tests/corestore_smoke.js).

js/00-core.js (API client, semaphore, error messages) and js/10-store.js (SSE
reconnection with backoff) have no browser-free test runner today, so the
smoke test loads both in a vm with a fake EventSource, a fake fetch and
recorded timers. It catches a dropped error message, a stale-source race
between reconnects or a wrong backoff delay, without a browser.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests/corestore_smoke.js"


class CoreStoreSmoke(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_api_client_and_store_run_with_a_fake_stream(self) -> None:
        done = subprocess.run(
            ["node", str(SMOKE)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=ROOT,
        )
        # A failure prints the assertion message and a short stack: keep only
        # the short lines, where the message and the stack are.
        short = "\n".join(
            line
            for line in (done.stdout + done.stderr).splitlines()
            if 0 < len(line) < 300
        )
        self.assertEqual(done.returncode, 0, short[-2500:])
        self.assertIn("corestore smoke: ok", done.stdout)

    def test_the_smoke_test_exists_and_covers_the_key_cases(self) -> None:
        text = SMOKE.read_text(encoding="utf-8")
        # Structural needles, not just words from assertion messages: the
        # exact backoff sequence with its cap, the exact-once reconnect and
        # the check that a stale stream leaves the state alone.
        for needle in (
            "safeLink",
            "readyState",
            "ApiError",
            "[5000, 10000, 20000, 40000, 60000, 60000]",
            "esInstances.length, before + 1",
            "S.snap.rev, 1",
        ):
            self.assertIn(needle, text, needle)


if __name__ == "__main__":
    unittest.main()
