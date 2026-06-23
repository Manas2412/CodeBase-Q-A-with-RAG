"""Tests for app/review/reviewer.py + app/review/checklist.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.review.bedrock_client import BedrockClient
from app.review.checklist import (
    DEFAULT_CHECKLIST,
    DEFAULT_CHECKLIST_VERSION,
    format_checklist,
)
from app.review.context_builder import ContextChunk
from app.review.diff_parser import DiffHunk
from app.review.reviewer import (
    Finding,
    ReviewParseError,
    parse_review_response,
    review_diff,
)


# ── helpers ─────────────────────────────────────────────────────────────
def _diff_hunk(file_path: str = "app/main.py") -> DiffHunk:
    return DiffHunk(
        file_path=file_path,
        old_file_path=file_path,
        change_type="modified",
        new_start=10,
        new_count=3,
        old_start=10,
        old_count=2,
        added_lines=("    return user.password", "    # TODO: remove"),
        removed_lines=("    return None",),
        raw=(
            "@@ -10,2 +10,3 @@\n"
            "-    return None\n"
            "+    return user.password\n"
            "+    # TODO: remove\n"
        ),
    )


def _ctx_chunk(file_path: str = "app/main.py", content: str = "def x(): pass") -> ContextChunk:
    return ContextChunk(
        file_path=file_path,
        name="x",
        chunk_type="function",
        start_line=0,
        end_line=5,
        content=content,
        relevance_reason="same_file",
        similarity=1.0,
        token_estimate=10,
    )


def _bedrock_returning(json_payload: dict, *, mock_bedrock) -> BedrockClient:
    """Override mock_bedrock to return a chat response containing `json_payload`."""
    text = json.dumps(json_payload)
    chat_payload = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_creation_input_tokens": 800,  # checklist cached on this call
            "cache_read_input_tokens": 0,
        },
    }
    stream = MagicMock()
    stream.read.return_value = json.dumps(chat_payload).encode()
    mock_bedrock.invoke_model.side_effect = None
    mock_bedrock.invoke_model.return_value = {"body": stream}
    return BedrockClient(client=mock_bedrock)


# ── parse_review_response ───────────────────────────────────────────────
def test_parse_review_response_extracts_summary_and_findings():
    raw = json.dumps(
        {
            "summary": "Two issues spotted.",
            "findings": [
                {
                    "severity": "critical",
                    "category": "security",
                    "file_path": "app/main.py",
                    "start_line": 11,
                    "end_line": 11,
                    "message": "Password returned to client.",
                    "suggestion": "Strip secret fields before serialising.",
                    "rule_id": "no-hardcoded-secrets",
                }
            ],
        }
    )
    summary, findings = parse_review_response(raw)
    assert summary == "Two issues spotted."
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "critical"
    assert f.file_path == "app/main.py"
    assert f.start_line == 11
    assert f.rule_id == "no-hardcoded-secrets"
    assert f.suggestion is not None


def test_parse_review_response_handles_markdown_code_fence():
    raw = "```json\n" + json.dumps({"summary": "ok", "findings": []}) + "\n```"
    summary, findings = parse_review_response(raw)
    assert summary == "ok"
    assert findings == []


def test_parse_review_response_handles_prose_around_json():
    """LLM prepended a sentence despite the 'JSON only' instruction — recover."""
    raw = (
        "Sure! Here's the review:\n\n"
        + json.dumps({"summary": "lgtm", "findings": []})
        + "\n\nLet me know if you need more detail."
    )
    summary, findings = parse_review_response(raw)
    assert summary == "lgtm"
    assert findings == []


def test_parse_review_response_skips_malformed_findings():
    """One bad finding doesn't kill the whole review."""
    raw = json.dumps(
        {
            "summary": "Mixed bag.",
            "findings": [
                {
                    "severity": "minor",
                    "category": "design",
                    "file_path": "x.py",
                    "start_line": 5,
                    "end_line": 5,
                    "message": "good",
                    "suggestion": None,
                    "rule_id": None,
                },
                # missing file_path
                {
                    "severity": "minor",
                    "message": "bad finding — should be dropped",
                },
            ],
        }
    )
    summary, findings = parse_review_response(raw)
    assert summary == "Mixed bag."
    assert len(findings) == 1
    assert findings[0].file_path == "x.py"


def test_parse_review_response_raises_on_garbage():
    with pytest.raises(ReviewParseError):
        parse_review_response("definitely not json at all")


