"""Shared pytest fixtures for the codebase review agent.

Fixtures defined here are auto-discovered by pytest and available to any
test under backend/tests/ without explicit import.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv

# ── Make backend/app importable from anywhere under tests/ ────────────────
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Load backend/.env once at collection so tests that read DATABASE_URL etc.
# pick up local docker container settings automatically.
load_dotenv(BACKEND_ROOT / ".env")


# ── Mock Bedrock runtime client ───────────────────────────────────────────
@pytest.fixture
def mock_bedrock() -> MagicMock:
    """A MagicMock standing in for boto3's bedrock-runtime client.

    Pre-shaped responses cover the two calls the review pipeline makes:
      • converse() — returns a fake Claude reply
      • invoke_model() — returns a fake 1024-dim Cohere embedding

    Tests can override return_value on the mock to simulate specific responses.
    """
    client = MagicMock(name="bedrock-runtime")

    # Default Converse response (Claude Opus shape)
    client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "mocked review output"}],
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        "stopReason": "end_turn",
    }

    # Default InvokeModel response (Cohere Embed v3 shape).
    # Body is a stream-like object; tests typically call body.read().
    embedding_stream = MagicMock()
    embedding_stream.read.return_value = (
        b'{"embeddings": [[' + b"0.0," * 1023 + b"0.0]]}"
    )
    client.invoke_model.return_value = {"body": embedding_stream}

    return client


# ── Synthetic git repo on disk ────────────────────────────────────────────
@pytest.fixture
def synthetic_repo(tmp_path: Path) -> Path:
    """Build a tiny real git repo in a pytest tmp_path. Returns its path.

    Use for tests that exercise:
      - app/ingestion/cloner.py (walking files, supported extensions)
      - app/providers/* (URL parsing — not network ops)
      - future diff parser (commit ranges, force-push detection)
    """
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()

    def _run(*args: str) -> None:
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    _run("git", "init", "-q", "-b", "main")
    _run("git", "config", "user.email", "test@example.invalid")
    _run("git", "config", "user.name", "Test Author")
    _run("git", "config", "commit.gpgsign", "false")

    (repo / "README.md").write_text("# synthetic-repo\n")
    (repo / "hello.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return f'hello, {name}'\n"
    )
    _run("git", "add", ".")
    _run("git", "commit", "-q", "-m", "initial commit")

    return repo
