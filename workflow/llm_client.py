"""The only place an LLM client is created. Any OpenAI-compatible endpoint works.

Reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from the environment, falling back
to the gitignored .env at the repo root. Optional LLM_EXTRA_BODY (JSON) is sent with
the fast, no-reasoning calls; for Qwen on vLLM use
{"chat_template_kwargs": {"enable_thinking": false}}.
"""

import json
import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
REQUIRED = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


def load_env(path=ENV_PATH):
    """Fill missing variables from KEY=VALUE lines; real environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def llm_model():
    load_env()
    return os.environ.get("LLM_MODEL")


def fast_extra_body():
    """Provider-specific body for calls that should skip reasoning (empty when unset)."""
    load_env()
    raw = os.environ.get("LLM_EXTRA_BODY")
    return json.loads(raw) if raw else {}


def make_client(timeout=90.0, max_retries=3):
    load_env()
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} (in the environment or {ENV_PATH}).")
    from openai import OpenAI

    class Client(OpenAI):
        def _should_retry(self, response):
            # Meta's API (2026-10-26) sometimes answers 404 model_not_found for a model it serves;
            # the same request succeeds on retry. The SDK never retries a 404 on its own.
            if response.status_code == 404:
                try:
                    return b"model_not_found" in response.read()
                except Exception:
                    return False
            return super()._should_retry(response)

    return Client(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        timeout=timeout,
        max_retries=max_retries,
    )
