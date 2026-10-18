"""Entry point for the frozen Windows build (PyInstaller targets this)."""

import sys

from aldur_appraiser.cli import main

if __name__ == "__main__":
    sys.exit(main())
