"""Tests for the provider abstraction.

Coverage:
  • detect_provider — URL → provider name
  • OpenForgeProvider.parse + auth_url
  • GitHubProvider.parse + auth_url
  • base._parse_ls_remote — symref output → (default, branches[])
  • list_branches against a real synthetic repo via file:// (no network)
"""

from __future__ import annotations

import pytest

from app.providers import (
    UnknownProviderError,
    detect_provider,
    get_provider,
)
from app.providers.base import (
    BaseProvider,
    Branch,
    ParsedRepo,
    ProviderError,
    _parse_ls_remote,
)
from app.providers.github import GitHubProvider
from app.providers.openforge import OpenForgeProvider


# ── detect_provider ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://openforge.gov.in/plugins/git/sports/gms-api.git", "openforge"),
        ("https://openforge.gov.in/plugins/git/sports/gms-api", "openforge"),
        ("https://github.com/octocat/Hello-World", "github"),
        ("https://github.com/octocat/Hello-World.git", "github"),
        ("https://www.github.com/octocat/Hello-World", "github"),
    ],
)
def test_detect_provider_recognises_known_hosts(url: str, expected: str):
    assert detect_provider(url) == expected


def test_detect_provider_raises_on_unknown():
    with pytest.raises(UnknownProviderError, match="openforge, github"):
        detect_provider("https://example.com/some/repo")


def test_get_provider_returns_singleton_for_same_host():
    p1 = get_provider("https://github.com/a/b")
    p2 = get_provider("https://github.com/c/d")
    assert p1 is p2


# ── OpenForge ────────────────────────────────────────────────────────────
class TestOpenForgeProvider:
    @pytest.fixture
    def provider(self) -> OpenForgeProvider:
        return OpenForgeProvider()

    def test_parse_extracts_project_and_repo(self, provider: OpenForgeProvider):
        url = "https://openforge.gov.in/plugins/git/sports/gms-management-service-api.git"
        parsed = provider.parse(url)
        assert parsed.provider == "openforge"
        assert parsed.host == "openforge.gov.in"
        assert parsed.org_or_project == "sports"
        assert parsed.repo == "gms-management-service-api"

    def test_parse_tolerates_missing_dot_git(self, provider: OpenForgeProvider):
        parsed = provider.parse(
            "https://openforge.gov.in/plugins/git/sports/gms-api"
        )
        assert parsed.repo == "gms-api"

    def test_parse_raises_on_malformed_path(self, provider: OpenForgeProvider):
        with pytest.raises(ProviderError, match="plugins/git"):
            provider.parse("https://openforge.gov.in/some-other-path")

    def test_auth_url_injects_tlp_token(self, provider: OpenForgeProvider):
        parsed = provider.parse(
            "https://openforge.gov.in/plugins/git/sports/gms-api.git"
        )
        url = provider.auth_url(parsed, "tlp.k1.abc123")
        assert (
            url
            == "https://review-bot:tlp.k1.abc123@openforge.gov.in/plugins/git/sports/gms-api.git"
        )

    def test_auth_url_without_token_returns_plain_https(
        self, provider: OpenForgeProvider
    ):
        parsed = provider.parse(
            "https://openforge.gov.in/plugins/git/sports/gms-api.git"
        )
        url = provider.auth_url(parsed, None)
        assert url == "https://openforge.gov.in/plugins/git/sports/gms-api.git"

    def test_token_env_var_name(self, provider: OpenForgeProvider):
        assert provider.token_env == "OPENFORGE_TOKEN"


