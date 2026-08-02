import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frozen_depth_forward_wrappers_run_directly_from_outside_repo(tmp_path):
    scripts = (
        "assess_frozen_stop_avoidance_depth_forward.py",
        "assess_frozen_runner_qualified_stop_depth_forward.py",
    )
    for name in scripts:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / name), "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "ModuleNotFoundError" not in result.stderr
