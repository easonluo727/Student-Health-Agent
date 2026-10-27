"""The only place an LLM client is created. Any OpenAI-compatible endpoint works.

Reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from the environment, falling back
to the gitignored .env at the repo root. Optional LLM_FALLBACK_MODELS (comma-separated) are tried
in order when the main model answers 404 model_not_found. Optional LLM_EXTRA_BODY (JSON) is sent with
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


def fallback_models():
    """LLM_FALLBACK_MODELS: comma-separated models to try, in order, when the main one is unavailable."""
    load_env()
    return [m.strip() for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]


def model_unavailable(error) -> bool:
    """Meta's 404 for a model it serves: code model_not_found, or (seen from Railway, 2026-10-27) only the message
    "The requested model was not found." with no code."""
    text = f"{error} {getattr(error, 'body', '')}".lower()
    return "model_not_found" in text or "model was not found" in text


def make_client(timeout=90.0, max_retries=3):
    load_env()
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} (in the environment or {ENV_PATH}).")
    from openai import NotFoundError, OpenAI

    fallbacks = fallback_models()

    class Client(OpenAI):
        # Meta's API (2026-10-26) answers 404 model_not_found for a model it serves while that model's
        # servers are unreachable, sometimes for minutes; other models keep working meanwhile. So on that
        # 404 the same request goes to the next model in LLM_FALLBACK_MODELS, and the whole list is tried
        # twice. The SDK's own retries (5xx, 429) still apply to each attempt.
        def request(self, cast_to, options, **kwargs):
            body = options.json_data
            if not isinstance(body, dict) or "model" not in body:
                return super().request(cast_to, options, **kwargs)
            chain = [body["model"]] + [m for m in fallbacks if m != body["model"]]
            for attempt, model in enumerate(chain * 2):
                try:
                    return super().request(cast_to, options.model_copy(update={"json_data": {**body, "model": model}}),
                                           **kwargs)
                except NotFoundError as error:
                    if not model_unavailable(error) or attempt == 2 * len(chain) - 1:
                        raise

    return Client(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        timeout=timeout,
        max_retries=max_retries,
    )
