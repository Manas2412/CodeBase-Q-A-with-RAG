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

    `invoke_model` is the only Bedrock call we make (both chat and embed
    go through it — chat with Anthropic Messages body, embed with Cohere
    body). Pre-loaded with shaped responses so most tests don't need to
    set return_value.

    The mock dispatches on `modelId` keyword: anthropic.* → chat response,
    cohere.* → embed response. Tests can override return_value or
    side_effect to simulate specific cases.
    """
    import json
    from unittest.mock import MagicMock

    # --- Pre-baked response payloads ----------------------------------
    chat_payload = {
        "id": "msg_test_001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "mocked review output"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    embed_payload = {"embeddings": [[0.0] * 1024]}

    def _stream_from(payload: dict) -> MagicMock:
        s = MagicMock(name="body-stream")
        s.read.return_value = json.dumps(payload).encode()
        return s

    def _invoke_model(modelId: str = "", **_: object) -> dict:
        """Route to chat or embed payload based on the model id family."""
        if modelId.startswith("cohere"):
            return {"body": _stream_from(embed_payload)}
        # anthropic.* (or anything else): assume a chat call
        return {"body": _stream_from(chat_payload)}

    client = MagicMock(name="bedrock-runtime")
    client.invoke_model.side_effect = _invoke_model
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
