"""The only place an LLM client is created. Every call goes to the AMD API.

Reads AMD_BASE_URL, AMD_API_KEY and AMD_MODEL from the environment, falling back
to the gitignored .env at the repo root. There is no OpenAI default or fallback.
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
REQUIRED = ("AMD_BASE_URL", "AMD_API_KEY", "AMD_MODEL")


def load_env(path=ENV_PATH):
    """Fill missing variables from KEY=VALUE lines; real environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def amd_model():
    load_env()
    return os.environ.get("AMD_MODEL")


def make_client(timeout=90.0, max_retries=1):
    load_env()
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} (in the environment or {ENV_PATH}).")
    from openai import OpenAI

    return OpenAI(
        base_url=os.environ["AMD_BASE_URL"],
        api_key=os.environ["AMD_API_KEY"],
        timeout=timeout,
        max_retries=max_retries,
    )
