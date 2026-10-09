from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_failure_and_sse_watchdog():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the frontend unit harness")
    result = subprocess.run([node, str(Path(__file__).with_name("router_frontend_check.cjs"))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "silent-SSE recovery passed" in result.stdout
