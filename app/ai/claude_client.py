"""Claude API client — the single boundary between this app and Anthropic.

Every AI call in the platform goes through :func:`get_structured_response`.
Phase 1.3+ services reuse this function and mock it at this boundary in tests.

Responsibilities (and their limits):

* Resolve which model a task runs on (see "Model configuration" below).
* Call the Messages API asking for JSON that matches a Pydantic ``schema``.
* Parse + **schema-validate** the response. On validation failure, retry once
  with the validation error fed back, asking for corrected JSON only.
* On repeated failure, raise :class:`AIOutputError` — never invent a fallback.
* On transport/API failure, raise :class:`AIRequestError`.
* Log token usage and an estimated USD cost for every real API response.

**Business-rule** validation (e.g. "a JD must yield at least one MANDATORY
requirement") is *not* done here — that belongs in the service layer.

Model configuration
--------------------
Each task has a default model in :data:`DEFAULT_MODELS`. Resolution order for
a given call, most specific wins:

    1. an explicit ``model=`` kwarg to :func:`get_structured_response`
    2. ``ANTHROPIC_MODEL_<TASK_NAME>`` (task_name upper-cased) env var
    3. ``ANTHROPIC_MODEL`` env var (global override, handy for ad-hoc smoke
       testing without touching per-task config)
    4. ``DEFAULT_MODELS[task_name]``

A ``task_name`` that is in none of the above (i.e. missing from
``DEFAULT_MODELS`` with no override supplied) raises :class:`AIConfigError`
*before* any API call is attempted — an unmapped task never silently falls
back to a hardcoded model.

Configuration (environment only, never hard-coded):
* ``ANTHROPIC_API_KEY`` — required at call time.
* ``ANTHROPIC_MODEL`` / ``ANTHROPIC_MODEL_<TASK>`` — see above.

Logging: task name, outcome, latency, and (when the API returned usage data)
token counts + estimated cost are logged at INFO. JD text, candidate content,
prompts and full raw AI responses are sensitive/large and are never logged
above DEBUG (validation error detail only).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TypeVar

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

# Per-task default model. Only "jd_analysis" has a real caller as of Phase 1.2;
# the rest are placeholders so every future phase's AI task is configured from
# one place instead of being retrofitted piecemeal.
DEFAULT_MODELS: dict[str, str] = {
    "jd_analysis": "claude-haiku-4-5-20251001",
    "resume_parsing": "claude-haiku-4-5-20251001",
    # Judgment-heavy (matching ambiguous evidence to a criterion; the UNKNOWN-vs
    # -FAIL call). Closer in nature to rubric_generation than to resume_parsing.
    # Per the project rule "judgment/synthesis => Sonnet; extraction => Haiku"
    # the initial default is Sonnet, PENDING an empirical Haiku-vs-Sonnet
    # comparison — run scripts/validate_prequalification.py and check the
    # UNKNOWN-vs-FAIL boundary cases before locking this in.
    "prequalification": "claude-sonnet-5",
    # Haiku vs Sonnet compared 2026-09-01 (scripts/compare_rubric_models.py on a
    # real job's requirements): output was near-identical — 16/17 criteria byte
    # -identical, the 17th a meaning-preserving reword; MANDATORY/PREFERRED
    # boundaries, BEHAVIORAL separation, and no invention/dropping held on both.
    # Haiku selected. Based on ONE comparison run — re-run that script if rubric
    # quality looks off on a more complex/ambiguous JD.
    "rubric_generation": "claude-haiku-4-5-20251001",
    "screening_question": "claude-sonnet-5",
    # Judgment-heavy: reconcile the resume-only prequalification verdict against
    # a candidate's screening answers, per criterion. Same nature as
    # prequalification/screening_question -> Sonnet, PENDING the same future
    # empirical Haiku-vs-Sonnet comparison those entries still carry (no
    # scripts/validate_screening_evaluation.py yet).
    "screening_evaluation": "claude-sonnet-5",
    "interview_guide": "claude-sonnet-5",
    "post_interview_analysis": "claude-sonnet-5",
}

# $ per 1M tokens: (input_rate, output_rate). Hardcoded — NOT fetched
# dynamically. Anthropic's rates can change; re-check
# https://www.anthropic.com/pricing periodically and update this table.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}

_MAX_TOKENS = 8000
_SYSTEM_PROMPT = (
    "You are a precise information-extraction assistant for a recruitment "
    "platform. You return ONLY a single valid JSON object matching the schema "
    "given in the user message — no prose, no markdown fences. You never "
    "fabricate information that is not supported by the provided source text."
)

_CORRECTION_TEMPLATE = (
    "Your previous response could not be parsed/validated against the required "
    "schema.\n\nError(s):\n{errors}\n\n"
    "Return ONLY a corrected JSON object that satisfies the schema. No prose, "
    "no markdown fences."
)

TModel = TypeVar("TModel", bound=BaseModel)

_client: anthropic.Anthropic | None = None


class AIError(Exception):
    """Base class for all AI-layer failures."""


class AIConfigError(AIError):
    """Raised when a task's model cannot be resolved (unmapped task_name)."""


class AIRequestError(AIError):
    """The Claude API call itself failed (network, timeout, 4xx/5xx, auth)."""