# ── GitHub ───────────────────────────────────────────────────────────────
class TestGitHubProvider:
    @pytest.fixture
    def provider(self) -> GitHubProvider:
        return GitHubProvider()

    def test_parse_extracts_org_and_repo(self, provider: GitHubProvider):
        parsed = provider.parse("https://github.com/octocat/Hello-World")
        assert parsed.provider == "github"
        assert parsed.host == "github.com"
        assert parsed.org_or_project == "octocat"
        assert parsed.repo == "Hello-World"

    def test_parse_tolerates_dot_git_suffix(self, provider: GitHubProvider):
        parsed = provider.parse("https://github.com/octocat/Hello-World.git")
        assert parsed.repo == "Hello-World"

    def test_parse_rejects_non_github_host(self, provider: GitHubProvider):
        with pytest.raises(ProviderError, match="github.com"):
            provider.parse("https://example.com/a/b")

    def test_auth_url_injects_pat(self, provider: GitHubProvider):
        parsed = provider.parse("https://github.com/octocat/Hello-World")
        url = provider.auth_url(parsed, "ghp_abc123")
        assert url == "https://ghp_abc123@github.com/octocat/Hello-World.git"

    def test_auth_url_without_token_returns_plain_https(
        self, provider: GitHubProvider
    ):
        parsed = provider.parse("https://github.com/octocat/Hello-World")
        url = provider.auth_url(parsed, None)
        assert url == "https://github.com/octocat/Hello-World.git"

    def test_token_env_var_name(self, provider: GitHubProvider):
        assert provider.token_env == "GITHUB_TOKEN"


# ── ls-remote parsing ────────────────────────────────────────────────────
def test_parse_ls_remote_picks_default_from_symref():
    output = (
        "ref: refs/heads/main\tHEAD\n"
        "aaaa111\trefs/heads/dev\n"
        "bbbb222\trefs/heads/main\n"
        "cccc333\trefs/heads/uat\n"
    )
    default, branches = _parse_ls_remote(output)
    assert default == "main"
    assert {b.name for b in branches} == {"dev", "main", "uat"}
    main = next(b for b in branches if b.name == "main")
    assert main.is_default is True
    assert main.sha == "bbbb222"
    others = [b for b in branches if b.name != "main"]
    assert all(b.is_default is False for b in others)


def test_parse_ls_remote_falls_back_to_common_default():
    """No symref line — pick 'main' if it's present."""
    output = (
        "aaaa111\trefs/heads/feature/x\n"
        "bbbb222\trefs/heads/main\n"
        "cccc333\trefs/heads/dev\n"
    )
    default, branches = _parse_ls_remote(output)
    assert default == "main"
    assert next(b for b in branches if b.name == "main").is_default


def test_parse_ls_remote_first_branch_when_no_common_default():
    """No 'main' / 'master' / 'trunk' — fall back to whatever's first."""
    output = "abc\trefs/heads/feature-only\n"
    default, branches = _parse_ls_remote(output)
    assert default == "feature-only"


def test_parse_ls_remote_handles_empty_output():
    default, branches = _parse_ls_remote("")
    assert default == ""
    assert branches == []


# ── list_branches integration via file:// ───────────────────────────────
class _LocalFileProvider(BaseProvider):
    """A throwaway provider whose auth_url just echoes the raw URL.

    Use to test BaseProvider.list_branches() against a local git repo via
    file:// without involving any real provider's URL parsing.
    """

    name = "local"
    token_env = ""

    def parse(self, url: str) -> ParsedRepo:
        return ParsedRepo(
            provider=self.name,
            host="local",
            org_or_project="",
            repo="",
            raw_url=url,
        )

    def auth_url(self, parsed: ParsedRepo, token: str | None) -> str:
        return parsed.raw_url


@pytest.mark.asyncio
async def test_list_branches_against_local_repo(multi_branch_repo):
    """multi_branch_repo has main, dev, uat. ls-remote should see all three."""
    provider = _LocalFileProvider()
    parsed = provider.parse(f"file://{multi_branch_repo}")
    default, branches = await provider.list_branches(parsed, None)

    assert {b.name for b in branches} == {"main", "dev", "uat"}
    assert all(len(b.sha) == 40 for b in branches)
    # main is the active branch in the fixture; ls-remote should see it as default
    assert default == "main"
    default_branches = [b for b in branches if b.is_default]
    assert len(default_branches) == 1
    assert default_branches[0].name == "main"


@pytest.mark.asyncio
async def test_list_branches_raises_on_unreachable_url():
    """Bogus path → git fails → ProviderError bubbles up."""
    provider = _LocalFileProvider()
    parsed = provider.parse("file:///definitely-not-a-real-repo")
    with pytest.raises(ProviderError):
        await provider.list_branches(parsed, None)
