"""Build the Windows installer (Semishigure-<version>-setup.exe).

    python tools/build_windows.py            # needs: pip install pynsist, and NSIS (makensis) on PATH

Works on Windows and on Linux (cross-build): it downloads the win_amd64 wheels of
every runtime dependency for the embedded Python, builds the semishigure wheel and
then runs pynsist with installer.cfg.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHEELS = ROOT / "build" / "wheels"
PY = "3.12"
# runtime dependencies (see pyproject.toml); the application window is Edge/Chrome in
# app mode, so nothing GUI-related is bundled
BINARY = ["pyyaml", "cryptography", "fastapi", "uvicorn", "websockets", "asyncssh", "openpyxl", "typing_extensions"]
SDIST_ONLY: list[str] = []


def run(*cmd: str) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> int:
    if shutil.which("makensis") is None:
        # standard install locations on Windows (winget/choco/installer) when not on PATH
        for d in (r"C:\Program Files (x86)\NSIS", r"C:\Program Files\NSIS"):
            if os.path.isfile(os.path.join(d, "makensis.exe")):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                break
        else:
            print("error: NSIS (makensis) is not on PATH (Windows: winget install NSIS.NSIS / choco install nsis; Debian: apt install nsis)", file=sys.stderr)
            return 2
    shutil.rmtree(WHEELS, ignore_errors=True)
    WHEELS.mkdir(parents=True)
    pip = [sys.executable, "-m", "pip"]
    # pip resolves the dependency tree for the target platform (win_amd64 / cp312)
    run(*pip, "download", "--only-binary=:all:", "--platform", "win_amd64", "--python-version", PY, "--implementation", "cp", "--abi", "cp312", "-d", str(WHEELS), *BINARY)
    for name in SDIST_ONLY:
        run(*pip, "wheel", "--no-deps", "-w", str(WHEELS), name)
    run(*pip, "wheel", "--no-deps", "-w", str(WHEELS), str(ROOT))
    for w in glob.glob(str(WHEELS / "*.whl")):
        n = os.path.basename(w)
        if not (n.endswith("-none-any.whl") or "win_amd64" in n or "win32.win_amd64" in n):
            print(f"error: {n} is not a Windows wheel", file=sys.stderr)
            return 2
    run(sys.executable, "-m", "nsist", "installer.cfg")
    out = sorted(glob.glob(str(ROOT / "build" / "nsis" / "*.exe")))
    print("installer:", out[-1] if out else "(not found)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