class AIOutputError(AIError):
    """Claude's response failed schema validation after all retries.

    Carries enough context to debug *which* layer went wrong (CLAUDE.md §29):
    prompt vs. model behaviour vs. validation.
    """

    def __init__(
        self,
        task_name: str,
        raw_response: str,
        validation_errors: str,
    ) -> None:
        super().__init__(
            f"{task_name}: AI response failed schema validation after retries"
        )
        self.task_name = task_name
        self.raw_response = raw_response
        self.validation_errors = validation_errors


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise AIRequestError(
                "ANTHROPIC_API_KEY is not set — add it to your .env "
                "(see .env.example)."
            )
        _client = anthropic.Anthropic()
    return _client


def _resolve_model(task_name: str, explicit_model: str | None) -> str:
    """Resolve the model for one call. See module docstring for precedence.

    Raises
    ------
    AIConfigError
        ``task_name`` has no explicit model, no matching env override, and is
        not in :data:`DEFAULT_MODELS`.
    """
    if explicit_model:
        return explicit_model

    task_env_key = f"ANTHROPIC_MODEL_{task_name.upper()}"
    task_override = os.getenv(task_env_key)
    if task_override:
        return task_override

    global_override = os.getenv("ANTHROPIC_MODEL")
    if global_override:
        return global_override

    if task_name in DEFAULT_MODELS:
        return DEFAULT_MODELS[task_name]

    raise AIConfigError(
        f"No model configured for task_name={task_name!r}: it is not in "
        f"DEFAULT_MODELS, and neither {task_env_key} nor ANTHROPIC_MODEL is set. "
        "Add it to DEFAULT_MODELS, pass model= explicitly, or set one of those "
        "env vars."
    )


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> str:
    """Return a formatted USD cost string, or ``"unknown"`` if unpriced."""
    rates = PRICING.get(model)
    if rates is None:
        return "unknown"
    input_rate, output_rate = rates
    cost = (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate
    return f"{cost:.6f}"


def _log_usage(task_name: str, model: str, attempt: int, total_attempts: int, message) -> None:
    """Log token counts + estimated cost for one API response, if it carried
    usage data. Never logs prompt/response content."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return
    cost_usd = _estimate_cost_usd(model, input_tokens, output_tokens)
    logger.info(
        "ai_usage task=%s attempt=%d/%d model=%s input_tokens=%d output_tokens=%d cost_usd=%s",
        task_name, attempt, total_attempts, model, input_tokens, output_tokens, cost_usd,
    )


def _extract_text(message: anthropic.types.Message) -> str:
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()


def _strip_code_fences(text: str) -> str:
    """Remove a wrapping ```...``` fence if the model added one anyway.

    Structural only — does not alter the JSON payload itself.
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def get_structured_response(
    prompt: str,
    schema: type[TModel],
    *,
    task_name: str,
    max_retries: int = 1,
    model: str | None = None,
) -> TModel:
    """Send ``prompt`` to Claude and return an instance of ``schema``.

    ``model`` overrides model resolution for this call only (see module
    docstring for the full precedence order); normally omitted so the
    task-based default/env config applies.

    Raises
    ------
    AIConfigError
        ``task_name`` has no resolvable model (see :func:`_resolve_model`).
        Raised before any API call.
    AIRequestError
        The API call failed (network / timeout / status / missing key).
    AIOutputError
        The response could not be parsed/validated after ``max_retries`` retries.
    """
    resolved_model = _resolve_model(task_name, model)
    client = _get_client()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    total_attempts = max_retries + 1

    last_raw = ""
    last_errors = ""

    for attempt_idx in range(total_attempts):
        attempt = attempt_idx + 1  # 1-based for logging ("attempt 1/2")
        started = time.monotonic()
        try:
            message = client.messages.create(
                model=resolved_model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=messages,
            )
        except anthropic.APIError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "ai task=%s outcome=request_error attempt=%d/%d model=%s latency_ms=%d kind=%s",
                task_name, attempt, total_attempts, resolved_model, latency_ms, type(exc).__name__,
            )
            raise AIRequestError(f"{task_name}: Claude API request failed") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        _log_usage(task_name, resolved_model, attempt, total_attempts, message)
        last_raw = _extract_text(message)

        try:
            payload = json.loads(_strip_code_fences(last_raw))
            result = schema.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_errors = str(exc)
            logger.warning(
                "ai task=%s outcome=validation_error attempt=%d/%d model=%s latency_ms=%d raw_len=%d",
                task_name, attempt, total_attempts, resolved_model, latency_ms, len(last_raw),
            )
            logger.debug("ai task=%s validation detail: %s", task_name, last_errors)
            if attempt_idx >= max_retries:
                raise AIOutputError(task_name, last_raw, last_errors) from exc
            messages.append({"role": "assistant", "content": last_raw})
            messages.append(
                {"role": "user", "content": _CORRECTION_TEMPLATE.format(errors=last_errors)}
            )
            continue

        logger.info(
            "ai task=%s outcome=ok attempt=%d/%d model=%s latency_ms=%d",
            task_name, attempt, total_attempts, resolved_model, latency_ms,
        )
        return result

    # Unreachable: the loop either returns or raises.
    raise AIOutputError(task_name, last_raw, last_errors)
