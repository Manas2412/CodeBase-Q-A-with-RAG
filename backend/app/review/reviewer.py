"""The reviewer — the orchestrator that produces a structured review for a push.

Two public entry points:

  • review_diff(hunks, context, *, bedrock, checklist)
        Pure LLM call. Takes pre-computed hunks and context, returns
        (summary, findings, ChatResponse). Useful when the caller has
        already done the diff + context work (tests, the polling agent
        running review_push_task) and just needs the LLM step.

  • run_review_for_push(project_id, before, after, branch, *, conn, ...)
        Full end-to-end orchestrator: diff → context → LLM → persist
        to `reviews` + `review_findings` tables. Returns a ReviewResult.

Prompt design
-------------
We use Bedrock prompt caching to make repeated reviews cheap: the
checklist (the same ~1000 tokens across every review of every project
within the 5-minute cache TTL) is sent as a cached prefix, getting
90% off on subsequent reads.

The output schema is JSON-only. The model is instructed to return
exactly `{summary: str, findings: [Finding, ...]}` with finding fields
matching the `review_findings` table columns. We parse defensively —
malformed individual findings get skipped rather than failing the whole
review, and partial JSON (truncated output) is recovered when possible.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Sequence

import asyncpg

from app.review.bedrock_client import (
    MAX_OUTPUT_TOKENS,
    BedrockClient,
    ChatResponse,
    get_bedrock_client,
)
from app.review.checklist import (
    DEFAULT_CHECKLIST,
    DEFAULT_CHECKLIST_VERSION,
    Rule,
    format_checklist,
)
from app.review.context_builder import ContextChunk, build_context
from app.review.diff_parser import DiffHunk, diff_between


# ── Output dataclasses ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Finding:
    """A single review finding — maps 1:1 to a `review_findings` row."""

    severity: str       # 'info' | 'minor' | 'major' | 'critical'
    category: str       # 'security' | 'correctness' | 'performance' | 'design' | 'testing' | 'documentation'
    file_path: str
    start_line: int | None
    end_line: int | None
    message: str
    suggestion: str | None
    rule_id: str | None


@dataclass(frozen=True)
class ReviewResult:
    """End-state of a full review pass."""

    review_id: uuid.UUID | None  # None when the caller asked for a dry-run (no DB write)
    summary: str
    findings: list[Finding]
    severity_counts: dict[str, int]
    token_usage: dict[str, int]
    raw_response: str            # the LLM's raw text — kept for debugging / audit


class ReviewParseError(Exception):
    """LLM output couldn't be parsed into a usable review shape."""


# ── Prompt assembly ──────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a senior tech lead reviewing a code change. "
    "Apply the supplied checklist rigorously. Stay concise. "
    "Cite specific lines when you flag something. "
    "Respond with valid JSON only — no prose before or after."
)


_OUTPUT_FORMAT_BLOCK = """\
== OUTPUT FORMAT ==
Respond with valid JSON only, matching this exact shape:

{
  "summary": "<one-paragraph overview of the change quality>",
  "findings": [
    {
      "severity": "info" | "minor" | "major" | "critical",
      "category": "security" | "correctness" | "performance" | "design" | "testing" | "documentation",
      "file_path": "<relative path from the diff>",
      "start_line": <integer or null>,
      "end_line": <integer or null>,
      "message": "<what's wrong and why it matters>",
      "suggestion": "<concrete fix, optional>",
      "rule_id": "<the matching checklist rule id, optional>"
    }
  ]
}

If the change is clean (no concerns), return an empty `findings` array
and a summary saying so.
"""


def _format_context_block(chunks: Sequence[ContextChunk]) -> str:
    if not chunks:
        return ""
    parts = ["== RELATED CODE (for context, not part of the diff) =="]
    for c in chunks:
        header = (
            f"--- {c.file_path} (lines {c.start_line + 1}-{c.end_line + 1}, "
            f"{c.chunk_type}{' ' + c.name if c.name else ''})"
        )
        parts.append(header)
        parts.append("```")
        parts.append(c.content)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def _format_diff_block(hunks: Sequence[DiffHunk]) -> str:
    parts = ["== DIFF TO REVIEW =="]
    for h in hunks:
        header_bits = [f"file: {h.file_path}", f"change: {h.change_type}"]
        if h.change_type == "renamed" and h.old_file_path:
            header_bits.append(f"renamed_from: {h.old_file_path}")
        parts.append("--- " + " | ".join(header_bits))
        parts.append("```diff")
        parts.append(h.raw)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def _build_user_message(
    hunks: Sequence[DiffHunk],
    context: Sequence[ContextChunk],
) -> str:
    """The non-cached portion of the prompt — diff and context vary per review."""
    blocks = []
    ctx = _format_context_block(context)
    if ctx:
        blocks.append(ctx)
    blocks.append(_format_diff_block(hunks))
    blocks.append(_OUTPUT_FORMAT_BLOCK)
    return "\n\n".join(blocks)


