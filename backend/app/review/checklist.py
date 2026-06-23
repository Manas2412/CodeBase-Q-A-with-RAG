"""Review checklist — what the senior-tech-lead reviewer looks for.

Phase 1 ships a hardcoded default. The checklist UI (Phase 1 Week 5)
adds CRUD on top with database-backed versioning. Until then this is the
single source of truth for rules.

Each rule has:
  • id          — short stable key (used in findings.rule_id for grouping)
  • category    — security | correctness | performance | design | testing | documentation
  • severity_default — the severity to use if the LLM doesn't specify one
  • title       — short human-readable rule name
  • description — what to look for (gets sent to the LLM)
"""

from __future__ import annotations

from typing import TypedDict


class Rule(TypedDict):
    id: str
    category: str
    severity_default: str  # 'info' | 'minor' | 'major' | 'critical'
    title: str
    description: str


DEFAULT_CHECKLIST_VERSION: int = 1


DEFAULT_CHECKLIST: list[Rule] = [
    # ── Security ────────────────────────────────────────────────────────
    {
        "id": "no-hardcoded-secrets",
        "category": "security",
        "severity_default": "critical",
        "title": "No hardcoded credentials",
        "description": (
            "Flag any string that looks like an API key, token, password, "
            "private key, or other credential committed to source. Includes "
            "AWS keys (AKIA…), JWT secrets, DB connection strings with "
            "embedded passwords, OAuth client secrets, etc."
        ),
    },
    {
        "id": "no-sql-injection",
        "category": "security",
        "severity_default": "critical",
        "title": "No SQL injection vectors",
        "description": (
            "User-controlled values must reach the database through "
            "parameterised queries / prepared statements, never through "
            "string concatenation or f-strings."
        ),
    },
    {
        "id": "no-shell-injection",
        "category": "security",
        "severity_default": "critical",
        "title": "No shell injection vectors",
        "description": (
            "Avoid passing user input to shell=True subprocess calls or "
            "os.system. If the shell is necessary, sanitise via shlex.quote."
        ),
    },
    {
        "id": "input-validation",
        "category": "security",
        "severity_default": "major",
        "title": "Inputs are validated",
        "description": (
            "External inputs (HTTP request bodies, query params, file "
            "uploads, third-party API responses) should have explicit "
            "validation — type checks, length caps, allow-lists where "
            "appropriate."
        ),
    },
    # ── Correctness ─────────────────────────────────────────────────────
    {
        "id": "null-edge-cases",
        "category": "correctness",
        "severity_default": "major",
        "title": "Null / empty / missing-key edge cases handled",
        "description": (
            "Operations on Optional values, dict lookups, list[0] accesses "
            "must defend against missing data. Especially flag silent "
            ".get() defaults that mask bugs."
        ),
    },
    {
        "id": "error-handling",
        "category": "correctness",
        "severity_default": "major",
        "title": "Errors handled or propagated, not swallowed",
        "description": (
            "Bare `except:` or `except Exception: pass` swallows real bugs. "
            "Exceptions should be caught at the right boundary and either "
            "logged, retried, or re-raised with context."
        ),
    },
    {
        "id": "race-conditions",
        "category": "correctness",
        "severity_default": "major",
        "title": "No obvious race conditions",
        "description": (
            "Shared mutable state in async/threaded code; check-then-act "
            "patterns; missing locks around critical sections."
        ),
    },
    # ── Performance ─────────────────────────────────────────────────────
    {
        "id": "n-plus-one",
        "category": "performance",
        "severity_default": "major",
        "title": "No N+1 query patterns",
        "description": (
            "A loop that fetches one DB row per iteration should be batched "
            "into a single query with IN / JOIN."
        ),
    },
    {
        "id": "unbounded-loops",
        "category": "performance",
        "severity_default": "major",
        "title": "Loops are bounded",
        "description": (
            "Loops over external data (API pagination, file lines, queue "
            "consumers) should have a max-iteration safeguard so a "
            "misbehaving upstream can't cause runaway work."
        ),
    },
    {
        "id": "sync-in-async",
        "category": "performance",
        "severity_default": "minor",
        "title": "No blocking calls in async functions",
        "description": (
            "Inside `async def`, avoid synchronous I/O (requests.get, "
            "time.sleep, os.read) that would block the event loop. Use "
            "the async sibling (httpx, asyncio.sleep, aiofiles) or "
            "asyncio.to_thread()."
        ),
    },
    # ── Design ──────────────────────────────────────────────────────────
    {
        "id": "single-responsibility",
        "category": "design",
        "severity_default": "minor",
        "title": "Functions and classes do one thing",
        "description": (
            "Functions over ~50 lines or with more than one clear "
            "responsibility should be split. Mixed concerns (e.g., HTTP "
            "parsing + business logic + DB writes in one function) are a "
            "smell."
        ),
    },
    {
        "id": "no-dead-code",
        "category": "design",
        "severity_default": "minor",
        "title": "No newly-introduced dead code",
        "description": (
            "Code that's added but never called, imported but never used, "
            "or commented out should be removed before merge."
        ),
    },
    # ── Testing ─────────────────────────────────────────────────────────
    {
        "id": "tests-for-new-code",
        "category": "testing",
        "severity_default": "minor",
        "title": "New public functions have tests",
        "description": (
            "When a new public function / endpoint / class method is "
            "introduced, the diff should include matching tests. "
            "Internal helpers can be tested via their callers."
        ),
    },
    # ── Documentation ───────────────────────────────────────────────────
    {
        "id": "docstrings-on-public-api",
        "category": "documentation",
        "severity_default": "info",
        "title": "Public functions have docstrings",
        "description": (
            "Public-facing functions (in __init__ exports, on REST handlers, "
            "in service-layer classes) need at least a one-line docstring "
            "describing intent and return."
        ),
    },
]


def format_checklist(rules: list[Rule]) -> str:
    """Render a checklist as a text block for the LLM prompt.

    Used as the `cache_prefix` for BedrockClient.chat() so the rules
    block gets cached at 90% discount across calls within the 5-min TTL.
    """
    lines: list[str] = ["# REVIEW CHECKLIST", ""]
    by_category: dict[str, list[Rule]] = {}
    for r in rules:
        by_category.setdefault(r["category"], []).append(r)

    for category, items in by_category.items():
        lines.append(f"## {category.title()}")
        for rule in items:
            lines.append(
                f"- **{rule['id']}** ({rule['severity_default']}): "
                f"{rule['title']}"
            )
            lines.append(f"    {rule['description']}")
        lines.append("")  # blank between categories
    return "\n".join(lines)


__all__ = [
    "DEFAULT_CHECKLIST",
    "DEFAULT_CHECKLIST_VERSION",
    "Rule",
    "format_checklist",
]
