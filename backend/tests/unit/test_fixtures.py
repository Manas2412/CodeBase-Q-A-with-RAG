"""Verify the conftest fixtures actually do what they claim."""

from pathlib import Path

import pytest


def test_mock_bedrock_converse_returns_shaped_response(mock_bedrock):
    """A test that wants Claude's reply gets a Claude-shaped dict back."""
    response = mock_bedrock.converse(modelId="x", messages=[])
    assert "output" in response
    assert response["output"]["message"]["role"] == "assistant"
    assert response["output"]["message"]["content"][0]["text"]


def test_mock_bedrock_invoke_model_returns_embedding(mock_bedrock):
    """A test that wants Cohere Embed v3 gets a body-with-read() response."""
    import json

    response = mock_bedrock.invoke_model(modelId="cohere.embed-multilingual-v3", body="{}")
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
