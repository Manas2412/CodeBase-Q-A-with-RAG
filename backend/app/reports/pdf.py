"""ReportLab PDF renderer for a single review.

Layout
======

  1. Header band  — project name + review timestamp
  2. Meta strip   — branch · before → after SHA · severity counts · tokens
  3. Summary      — Claude's natural-language verdict
  4. Commits      — attribution table (SHA · author · date · subject)
  5. Findings     — grouped by severity (critical → info), each with:
       • severity + category tag
       • file:line
       • message
       • flagged code (from the diff)
       • suggested fix (as code)
       • suggestion (as prose)

Design notes
============

* Uses ReportLab Platypus (flowables + SimpleDocTemplate) — the code stays
  declarative instead of manually placing coordinates.
* Colour palette mirrors the web dashboard's severity tokens so the two
  surfaces feel like the same product.
* Long code snippets are wrapped in a `Preformatted` flowable with a soft
  background — same convention as the web card.
* Empty sections (no summary, no commits, no findings) drop out entirely
  rather than rendering "None" placeholders.
"""

from __future__ import annotations

import datetime
import io
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ── Palette ─────────────────────────────────────────────────────────────
#: Indigo brand accent — matches --primary in the web app.
BRAND = colors.HexColor("#4f46e5")
TEXT = colors.HexColor("#1e1e1e")
MUTED = colors.HexColor("#6b7280")
BORDER = colors.HexColor("#e5e7eb")
CODE_BG = colors.HexColor("#f6f7f9")
ADDED_BG = colors.HexColor("#e8f4ff")   # +… lines in diff snippet
REMOVED_BG = colors.HexColor("#fef2f2")  # -… lines in diff snippet

SEVERITY_COLOURS: dict[str, colors.Color] = {
    "critical": colors.HexColor("#d92626"),
    "major":    colors.HexColor("#e56b1f"),
    "minor":    colors.HexColor("#c98a00"),
    "info":     colors.HexColor("#2064c8"),
}
SEVERITY_ORDER = ["critical", "major", "minor", "info"]


# ── Style helpers ───────────────────────────────────────────────────────
def _build_styles():
    """Base styles derived from ReportLab's default sheet.

    Keeping them in a closure means test-mode + prod-mode share the same
    typography without a module-level singleton — safer under pytest's
    parallel test discovery.
    """
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "title", parent=base["Title"],
            fontSize=18, leading=22, textColor=TEXT, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"],
            fontSize=9, leading=12, textColor=MUTED, spaceAfter=8,
        ),
        "section": ParagraphStyle(
            "section", parent=base["Heading2"],
            fontSize=11, leading=14, textColor=BRAND,
            fontName="Helvetica-Bold",
            spaceBefore=14, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"],
            fontSize=10, leading=14, textColor=TEXT, alignment=TA_LEFT,
        ),
        "muted": ParagraphStyle(
            "muted", parent=base["Normal"],
            fontSize=8, leading=11, textColor=MUTED,
        ),
        "code": ParagraphStyle(
            "code", parent=base["Code"],
            fontSize=8, leading=11, textColor=TEXT,
            backColor=CODE_BG, borderPadding=4,
        ),
        "codeAdded": ParagraphStyle(
            "codeAdded", parent=base["Code"],
            fontSize=8, leading=11, textColor=TEXT,
            backColor=ADDED_BG, borderPadding=4,
        ),
        "codeRemoved": ParagraphStyle(
            "codeRemoved", parent=base["Code"],
            fontSize=8, leading=11, textColor=colors.HexColor("#7a1a1a"),
            backColor=REMOVED_BG, borderPadding=4,
        ),
        # Severity-specific paragraph styles built lazily
    }
    return styles


def _short_sha(sha: str | None) -> str:
    if not sha:
        return "—"
    return sha[:8]


