import os
import subprocess
import sys
from pathlib import Path

SKILL_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "digipos-topup-monitoring"
    / "scripts"
    / "download_topup.py"
)


def run_extract(start, end, users=None, output_dir="finpay-topup-inbox", password=None):
    if not SKILL_SCRIPT.exists():
        raise FileNotFoundError(f"DigiPOS skill script not found: {SKILL_SCRIPT}")
    cmd = [sys.executable, str(SKILL_SCRIPT), "--start", start, "--end", end, "--output-dir", output_dir]
    if users:
        cmd += ["--users", *users]
    child_env = None
    if password:
        child_env = os.environ.copy()
        child_env["DIGIPOS_PASSWORD"] = password
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=child_env)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "command": " ".join(cmd),
    }
