"""Verify the conftest fixtures actually do what they claim."""

import json
from pathlib import Path


def test_mock_bedrock_chat_returns_anthropic_shaped_response(mock_bedrock):
    """An anthropic.* modelId routes to the chat payload (Anthropic Messages API)."""
    response = mock_bedrock.invoke_model(modelId="us.anthropic.claude-opus-4-6-v1")
    payload = json.loads(response["body"].read())
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["content"][0]["type"] == "text"
    assert payload["content"][0]["text"] == "mocked review output"
    assert payload["stop_reason"] == "end_turn"
    assert "usage" in payload


def test_mock_bedrock_embed_returns_cohere_shaped_response(mock_bedrock):
    """A cohere.* modelId routes to a 1024-dim embedding payload."""
    response = mock_bedrock.invoke_model(modelId="cohere.embed-multilingual-v3")
    payload = json.loads(response["body"].read())
    assert "embeddings" in payload
    assert len(payload["embeddings"]) == 1
    assert len(payload["embeddings"][0]) == 1024  # locked dim


def test_synthetic_repo_is_a_real_git_repo(synthetic_repo: Path):
    """synthetic_repo lays down a working git repo with one commit."""
    assert (synthetic_repo / ".git").is_dir()
    assert (synthetic_repo / "README.md").read_text() == "# synthetic-repo\n"
    assert (synthetic_repo / "hello.py").exists()


def test_synthetic_repo_has_initial_commit(synthetic_repo: Path):
    """Confirms commits + author config landed cleanly."""
    import subprocess

    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=synthetic_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert "initial commit" in lines[0]
