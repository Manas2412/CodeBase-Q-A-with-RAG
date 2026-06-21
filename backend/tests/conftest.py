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
def _git(*args: str, cwd: Path) -> None:
    """Run git in cwd; capture+ignore output."""
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def synthetic_repo(tmp_path: Path) -> Path:
    """Tiny single-branch repo (`main`) with one commit. For cloner / chunker tests."""
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()

    _git("git", "init", "-q", "-b", "main", cwd=repo)
    _git("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _git("git", "config", "user.name", "Test Author", cwd=repo)
    _git("git", "config", "commit.gpgsign", "false", cwd=repo)

    (repo / "README.md").write_text("# synthetic-repo\n")
    (repo / "hello.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return f'hello, {name}'\n"
    )
    _git("git", "add", ".", cwd=repo)
    _git("git", "commit", "-q", "-m", "initial commit", cwd=repo)

    return repo


@pytest.fixture
def multi_branch_repo(tmp_path: Path) -> Path:
    """Synthetic repo with three branches: main (default), dev, uat.

    Use for tests exercising provider.list_branches() and the clone manager
    against a real git remote via a `file://` URL — no network, fully isolated.
    """
    repo = tmp_path / "multi-branch-repo"
    repo.mkdir()

    _git("git", "init", "-q", "-b", "main", cwd=repo)
    _git("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _git("git", "config", "user.name", "Test Author", cwd=repo)
    _git("git", "config", "commit.gpgsign", "false", cwd=repo)

    (repo / "README.md").write_text("# multi-branch repo\n")
    _git("git", "add", ".", cwd=repo)
    _git("git", "commit", "-q", "-m", "initial commit on main", cwd=repo)

    for branch in ("dev", "uat"):
        _git("git", "checkout", "-q", "-b", branch, cwd=repo)
        (repo / f"{branch}.md").write_text(f"# {branch} branch marker\n")
        _git("git", "add", ".", cwd=repo)
        _git("git", "commit", "-q", "-m", f"add {branch} marker", cwd=repo)

    _git("git", "checkout", "-q", "main", cwd=repo)
    return repo


# ── Storage paths redirected to tmp ───────────────────────────────────────
@pytest.fixture
def temp_repos_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CODEREVIEW_REPOS_DIR at a fresh pytest tmp_path.

    Lets clone_manager tests write to disk without polluting ~/.codereview/repos.
    """
    monkeypatch.setenv("CODEREVIEW_REPOS_DIR", str(tmp_path))
    return tmp_path
