"""Base Provider contract + shared dataclasses.

A Provider knows how to:
  • parse a URL into structured fields (provider, org/project, repo)
  • inject an auth token into the clone URL
  • discover branches without cloning (`git ls-remote --heads --symref`)

This contract is what `app/storage/clone_manager.py`, the polling agent
(Week 3), and the `POST /projects/probe` endpoint all rely on.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Anything a provider can't handle — bad URL, auth failure, network."""


@dataclass(frozen=True)
class ParsedRepo:
    """Structured pieces extracted from a repo URL."""

    provider: str        # 'openforge' | 'github'
    host: str            # 'openforge.gov.in' | 'github.com'
    org_or_project: str  # 'sports' (tuleap project) | 'octocat' (gh org)
    repo: str            # 'gms-management-service-api' | 'Hello-World'
    raw_url: str         # original URL as the user pasted it
    # provider-specific extras (e.g. tuleap path prefix) live here
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Branch:
    """A single branch as returned by `git ls-remote --heads`."""

    name: str
    sha: str
    is_default: bool = False


class BaseProvider(ABC):
    """Abstract base every provider implements."""

    #: Short name used in URLs, env vars, DB rows.
    name: str = ""

    #: Env var that holds the provider's access token.
    token_env: str = ""

    # ── URL handling ────────────────────────────────────────────────────
    @abstractmethod
    def parse(self, url: str) -> ParsedRepo:
        """Validate the URL and extract structured fields. Raise ProviderError on bad input."""

    @abstractmethod
    def auth_url(self, parsed: ParsedRepo, token: str | None) -> str:
        """Return a clone URL with the token injected, or the raw URL when no token."""

    # ── Token helpers ──────────────────────────────────────────────────
    def get_token(self) -> str | None:
        """Read the provider's token from the environment, returning None if unset."""
        value = os.getenv(self.token_env, "").strip()
        return value or None

    # ── Branch discovery via `git ls-remote` ────────────────────────────
    async def list_branches(
        self, parsed: ParsedRepo, token: str | None
    ) -> tuple[str, list[Branch]]:
        """Return (default_branch, branches[]) by querying the remote.

        Implementation is shared by all providers — provider-specific behaviour
        lives in `auth_url()` (where the token gets baked into the clone URL).
        """
        url = self.auth_url(parsed, token)
        cmd = ["git", "ls-remote", "--heads", "--symref", url]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ProviderError(_redact(stderr.decode(errors="replace"), token).strip())

        return _parse_ls_remote(stdout.decode(errors="replace"))


# ── Helpers (module-private) ──────────────────────────────────────────────
def _parse_ls_remote(output: str) -> tuple[str, list[Branch]]:
    """Turn `git ls-remote --heads --symref` output into (default, branches).

    Sample output:
        ref: refs/heads/main	HEAD
        abc123…	refs/heads/dev
        def456…	refs/heads/main
        ghi789…	refs/heads/uat
    """
    default = ""
    branches: list[Branch] = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("ref:"):
            # `ref: refs/heads/main\tHEAD`
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                default = parts[1].removeprefix("refs/heads/")
            continue
        # `<sha>\trefs/heads/<branch>`
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
            name = parts[1].removeprefix("refs/heads/")
            branches.append(Branch(name=name, sha=parts[0], is_default=False))

    if not default and branches:
        # Some servers omit the symref; fall back to common defaults.
        guess = next(
            (b.name for b in branches if b.name in ("main", "master", "trunk")),
            branches[0].name,
        )
        default = guess

    # Mark default flag now that we know which one it is
    branches = [
        Branch(name=b.name, sha=b.sha, is_default=(b.name == default))
        for b in branches
    ]
    return default, branches


def _redact(text: str, token: str | None) -> str:
    """Don't echo the access token back to the API caller in error messages."""
    if not token:
        return text
    return text.replace(token, "<TOKEN>")
