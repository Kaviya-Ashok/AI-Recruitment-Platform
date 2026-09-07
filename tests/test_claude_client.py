"""Tests for app.ai.claude_client.

Mocking boundary for THIS file: the Anthropic SDK call
(``claude_client._get_client().messages.create``). These tests exercise the
client's own parse / retry / error logic, so they mock one level below the
public function. No real network call is ever made — asserted via mock call
counts, not merely the absence of an error.

Service-layer tests mock one level higher — at ``get_structured_response`` —
see tests/test_job_service_analysis.py. That split is the convention Phase 1.3+
follows.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.ai import claude_client
from app.ai.claude_client import (
    AIConfigError,
    AIOutputError,
    AIRequestError,
    get_structured_response,
)
from app.ai.schemas.jd_analysis import JdAnalysisResult


def _text_message(text: str) -> SimpleNamespace:
    """Mimic anthropic's Message: .content is a list of blocks with .type/.text."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _message_with_usage(text: str, input_tokens: int, output_tokens: int) -> SimpleNamespace:
    """Like _text_message, plus a .usage block (real Message responses have one)."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.fixture
def mock_sdk(mocker):
    """Patch the SDK client; return the mock whose .messages.create you set."""
    fake_client = mocker.MagicMock()
    mocker.patch.object(claude_client, "_get_client", return_value=fake_client)
    # Guarantee we never construct a real Anthropic() client either.
    mocker.patch.object(
        claude_client.anthropic, "Anthropic", side_effect=AssertionError("real SDK used")
    )
    return fake_client


def test_returns_parsed_model_on_valid_response(mock_sdk, ai_response_json):
    mock_sdk.messages.create.return_value = _text_message(
        ai_response_json("jd_analysis_valid.json")
    )

    result = get_structured_response(
        "prompt", JdAnalysisResult, task_name="jd_analysis"
    )

    assert isinstance(result, JdAnalysisResult)
    assert len(result.requirements) == 6
    assert mock_sdk.messages.create.call_count == 1


def test_tolerates_markdown_fences(mock_sdk, ai_response_json):
    fenced = "```json\n" + ai_response_json("jd_analysis_valid.json") + "\n```"
    mock_sdk.messages.create.return_value = _text_message(fenced)

    result = get_structured_response(
        "prompt", JdAnalysisResult, task_name="jd_analysis"
    )
    assert len(result.requirements) == 6
    assert mock_sdk.messages.create.call_count == 1


def test_retries_once_then_succeeds(mock_sdk, ai_response_json):
    mock_sdk.messages.create.side_effect = [
        _text_message("not json at all"),
        _text_message(ai_response_json("jd_analysis_valid.json")),
    ]

    result = get_structured_response(
        "prompt", JdAnalysisResult, task_name="jd_analysis"
    )

    assert isinstance(result, JdAnalysisResult)
    assert mock_sdk.messages.create.call_count == 2
    # The retry must feed the bad output + a correction back as new messages.
    retry_messages = mock_sdk.messages.create.call_args_list[1].kwargs["messages"]
    assert len(retry_messages) == 3
    assert retry_messages[1]["role"] == "assistant"
    assert retry_messages[2]["role"] == "user"


def test_raises_ai_output_error_after_retry_also_fails(mock_sdk):
    mock_sdk.messages.create.side_effect = [
        _text_message("still not json"),
        _text_message('{"requirements": []}'),  # valid JSON, invalid schema
    ]

    with pytest.raises(AIOutputError) as excinfo:
        get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")

    assert mock_sdk.messages.create.call_count == 2
    err = excinfo.value
    assert err.task_name == "jd_analysis"
    assert err.raw_response == '{"requirements": []}'
    assert err.validation_errors


def test_api_error_becomes_ai_request_error(mock_sdk):
    mock_sdk.messages.create.side_effect = claude_client.anthropic.APIError(
        message="boom", request=None, body=None
    )

    with pytest.raises(AIRequestError):
        get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")


def test_missing_api_key_raises_request_error(mocker):
    mocker.patch.object(claude_client, "_client", None)
    mocker.patch.dict("os.environ", {}, clear=True)

    with pytest.raises(AIRequestError):
        get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")


# --- per-task model resolution (added in the claude_client follow-up) ------


def test_jd_analysis_defaults_to_haiku(mock_sdk, ai_response_json, mocker):
    mocker.patch.dict(os.environ, {}, clear=False)
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_MODEL_JD_ANALYSIS"):
        os.environ.pop(key, None)
    mock_sdk.messages.create.return_value = _text_message(
        ai_response_json("jd_analysis_valid.json")
    )

    get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")

    assert (
        mock_sdk.messages.create.call_args.kwargs["model"]
        == "claude-haiku-4-5-20251001"
    )


def test_explicit_model_kwarg_overrides_everything(mock_sdk, ai_response_json, mocker):
    mocker.patch.dict(
        os.environ,
        {
            "ANTHROPIC_MODEL": "claude-opus-5",
            "ANTHROPIC_MODEL_JD_ANALYSIS": "claude-sonnet-5",
        },
    )
    mock_sdk.messages.create.return_value = _text_message(
        ai_response_json("jd_analysis_valid.json")
    )

    get_structured_response(
        "prompt", JdAnalysisResult, task_name="jd_analysis", model="claude-fable-5"
    )

    assert mock_sdk.messages.create.call_args.kwargs["model"] == "claude-fable-5"


def test_task_specific_env_overrides_default_but_not_kwarg(
    mock_sdk, ai_response_json, mocker
):
    mocker.patch.dict(
        os.environ, {"ANTHROPIC_MODEL_JD_ANALYSIS": "claude-sonnet-5"}
    )
    mock_sdk.messages.create.return_value = _text_message(
        ai_response_json("jd_analysis_valid.json")
    )

    get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")

    assert mock_sdk.messages.create.call_args.kwargs["model"] == "claude-sonnet-5"


def test_global_env_overrides_default_but_loses_to_task_specific_env(
    mock_sdk, ai_response_json, mocker
):
    mocker.patch.dict(
        os.environ,
        {
            "ANTHROPIC_MODEL": "claude-opus-5",
            "ANTHROPIC_MODEL_JD_ANALYSIS": "claude-sonnet-5",
        },
    )
    mock_sdk.messages.create.return_value = _text_message(
        ai_response_json("jd_analysis_valid.json")
    )

    get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")

    assert mock_sdk.messages.create.call_args.kwargs["model"] == "claude-sonnet-5"

    # And with only the global override set, it wins over DEFAULT_MODELS.
    mocker.patch.dict(
        os.environ, {"ANTHROPIC_MODEL": "claude-opus-5"}, clear=False
    )
    os.environ.pop("ANTHROPIC_MODEL_JD_ANALYSIS", None)
    get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")
    assert mock_sdk.messages.create.call_args.kwargs["model"] == "claude-opus-5"


def test_unknown_task_name_raises_config_error_before_any_api_call(mock_sdk, mocker):
    mocker.patch.dict(os.environ, {}, clear=False)
    for key in list(os.environ):
        if key.startswith("ANTHROPIC_MODEL"):
            os.environ.pop(key, None)

    with pytest.raises(AIConfigError):
        get_structured_response(
            "prompt", JdAnalysisResult, task_name="totally_unmapped_task"
        )

    mock_sdk.messages.create.assert_not_called()


# --- token/cost logging ----------------------------------------------


def test_logs_task_model_tokens_and_cost(mock_sdk, ai_response_json, mocker, caplog):
    mocker.patch.dict(os.environ, {}, clear=False)
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_MODEL_JD_ANALYSIS"):
        os.environ.pop(key, None)
    mock_sdk.messages.create.return_value = _message_with_usage(
        ai_response_json("jd_analysis_valid.json"),
        input_tokens=1000,
        output_tokens=2000,
    )

    with caplog.at_level("INFO", logger="app.ai.claude_client"):
        get_structured_response("prompt", JdAnalysisResult, task_name="jd_analysis")

    usage_lines = [r.message for r in caplog.records if "ai_usage" in r.message]
    assert len(usage_lines) == 1
    line = usage_lines[0]
    # claude-haiku-4-5-20251001 is $1.00/$5.00 per 1M -> 1000*1/1e6 + 2000*5/1e6 = 0.011
    assert "task=jd_analysis" in line
    assert "model=claude-haiku-4-5-20251001" in line
    assert "input_tokens=1000" in line
    assert "output_tokens=2000" in line
    assert "cost_usd=0.011000" in line
    # No prompt or response content leaked into the log line.
    assert "requirement" not in line.lower()


def test_unpriced_model_logs_cost_unknown_not_crash(mock_sdk, ai_response_json, mocker, caplog):
    mocker.patch.dict(os.environ, {"ANTHROPIC_MODEL_JD_ANALYSIS": "claude-mystery-9000"})
    mock_sdk.messages.create.return_value = _message_with_usage(
        ai_response_json("jd_analysis_valid.json"),
        input_tokens=500,
        output_tokens=500,
    )

    with caplog.at_level("INFO", logger="app.ai.claude_client"):
        result = get_structured_response(
            "prompt", JdAnalysisResult, task_name="jd_analysis"
        )

    assert isinstance(result, JdAnalysisResult)  # did not crash
    usage_lines = [r.message for r in caplog.records if "ai_usage" in r.message]
    assert len(usage_lines) == 1
    assert "cost_usd=unknown" in usage_lines[0]
    assert "model=claude-mystery-9000" in usage_lines[0]
