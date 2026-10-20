import threading
import time
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

from engine.config import ANTHROPIC_API_KEY

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
T = TypeVar("T", bound=BaseModel)


# --- spend tracking -------------------------------------------------------

PRICES = {  # USD per million tokens, (input, output)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
_DEFAULT_PRICE = (4.0, 20.0)  # unknown model: assume expensive, never hide cost

_spend = {"usd": 0.0, "calls": 0, "in_tokens": 0, "out_tokens": 0}
_lock = threading.Lock()


def _record(model: str, usage) -> None:
    pin, pout = PRICES.get(model, _DEFAULT_PRICE)
    cost = (usage.input_tokens * pin + usage.output_tokens * pout) / 1_000_000
    with _lock:
        _spend["usd"] += cost
        _spend["calls"] += 1
        _spend["in_tokens"] += usage.input_tokens
        _spend["out_tokens"] += usage.output_tokens


def spend() -> dict:
    with _lock:
        return {
            "calls": _spend["calls"],
            "in_tokens": _spend["in_tokens"],
            "out_tokens": _spend["out_tokens"],
            "usd": round(_spend["usd"], 4),
        }


def reset_spend() -> None:
    with _lock:
        for k in _spend:
            _spend[k] = 0 if k != "usd" else 0.0


# --- error classification -------------------------------------------------

class _Truncated(Exception):
    """Response hit max_tokens. Retry with a larger budget, not the same one."""


class FatalLLMError(RuntimeError):
    """Will never succeed on retry. Stop the run."""


_FATAL_TYPES = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)

_FATAL_TEXT = (
    "credit balance is too low",
    "invalid x-api-key",
    "not supported for this model",
    "model not found",
)

_TRUNCATION_TEXT = ("eof while parsing", "json_invalid")


def _is_fatal(e: Exception) -> bool:
    if isinstance(e, _FATAL_TYPES):
        return True
    msg = str(e).lower()
    return any(s in msg for s in _FATAL_TEXT)


def _looks_truncated(e: Exception) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in _TRUNCATION_TEXT)


# --- main entry point -----------------------------------------------------

def structured(model: str, system: str, user: str, output_model: Type[T],
               max_tokens: int = 2000, retries: int = 3) -> T:
    """Schema-constrained response. Returns a validated Pydantic instance.

    Notes:
      - Thinking tokens share the max_tokens budget, so a truncated response
        is retried with a doubled budget rather than an identical one.
      - A JSON parse error on a grammar-constrained response is almost always
        truncation surfacing as a validation error, so it grows the budget too.
      - Billing, auth and model-capability errors raise immediately. Retrying
        them triples the damage and never succeeds.
    """
    budget = max_tokens
    last: Exception | None = None

    for attempt in range(retries):
        try:
            resp = _client.messages.parse(
                model=model,
                max_tokens=budget,
                system=system,
                output_format=output_model,
                messages=[{"role": "user", "content": user}],
            )

            if getattr(resp, "usage", None) is not None:
                _record(model, resp.usage)

            if resp.stop_reason == "refusal":
                raise ValueError("model refused the request")
            if resp.stop_reason == "max_tokens":
                raise _Truncated(f"truncated at {budget} tokens")

            return resp.parsed_output

        except _Truncated as e:
            last = e
            budget *= 2

        except Exception as e:  # noqa: BLE001
            if _is_fatal(e):
                raise FatalLLMError(f"fatal, not retrying: {e}") from e
            last = e
            if _looks_truncated(e):
                budget *= 2
            time.sleep(2 ** attempt)

    raise RuntimeError(f"structured call failed after {retries} attempts: {last}")