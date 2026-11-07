"""Start a worker from a file path, for a host that runs the interpreter in isolated mode.

``python -I`` ignores ``PYTHONPATH`` and does not put the script's folder on
``sys.path``, so a package cannot be found by name. This file adds the folder
that holds the package, then hands over to the runner. It imports nothing
else first.
"""

import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bazarr_plugin_sdk.runner import run_process

    run_process()
