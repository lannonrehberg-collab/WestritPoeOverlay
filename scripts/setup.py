#!/usr/bin/env python3
"""Cross-platform setup for aldur-appraiser.

Pure standard-library so it runs anywhere Python 3.11+ does (Linux, Windows,
macOS). It creates a virtualenv, installs the package with the vision extra,
then checks the platform-specific capture prerequisites and prints guidance.

    python scripts/setup.py            # full setup
    python scripts/setup.py --check    # only run the environment checks

The only thing it can't install for you is the Linux/Wayland capture stack
(GStreamer + PyGObject), because those are distro packages, not pip wheels —
it detects them and tells you the exact package names instead.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
MIN_PY = (3, 11)

GREEN, YELLOW, RED, BOLD, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
if os.name == "nt" or not sys.stdout.isatty():
    GREEN = YELLOW = RED = BOLD = OFF = ""


def say(msg: str) -> None:
    print(f"{BOLD}==>{OFF} {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{OFF}   {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{OFF} {msg}")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd: list[str]) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd)


# --- steps -------------------------------------------------------------------


def check_python() -> None:
    if sys.version_info < MIN_PY:
        found = sys.version.split()[0]
        sys.exit(f"{RED}Python {MIN_PY[0]}.{MIN_PY[1]}+ required, found {found}{OFF}")
    ok(f"Python {sys.version.split()[0]}")


def create_venv() -> None:
    if venv_python().exists():
        ok(f"venv exists at {VENV}")
        return
    say(f"Creating virtualenv at {VENV}")
    run([sys.executable, "-m", "venv", str(VENV)])


def install_package() -> None:
    say("Installing aldur-appraiser with the vision + overlay extras")
    py = str(venv_python())
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-e", ".[vision,overlay]"])


def _system_has_gstreamer() -> bool:
    """Can a Python import GStreamer/PyGObject (checked via this interpreter)?"""
    code = "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst"
    for exe in (sys.executable, shutil.which("python3"), "/usr/bin/python3"):
        if not exe:
            continue
        if subprocess.run([exe, "-c", code], capture_output=True).returncode == 0:
            return True
    return False


def _distro_hint() -> str:
    pkgs_dnf = "gstreamer1-plugin-pipewire python3-gobject gstreamer1-plugins-good"
    if Path("/run/ostree-booted").exists():  # Bazzite / atomic
        return (
            "Atomic/immutable distro detected. Either layer the packages:\n"
            f"      rpm-ostree install {pkgs_dnf}   (then reboot)\n"
            "    or run the tool inside a distrobox container that has them."
        )
    if shutil.which("dnf"):
        return f"sudo dnf install {pkgs_dnf}"
    if shutil.which("apt"):
        return "sudo apt install gstreamer1.0-pipewire python3-gi gstreamer1.0-plugins-good"
    if shutil.which("pacman"):
        return "sudo pacman -S gst-plugin-pipewire python-gobject gst-plugins-good"
    return f"install (via your package manager): {pkgs_dnf}"


def check_capture_prereqs() -> None:
    say("Checking screen-capture prerequisites")
    plat = sys.platform
    if plat == "win32":
        ok("Windows: capture uses mss (bundled wheel) — nothing else needed.")
        return
    if plat == "darwin":
        ok("macOS: capture uses mss (bundled wheel). Grant Screen Recording "
           "permission to your terminal on first run.")
        return
    # Linux
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if not wayland:
        ok("Linux/X11: capture uses mss (bundled wheel).")
        return
    print("  Linux/Wayland: mss can't read the screen here; the portal+PipeWire "
          "backend is used.")
    if _system_has_gstreamer():
        ok("GStreamer + PyGObject available (portal backend ready).")
    else:
        warn("GStreamer/PyGObject not found. Install the system packages:\n      "
             + _distro_hint())


def check_tesseract() -> None:
    if shutil.which("tesseract"):
        ok("tesseract found (optional OCR fallback available).")
    else:
        print("  tesseract not found — fine; RapidOCR is the primary engine. "
              "Install tesseract only if you want the fallback.")


def next_steps() -> None:
    activate = (
        ".venv\\Scripts\\activate" if os.name == "nt" else "source .venv/bin/activate"
    )
    print()
    say("Done. Next steps:")
    print(f"  1. {activate}")
    print("  2. appraiser table --top 10        # verify pricing works")
    print("  3. appraiser capture-test          # verify screen capture")
    print("  4. appraiser run                   # live loop (open a Remnant in-game)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up aldur-appraiser.")
    parser.add_argument("--check", action="store_true", help="only run environment checks")
    args = parser.parse_args()

    check_python()
    if not args.check:
        create_venv()
        install_package()
    check_capture_prereqs()
    check_tesseract()
    if not args.check:
        next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
