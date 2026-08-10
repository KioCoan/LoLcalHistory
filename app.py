"""Entry point for the packaged desktop application.

Kept as a plain top-level script because PyInstaller needs a concrete file to
start from; all the work lives in `lolhist.desktop`.
"""

from __future__ import annotations

import multiprocessing
import sys

from lolhist.desktop import main

if __name__ == "__main__":
    # Required before anything spawns a process in a frozen build, or the child
    # re-runs the whole app instead of the worker.
    multiprocessing.freeze_support()
    sys.exit(main())
