from pathlib import Path
import subprocess
import sys


def test_main_file_can_run_directly():
    main_file = Path(__file__).resolve().parent.parent / "opportunity_agent" / "main.py"
    result = subprocess.run(
        [sys.executable, str(main_file), "--demo"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Replanning completed" in result.stdout