def _format_ts(iso: str | None) -> str:
    """ISO string → 'Jun 24, 2026 · 12:41 UTC'."""
    if not iso:
        return "—"
    try:
        # asyncpg gives us tz-aware datetimes; the API JSON-encodes as ISO strings.
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y · %H:%M UTC")
    except (ValueError, TypeError):
        return iso  # give up — better to show the raw string than crash


def _escape_html(text: str | None) -> str:
    """Cheap HTML-escape for the paragraph markup (ReportLab reads markup
    like <b>, <i>, <font>, but blows up on stray < and &)."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── Section builders ────────────────────────────────────────────────────
def _header(review, project, styles):
    title = project.get("name") or "Code review"
    return [
        Paragraph(f"<b>Code Review</b> · {_escape_html(title)}", styles["title"]),
        Paragraph(
            f"Generated {_format_ts(review.get('completed_at') or review.get('created_at'))}",
            styles["subtitle"],
        ),
        HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceBefore=2, spaceAfter=10),
    ]


def _meta_strip(review, styles):
    """One-row table: branch · before → after · severity counts · tokens."""
    branch = review.get("branch", "—")
    before = _short_sha(review.get("before_sha"))
    after = _short_sha(review.get("after_sha"))
    counts = review.get("severity_counts") or {}
    tokens = review.get("token_usage") or {}

    sev_bits = []
    for k in SEVERITY_ORDER:
        n = counts.get(k, 0)
        if n:
            colour = SEVERITY_COLOURS[k].hexval()
            sev_bits.append(
                f'<font color="{colour}"><b>{n}</b></font> {k}'
            )
    sev_text = " · ".join(sev_bits) or '<font color="#2064c8">No findings</font>'

    tok_bits = []
    if tokens.get("input"):
        tok_bits.append(f"{tokens['input']:,} in")
    if tokens.get("output"):
        tok_bits.append(f"{tokens['output']:,} out")
    if tokens.get("cache_read"):
        tok_bits.append(f"{tokens['cache_read']:,} cached")
    tok_text = " · ".join(tok_bits) or "—"

    rows = [
        [
            Paragraph("<b>Branch</b>", styles["muted"]),
            Paragraph(f"<b>Range</b>", styles["muted"]),
            Paragraph("<b>Findings</b>", styles["muted"]),
            Paragraph("<b>Tokens</b>", styles["muted"]),
        ],
        [
            Paragraph(_escape_html(branch), styles["body"]),
            Paragraph(f'<font face="Courier">{before} → {after}</font>', styles["body"]),
            Paragraph(sev_text, styles["body"]),
            Paragraph(tok_text, styles["body"]),
        ],
    ]

    tbl = Table(rows, colWidths=[1.2 * inch, 1.7 * inch, 2.2 * inch, 1.4 * inch])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
    ]))
    return [tbl, Spacer(1, 6)]


def _summary_section(review, styles):
    text = review.get("summary")
    if not text:
        return []
    return [
        Paragraph("Summary", styles["section"]),
        Paragraph(_escape_html(text), styles["body"]),
    ]


def _commits_section(commits, styles):
    if not commits:
        return []
    rows = [[
        Paragraph("<b>SHA</b>", styles["muted"]),
        Paragraph("<b>Author</b>", styles["muted"]),
        Paragraph("<b>Date</b>", styles["muted"]),
        Paragraph("<b>Subject</b>", styles["muted"]),
    ]]
    for c in commits:
        rows.append([
            Paragraph(
                f'<font face="Courier">{_short_sha(c.get("sha"))}</font>',
                styles["body"],
            ),
            Paragraph(_escape_html(c.get("author_name", "—")), styles["body"]),
            Paragraph(_format_ts(c.get("committed_at")), styles["muted"]),
            Paragraph(_escape_html(c.get("subject", "")), styles["body"]),
        ])
    tbl = Table(rows, colWidths=[0.9 * inch, 1.3 * inch, 1.6 * inch, 2.7 * inch])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
    ]))
    return [
        Paragraph("Commits in this review", styles["section"]),
        tbl,
    ]


def _finding_block(finding, styles):
    """One finding as a KeepTogether so it doesn't split across pages."""
    parts = []

    sev = finding.get("severity", "info").lower()
    sev_colour = SEVERITY_COLOURS.get(sev, MUTED).hexval()
    category = finding.get("category", "").lower()

    header_bits = [
        f'<font color="{sev_colour}"><b>{sev.upper()}</b></font>',
        _escape_html(category) if category else "",
    ]
    header_bits = [b for b in header_bits if b]
    location = finding.get("file_path", "")
    if location:
        loc = _escape_html(location)
        if finding.get("start_line") is not None:
            loc += f":{finding['start_line']}"
            if finding.get("end_line") and finding["end_line"] != finding["start_line"]:
                loc += f"–{finding['end_line']}"
        header_bits.append(f'<font face="Courier">{loc}</font>')

    parts.append(Paragraph(" · ".join(header_bits), styles["body"]))
    parts.append(Paragraph(_escape_html(finding.get("message", "")), styles["body"]))

    snippet = finding.get("code_snippet")
    if snippet:
        # Tint the whole block based on the presence of + or - prefixes.
        first_char = snippet.lstrip()[:1]
        style = (
            styles["codeAdded"] if first_char == "+"
            else styles["codeRemoved"] if first_char == "-"
            else styles["code"]
        )
        parts.append(Spacer(1, 2))
        parts.append(Paragraph("<i>Flagged code</i>", styles["muted"]))
        parts.append(Preformatted(snippet, style))

    suggested = finding.get("suggested_code")
    if suggested:
        parts.append(Spacer(1, 2))
        parts.append(Paragraph("<i>Suggested fix</i>", styles["muted"]))
        parts.append(Preformatted(suggested, styles["code"]))

    suggestion = finding.get("suggestion")
    if suggestion:
        parts.append(Spacer(1, 2))
        parts.append(Paragraph(f"<i>Note:</i> {_escape_html(suggestion)}", styles["muted"]))

    parts.append(Spacer(1, 10))
    return KeepTogether(parts)


