"""Tests for app.reports.pdf.

We test the renderer's public function `render_review_pdf` — feed
handcrafted dicts, assert the returned bytes are a valid PDF and contain
the strings we expect (project name, severity labels, summary text).

Byte-checking is a very light-touch approach — we don't render pixel-diffs
or read the PDF's page structure. That's fine: the renderer is
deterministic given deterministic input, and heavier assertions bring in
pypdf just to double-check that reportlab did its job.
"""

from __future__ import annotations

from app.reports import render_review_pdf


def _sample_review() -> dict:
    return {
        "branch": "main",
        "before_sha": "aaaa1111aaaa1111",
        "after_sha": "bbbb2222bbbb2222",
        "status": "done",
        "severity_counts": {"critical": 1, "major": 2, "minor": 0, "info": 3},
        "token_usage": {
            "input": 32_535,
            "output": 1_836,
            "total": 34_371,
            "cache_read": 800,
        },
        "summary": "This diff refactors the AI customer support email service.",
        "created_at": "2026-06-24T09:35:00+00:00",
        "completed_at": "2026-06-24T09:35:14+00:00",
    }


def _sample_project() -> dict:
    return {
        "name": "AI-Customer-Support-Service",
        "provider": "github",
        "repo_url": "https://github.com/example/AI-Customer-Support-Service.git",
    }


def _sample_commits() -> list[dict]:
    return [
        {
            "sha": "5f553a6cccccccccc",
            "author_name": "Manas Sisodia",
            "author_email": "manas@example.invalid",
            "committed_at": "2026-06-23T12:30:00+00:00",
            "subject": "fixed errors",
            "source": "poll",
        }
    ]


def _sample_findings() -> list[dict]:
    return [
        {
            "severity": "critical",
            "category": "security",
            "file_path": "whitelist.yaml",
            "start_line": 2,
            "end_line": 2,
            "message": "A real email address is hardcoded in the repository.",
            "code_snippet": "+  - user@example.invalid",
            "suggested_code": "  - team-alias@example.invalid",
            "suggestion": "Move the whitelist into an env-configured file.",
            "rule_id": "SEC-001",
        },
        {
            "severity": "minor",
            "category": "testing",
            "file_path": "support.py",
            "start_line": None,
            "end_line": None,
            "message": "No tests are included for the new methods.",
            "code_snippet": None,
            "suggested_code": None,
            "suggestion": "Add pytest coverage for process_email.",
            "rule_id": None,
        },
    ]


def test_render_review_pdf_returns_valid_pdf_bytes():
    """Bytes must start with the PDF magic header and end with %%EOF."""
    pdf = render_review_pdf(
        review=_sample_review(),
        project=_sample_project(),
        commits=_sample_commits(),
        findings=_sample_findings(),
    )
    assert pdf.startswith(b"%PDF-")
    # reportlab writes %%EOF as the last non-whitespace token
    assert b"%%EOF" in pdf[-32:]
    # Non-trivial size — a header-only PDF is <1KB; ours should be several KB
    assert len(pdf) > 2_000


def test_render_pdf_includes_project_name_and_summary():
    """Content sanity — the project name + summary text land in the PDF stream.

    Compression is off for this assertion so latin-1 decoding hits the raw
    paragraph streams. Production output stays compressed.
    """
    pdf = render_review_pdf(
        review=_sample_review(),
        project=_sample_project(),
        commits=_sample_commits(),
        findings=_sample_findings(),
        _compressed=False,
    )
    text = pdf.decode("latin-1", errors="ignore")
    assert "AI-Customer-Support-Service" in text
    # The summary text lives in one of the paragraph streams
    assert "refactors the AI customer support" in text


def test_render_pdf_handles_review_without_findings():
    """A clean review still renders — findings section shows the empty-state text."""
    review = _sample_review()
    review["severity_counts"] = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    pdf = render_review_pdf(
        review=review,
        project=_sample_project(),
        commits=[],
        findings=[],
        _compressed=False,
    )
    assert pdf.startswith(b"%PDF-")
    text = pdf.decode("latin-1", errors="ignore")
    assert "No findings" in text


def test_render_pdf_handles_null_optional_fields():
    """Missing summary, empty commits, no findings — must not raise."""
    review = _sample_review()
    review["summary"] = None
    review["completed_at"] = None
    pdf = render_review_pdf(
        review=review,
        project=_sample_project(),
        commits=[],
        findings=[],
    )
    assert pdf.startswith(b"%PDF-")


def test_render_pdf_finding_with_html_special_chars_does_not_break_output():
    """Angle brackets + ampersands in messages must not corrupt the paragraph
    markup (reportlab reads Paragraph text as a mini-HTML)."""
    findings = [
        {
            "severity": "major",
            "category": "correctness",
            "file_path": "app/main.py",
            "start_line": 10,
            "end_line": 10,
            "message": "Compare with <string> & handle None safely.",
            "code_snippet": "+  if x < 5 && y > 3:",
            "suggested_code": None,
            "suggestion": "Rewrite the < & > checks with parenthesised chains.",
            "rule_id": None,
        }
    ]
    pdf = render_review_pdf(
        review=_sample_review(),
        project=_sample_project(),
        commits=[],
        findings=findings,
    )
    assert pdf.startswith(b"%PDF-")
