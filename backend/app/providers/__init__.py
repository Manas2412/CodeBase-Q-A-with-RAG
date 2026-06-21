"""Provider abstraction for source platforms.

Two providers in Phase 1: OpenForge (Tuleap) and GitHub. Adding a third
(GitLab, Bitbucket, self-hosted Gitea) is one new module plus a registry
entry — `BaseProvider` defines the contract.
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.providers.base import BaseProvider, Branch, ParsedRepo, ProviderError
from app.providers.github import GitHubProvider
from app.providers.openforge import OpenForgeProvider


class UnknownProviderError(ProviderError):
    """The URL doesn't match any registered provider."""


_PROVIDERS: dict[str, BaseProvider] = {
    "openforge": OpenForgeProvider(),
    "github": GitHubProvider(),
}


def detect_provider(url: str) -> str:
    """Return the provider name for a repo URL.

    Recognises OpenForge (Tuleap) and GitHub. Raises UnknownProviderError
    if no registered provider claims the URL.
    """
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path or ""

    if "openforge.gov.in" in host or "/plugins/git/" in path:
        return "openforge"
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    raise UnknownProviderError(
        f"Could not determine provider from URL: {url!r}. "
        "Supported providers (Phase 1): openforge, github."
    )


def get_provider(url: str) -> BaseProvider:
    """Return the singleton Provider instance for a URL."""
    return _PROVIDERS[detect_provider(url)]


__all__ = [
    "BaseProvider",
    "Branch",
    "ParsedRepo",
    "ProviderError",
    "UnknownProviderError",
    "detect_provider",
    "get_provider",
]
