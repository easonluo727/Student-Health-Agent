"""Compatibility entry point for the AMD-hosted Branch A agent.

The maintained implementation lives in ``agent.py``. Keeping this small
launcher preserves the historical ``python agent_amd.py`` command without
duplicating the agent loop, model configuration, or safety guards.
"""

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("agent.py")), run_name="__main__")
