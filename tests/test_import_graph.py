"""``ormas_subnet`` must never import anything from ``tensorbox_spec``.

That's the whole point of extracting it: a miner installs this package alone,
with no monorepo attached. Runs in a subprocess so the parent test process's own
``tensorbox_spec`` imports (e.g. from ``test_protocol_parity.py`` in the same
session) can't contaminate the check.
"""
from __future__ import annotations

import subprocess
import sys

_CHECK = """
import sys
import ormas_subnet  # noqa: F401
import ormas_subnet.client
import ormas_subnet.protocol
import ormas_subnet.skeleton
import ormas_subnet.reference_solver

leaked = sorted(name for name in sys.modules if name == "tensorbox_spec" or name.startswith("tensorbox_spec."))
print("LEAKED:" + ",".join(leaked))
"""


def test_no_tensorbox_spec_import_in_subprocess() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.startswith("LEAKED:")]
    assert lines, proc.stdout
    leaked = lines[-1].split("LEAKED:", 1)[1]
    assert leaked == "", f"ormas_subnet imported tensorbox_spec modules: {leaked}"