# ── Response parsing ─────────────────────────────────────────────────────
def _strip_code_fences(text: str) -> str:
    return re.sub(
        r"^```(?:json)?\s*|\s*```\s*$",
        "",
        text.strip(),
        flags=re.MULTILINE,
    ).strip()


def _try_extract_json_object(text: str) -> dict | None:
    """Find the first { ... } block in the text and try to parse it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_review_response(raw: str) -> tuple[str, list[Finding]]:
    """Extract (summary, findings) from an LLM response.

    Tolerates markdown code-fence wrapping and surrounding prose. Individual
    malformed findings get dropped (with the rest of the review preserved)
    rather than failing the whole call — a single bad finding shouldn't
    cost the user the entire review.
    """
    cleaned = _strip_code_fences(raw)
    data: dict | None = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = _try_extract_json_object(cleaned)
    if not isinstance(data, dict):
        raise ReviewParseError(
            f"LLM response did not contain a JSON object: {raw[:200]!r}"
        )

    summary = str(data.get("summary", "")).strip()
    raw_findings = data.get("findings") or []
    findings: list[Finding] = []
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        try:
            findings.append(_to_finding(f))
        except (KeyError, ValueError, TypeError):
            continue  # skip malformed; preserve the rest
    return summary, findings


def _to_finding(raw: dict) -> Finding:
    return Finding(
        severity=str(raw.get("severity", "info")).lower(),
        category=str(raw.get("category", "general")).lower(),
        file_path=str(raw["file_path"]),
        start_line=_to_int_or_none(raw.get("start_line")),
        end_line=_to_int_or_none(raw.get("end_line")),
        message=str(raw.get("message", "")).strip(),
        suggestion=(str(raw["suggestion"]).strip() if raw.get("suggestion") else None),
        rule_id=(str(raw["rule_id"]).strip() if raw.get("rule_id") else None),
    )


def _to_int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _count_severities(findings: Sequence[Finding]) -> dict[str, int]:
    counts = {"info": 0, "minor": 0, "major": 0, "critical": 0}
    for f in findings:
        if f.severity in counts:
            counts[f.severity] += 1
    return counts


# ── Pure LLM step ────────────────────────────────────────────────────────
async def review_diff(
    hunks: Sequence[DiffHunk],
    context: Sequence[ContextChunk],
    *,
    bedrock: BedrockClient | None = None,
    checklist: list[Rule] | None = None,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    temperature: float = 0.0,
) -> tuple[str, list[Finding], ChatResponse]:
    """Run the LLM review on pre-computed hunks + context.

    Returns (summary, findings, raw_chat_response). The raw response is
    returned so callers can persist token usage + cache stats.
    """
    bc = bedrock or get_bedrock_client()
    rules = checklist or DEFAULT_CHECKLIST

    cache_prefix = format_checklist(rules)
    user_message = _build_user_message(hunks, context)

    response = await bc.chat(
        messages=[{"role": "user", "content": user_message}],
        system=SYSTEM_PROMPT,
        cache_prefix=cache_prefix,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    summary, findings = parse_review_response(response.text)
    return summary, findings, response


# ── Full orchestrator (with persistence) ─────────────────────────────────
async def run_review_for_push(
    project_id: str,
    before: str,
    after: str,
    branch: str,
    *,
    conn: asyncpg.Connection,
    bedrock: BedrockClient | None = None,
    checklist: list[Rule] | None = None,
    checklist_version: int = DEFAULT_CHECKLIST_VERSION,
    token_budget: int = 10_000,
    persist: bool = True,
    review_id: uuid.UUID | None = None,
) -> ReviewResult:
    """End-to-end review for a (branch, before..after) push.

    1. Diff via `git diff`
    2. Context via pgvector
    3. LLM call (Claude Opus via Bedrock, JSON-mode prompt)
    4. Parse findings
    5. Persist to `reviews` + `review_findings` (when persist=True)

    Persistence modes:
      * `review_id` provided (Day-5 production path) — UPDATE the
        pre-existing pending row claimed atomically by `_review_push`.
      * `review_id` None (tests, ad-hoc reviews) — INSERT a fresh
        'done' row. Same behaviour as Day 4.

    For dry-runs pass `persist=False`; the returned `review_id` is then
    whatever was passed in (or None).

    Empty diffs short-circuit — we return an empty review without an LLM call,
    and if `persist=True` with a pre-claimed `review_id`, we still mark it
    'done' so the row doesn't linger as a stale 'running'.
    """
    hunks = await diff_between(project_id, before, after)
    if not hunks:
        # Nothing to review. Don't burn LLM tokens.
        empty_summary = "No changes between the supplied SHAs."
        empty_severity = _count_severities([])
        empty_token_usage = {"input": 0, "output": 0, "total": 0}

        # When a pre-claimed review_id is in play we still mark the row
        # 'done' so it doesn't linger as 'running' forever (otherwise the
        # partial unique index keeps blocking future writes for this tuple).
        if persist and review_id is not None:
            await _persist_review(
                conn=conn,
                project_id=project_id,
                branch=branch,
                before_sha=before,
                after_sha=after,
                summary=empty_summary,
                severity_counts=empty_severity,
                token_usage=empty_token_usage,
                checklist_version=checklist_version,
                findings=[],
                review_id=review_id,
            )

        return ReviewResult(
            review_id=review_id,
            summary=empty_summary,
            findings=[],
            severity_counts=empty_severity,
            token_usage=empty_token_usage,
            raw_response="",
        )

    context = await build_context(
        conn, project_id, hunks, token_budget=token_budget, client=bedrock
    )
    summary, findings, response = await review_diff(
        hunks,
        context,
        bedrock=bedrock,
        checklist=checklist,
    )

    severity_counts = _count_severities(findings)
    token_usage = {
        "input": response.input_tokens,
        "output": response.output_tokens,
        "total": response.total_tokens,
        "cache_read": response.cache_read_tokens,
        "cache_creation": response.cache_creation_tokens,
    }

    persisted_review_id: uuid.UUID | None = review_id
    if persist:
        # _persist_review handles both modes: UPDATE the pre-claimed row
        # if review_id is set, otherwise INSERT a fresh 'done' row.
        persisted_review_id = await _persist_review(
            conn=conn,
            project_id=project_id,
            branch=branch,
            before_sha=before,
            after_sha=after,
            summary=summary,
            severity_counts=severity_counts,
            token_usage=token_usage,
            checklist_version=checklist_version,
            findings=findings,
            review_id=review_id,
        )

    return ReviewResult(
        review_id=persisted_review_id,
        summary=summary,
        findings=findings,
        severity_counts=severity_counts,
        token_usage=token_usage,
        raw_response=response.text,
    )


# ── Persistence ──────────────────────────────────────────────────────────
async def _persist_review(
    *,
    conn: asyncpg.Connection,
    project_id: str,
    branch: str,
    before_sha: str,
    after_sha: str,
    summary: str,
    severity_counts: dict[str, int],
    token_usage: dict[str, int],
    checklist_version: int,
    findings: Sequence[Finding],
    review_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Finalise a review row.

    Two modes:
      * `review_id` given (production path) — UPDATE the pre-existing
        pending/running row to 'done' with the LLM outputs. This is what
        the polling worker uses since Day 5: `_review_push` atomically
        claims the row first, then this function fills it in.
      * `review_id` None (ad-hoc, tests, dry-run) — INSERT a fresh row.
        Kept for backward compatibility with any code path that doesn't
        pre-claim.

    Findings get inserted in a separate executemany either way.
    """
    if review_id is None:
        # Legacy / standalone path — used by tests and any caller that
        # doesn't pre-claim. INSERT a new 'done' row.
        review_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO reviews (
                id, project_id, branch, before_sha, after_sha,
                status, summary, severity_counts, token_usage,
                checklist_version, batch_mode, completed_at
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, $5,
                'done', $6, $7, $8,
                $9, 'batch', now()
            )
            """,
            review_id,
            project_id,
            branch,
            before_sha,
            after_sha,
            summary,
            severity_counts,
            token_usage,
            checklist_version,
        )
    else:
        # Production path — fill in the pending row claimed earlier.
        # We don't touch project_id/branch/before_sha/after_sha — they
        # were set at claim time and re-asserting them risks masking a
        # caller-side mismatch bug.
        await conn.execute(
            """
            UPDATE reviews
               SET status = 'done',
                   summary = $2,
                   severity_counts = $3,
                   token_usage = $4,
                   checklist_version = $5,
                   completed_at = now()
             WHERE id = $1::uuid
            """,
            review_id,
            summary,
            severity_counts,
            token_usage,
            checklist_version,
        )

    if findings:
        records = [
            (
                uuid.uuid4(),
                review_id,
                f.severity,
                f.category,
                f.file_path,
                f.start_line,
                f.end_line,
                f.message,
                f.suggestion,
                f.rule_id,
            )
            for f in findings
        ]
        await conn.executemany(
            """
            INSERT INTO review_findings (
                id, review_id, severity, category, file_path,
                start_line, end_line, message, suggestion, rule_id
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, $5,
                $6, $7, $8, $9, $10
            )
            """,
            records,
        )

    return review_id


__all__ = [
    "Finding",
    "ReviewParseError",
    "ReviewResult",
    "SYSTEM_PROMPT",
    "parse_review_response",
    "review_diff",
    "run_review_for_push",
]
