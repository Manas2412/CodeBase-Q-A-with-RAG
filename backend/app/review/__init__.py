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

__all__ = [
    "MAX_OUTPUT_TOKENS",
    "BedrockClient",
    "BedrockError",
    "ChatResponse",
    "get_bedrock_client",
]
