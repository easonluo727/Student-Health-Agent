"""The only place an LLM client is created. Any OpenAI-compatible endpoint works.

Reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from the environment, falling back
to the gitignored .env at the repo root. Optional LLM_FALLBACK_MODELS (comma-separated) are tried
in order when the main model answers 404 model_not_found or a gateway error (502, 503, 504) or can't be reached;
when all of them fail, optional LLM_BACKUP_MODELS (comma-separated) are tried on a second provider,
LLM_BACKUP_BASE_URL with LLM_BACKUP_API_KEY. Optional LLM_EXTRA_BODY (JSON) is sent with
the fast, no-reasoning calls; for Qwen on vLLM use
{"chat_template_kwargs": {"enable_thinking": false}}.

Bill photos may be read by a separate model (bill_readers): BILL_BASE_URL, BILL_MODEL and optional BILL_API_KEY,
for example the fine-tuned Muse Glimmer served by scripts/bills/modal_serve.py. BILL_TIMEOUT (seconds, default 90)
bounds one attempt there, so a cold or stopped server falls back to the main model instead of stalling the turn.
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


# Gateway errors (2026-11-04: "503 Internet Connection Server Error" after every question): the model's servers are
# down or overloaded. The SDK has already retried the same model by then, so only another model can help.
UNREACHABLE = (502, 503, 504)


def make_client(timeout=90.0, max_retries=3):
    load_env()
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} (in the environment or {ENV_PATH}).")
    from openai import APIConnectionError, APIStatusError, NotFoundError, OpenAI

    fallbacks = fallback_models()
    backup_models = [m.strip() for m in os.environ.get("LLM_BACKUP_MODELS", "").split(",") if m.strip()]
    backup = (OpenAI(base_url=os.environ["LLM_BACKUP_BASE_URL"], api_key=os.environ["LLM_BACKUP_API_KEY"],
                     timeout=timeout, max_retries=max_retries)
              if backup_models and os.environ.get("LLM_BACKUP_BASE_URL") and os.environ.get("LLM_BACKUP_API_KEY")
              else None)

    class Client(OpenAI):
        # Meta's API (2026-10-26) answers 404 model_not_found for a model it serves while that model's
        # servers are unreachable, sometimes for minutes; other models keep working meanwhile. So on that
        # 404 the same request goes to the next model in LLM_FALLBACK_MODELS, and the whole list is tried
        # twice. The SDK's own retries (5xx, 429, timeouts) still apply to each attempt, so a gateway error
        # (UNREACHABLE) or no connection moves on to the next model but never back to one that already failed
        # that way. When every one of them is down, LLM_BACKUP_MODELS are tried in order on another provider
        # (LLM_BACKUP_BASE_URL, LLM_BACKUP_API_KEY), once each.
        backup_client = backup

        def request(self, cast_to, options, **kwargs):
            body = options.json_data
            if not isinstance(body, dict) or "model" not in body:
                return super().request(cast_to, options, **kwargs)
            chain = [body["model"]] + [m for m in fallbacks if m != body["model"]]
            down = set()
            last = None
            for attempt, model in enumerate(chain * 2):
                if model in down:
                    continue
                try:
                    return super().request(cast_to, options.model_copy(update={"json_data": {**body, "model": model}}),
                                           **kwargs)
                except NotFoundError as error:
                    if not model_unavailable(error):
                        raise
                    last = error
                except APIStatusError as error:
                    if error.status_code not in UNREACHABLE:
                        raise
                    down.add(model)
                    last = error
                except APIConnectionError as error:
                    down.add(model)
                    last = error
            for model in backup_models if self.backup_client else []:
                try:
                    return self.backup_client.request(
                        cast_to, options.model_copy(update={"json_data": {**body, "model": model}}), **kwargs)
                except NotFoundError as error:
                    if not model_unavailable(error):
                        raise
                    last = error
                except APIStatusError as error:
                    if error.status_code not in UNREACHABLE:
                        raise
                    last = error
                except APIConnectionError as error:
                    last = error
            raise last

    return Client(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        timeout=timeout,
        max_retries=max_retries,
    )


def bill_readers():
    """[(client, model)] to read a bill photo with, in order: the BILL_* model when set, then the main model."""
    load_env()
    readers = []
    if os.environ.get("BILL_BASE_URL") and os.environ.get("BILL_MODEL"):
        from openai import OpenAI

        readers.append((OpenAI(base_url=os.environ["BILL_BASE_URL"], api_key=os.environ.get("BILL_API_KEY") or "none",
                               timeout=float(os.environ.get("BILL_TIMEOUT", 90)), max_retries=0),
                        os.environ["BILL_MODEL"]))
    readers.append((make_client(timeout=180.0), llm_model()))
    return readers