def _findings_section(findings, styles):
    if not findings:
        return [
            Paragraph("Findings", styles["section"]),
            Paragraph("No findings flagged for this review.", styles["muted"]),
        ]

    # Backend already sorts critical → info via SQL. Preserve that here.
    parts = [Paragraph("Findings", styles["section"])]
    for f in findings:
        parts.append(_finding_block(f, styles))
    return parts


# ── Public API ──────────────────────────────────────────────────────────
def render_review_pdf(
    *,
    review: dict,
    project: dict,
    commits: Iterable[dict],
    findings: Iterable[dict],
    _compressed: bool = True,
) -> bytes:
    """Render a review PDF from plain dicts.

    Parameters
    ----------
    review
        The review row shape returned by GET /reviews/{id} (severity_counts,
        token_usage, summary, before/after_sha, branch, created_at, etc.).
    project
        A project dict with at least `name`. Used in the header.
    commits
        List of Commit dicts (sha, author_name, committed_at, subject).
        Empty → the section is omitted.
    findings
        List of FindingOut dicts. Rendered in the order given.
    _compressed
        Private toggle — default True (production output is compressed like
        every well-behaved PDF). Tests pass False so grepping the raw bytes
        for expected strings works without pulling pypdf into the test deps.

    Returns
    -------
    bytes
        A valid PDF byte string. The caller wraps it in a StreamingResponse.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"Code review — {project.get('name', 'report')}",
        author="Code Review Agent",
        pageCompression=1 if _compressed else 0,
    )
    styles = _build_styles()

    story: list = []
    story += _header(review, project, styles)
    story += _meta_strip(review, styles)
    story += _summary_section(review, styles)
    story += _commits_section(list(commits), styles)
    story += _findings_section(list(findings), styles)

    doc.build(story)
    return buffer.getvalue()
