"""Review pipeline — diff parsing, context retrieval, Bedrock calls, persistence.

Phase 1 Week 2 lands the LLM-facing pieces:
  bedrock_client   — boto3 wrapper for Converse (LLM) + InvokeModel (embed)
  diff_parser      — git diff -> structured DiffHunks
  context_builder  — pulls related chunks from pgvector with a token budget
  reviewer         — assembles the prompt, calls Opus, parses findings
  checklist        — versioned rule sets injected into the prompt
"""

from app.review.bedrock_client import (
    MAX_OUTPUT_TOKENS,
    BedrockClient,
    BedrockError,
    ChatResponse,
    get_bedrock_client,
)
from app.review.checklist import (
    DEFAULT_CHECKLIST,
    DEFAULT_CHECKLIST_VERSION,
    Rule,
    format_checklist,
)
from app.review.context_builder import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    DEFAULT_SEMANTIC_TOP_K,
    ContextChunk,
    build_context,
)
from app.review.diff_parser import (
    DIFF_CONTEXT_LINES,
    CommitInfo,
    DiffHunk,
    commits_between,
    diff_between,
)
from app.review.reviewer import (
    Finding,
    ReviewParseError,
    ReviewResult,
    parse_review_response,
    review_diff,
    run_review_for_push,
)

__all__ = [
    # bedrock
    "MAX_OUTPUT_TOKENS",
    "BedrockClient",
    "BedrockError",
    "ChatResponse",
    "get_bedrock_client",
    # diff
    "DIFF_CONTEXT_LINES",
    "CommitInfo",
    "DiffHunk",
    "commits_between",
    "diff_between",
    # context
    "ContextChunk",
    "DEFAULT_CONTEXT_BUDGET_TOKENS",
    "DEFAULT_SEMANTIC_TOP_K",
    "build_context",
    # checklist
    "DEFAULT_CHECKLIST",
    "DEFAULT_CHECKLIST_VERSION",
    "Rule",
    "format_checklist",
    # reviewer
    "Finding",
    "ReviewParseError",
    "ReviewResult",
    "parse_review_response",
    "review_diff",
    "run_review_for_push",
]