def test_parse_review_response_coerces_string_line_numbers():
    """LLMs sometimes return line numbers as strings — coerce to int."""
    raw = json.dumps(
        {
            "summary": "x",
            "findings": [
                {
                    "severity": "info",
                    "category": "style",
                    "file_path": "x.py",
                    "start_line": "42",
                    "end_line": "44",
                    "message": "...",
                }
            ],
        }
    )
    _, findings = parse_review_response(raw)
    assert findings[0].start_line == 42
    assert findings[0].end_line == 44


# ── review_diff ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_review_diff_calls_bedrock_with_cache_prefix(mock_bedrock):
    """The checklist goes in via cache_prefix so prompt caching kicks in."""
    bc = _bedrock_returning(
        {"summary": "clean", "findings": []}, mock_bedrock=mock_bedrock
    )

    summary, findings, response = await review_diff(
        [_diff_hunk()], [_ctx_chunk()], bedrock=bc
    )

    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])

    # First content block of the first message must be the cached checklist
    first_blocks = body["messages"][0]["content"]
    assert first_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # And the cached text is the rendered checklist (starts with the heading)
    assert first_blocks[0]["text"].startswith("# REVIEW CHECKLIST")
    # Non-cached blocks follow
    assert "cache_control" not in first_blocks[1]


@pytest.mark.asyncio
async def test_review_diff_returns_parsed_findings(mock_bedrock):
    bc = _bedrock_returning(
        {
            "summary": "Looks risky.",
            "findings": [
                {
                    "severity": "critical",
                    "category": "security",
                    "file_path": "app/main.py",
                    "start_line": 11,
                    "end_line": 11,
                    "message": "Password leak",
                    "suggestion": "Don't return secrets.",
                    "rule_id": "no-hardcoded-secrets",
                }
            ],
        },
        mock_bedrock=mock_bedrock,
    )

    summary, findings, response = await review_diff(
        [_diff_hunk()], [_ctx_chunk()], bedrock=bc
    )

    assert summary == "Looks risky."
    assert len(findings) == 1
    assert findings[0].rule_id == "no-hardcoded-secrets"
    # ChatResponse still carries token usage for the caller to persist
    assert response.input_tokens == 120
    assert response.cache_creation_tokens == 800


@pytest.mark.asyncio
async def test_review_diff_includes_diff_and_context_in_user_message(mock_bedrock):
    bc = _bedrock_returning(
        {"summary": "ok", "findings": []}, mock_bedrock=mock_bedrock
    )
    await review_diff(
        [_diff_hunk("app/foo.py")],
        [_ctx_chunk("app/foo.py", content="def helper(): return 7")],
        bedrock=bc,
    )

    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    # The user message is the SECOND content block (first is cached checklist)
    user_text = body["messages"][0]["content"][1]["text"]
    assert "RELATED CODE" in user_text
    assert "def helper(): return 7" in user_text
    assert "DIFF TO REVIEW" in user_text
    assert "app/foo.py" in user_text
    assert "OUTPUT FORMAT" in user_text
    assert "valid JSON only" in user_text


@pytest.mark.asyncio
async def test_review_diff_uses_system_prompt(mock_bedrock):
    bc = _bedrock_returning(
        {"summary": "ok", "findings": []}, mock_bedrock=mock_bedrock
    )
    await review_diff([_diff_hunk()], [], bedrock=bc)

    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    assert "senior tech lead" in body["system"]


@pytest.mark.asyncio
async def test_review_diff_empty_context_is_acceptable(mock_bedrock):
    """A diff with no existing context (brand-new project, nothing in pgvector
    yet) should still produce a usable review."""
    bc = _bedrock_returning(
        {"summary": "fresh", "findings": []}, mock_bedrock=mock_bedrock
    )
    summary, findings, _ = await review_diff([_diff_hunk()], [], bedrock=bc)
    assert summary == "fresh"
    assert findings == []


# ── format_checklist ────────────────────────────────────────────────────
def test_format_checklist_groups_by_category():
    text = format_checklist(DEFAULT_CHECKLIST)
    # Heading present
    assert "# REVIEW CHECKLIST" in text
    # Categories grouped
    assert "## Security" in text
    assert "## Correctness" in text
    assert "## Performance" in text
    # Rule ids appear under their category with the severity in the bullet
    assert "no-hardcoded-secrets" in text
    assert "(critical)" in text


def test_default_checklist_rules_have_required_keys():
    for rule in DEFAULT_CHECKLIST:
        assert set(rule.keys()) >= {
            "id",
            "category",
            "severity_default",
            "title",
            "description",
        }
        assert rule["severity_default"] in ("info", "minor", "major", "critical")
        assert rule["category"] in (
            "security",
            "correctness",
            "performance",
            "design",
            "testing",
            "documentation",
        )


def test_default_checklist_version_is_one():
    assert DEFAULT_CHECKLIST_VERSION == 1
