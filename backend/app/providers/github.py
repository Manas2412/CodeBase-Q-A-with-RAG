"""GitHub provider.

URL pattern:
    https://github.com/<org>/<repo>[.git]

Auth: a Personal Access Token (classic or fine-grained) injected as the
HTTPS password. For private repos, GitHub also accepts the token in
place of the username (`https://<TOKEN>@github.com/...`).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.providers.base import BaseProvider, ParsedRepo, ProviderError


_PATH_RE = re.compile(r"^/(?P<org>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


class GitHubProvider(BaseProvider):
    name = "github"
    token_env = "GITHUB_TOKEN"

    def parse(self, url: str) -> ParsedRepo:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in ("github.com", "www.github.com"):
            raise ProviderError(f"Not a github.com URL: {url!r}")

        match = _PATH_RE.match(parsed.path or "")
        if not match:
            raise ProviderError(
                "GitHub URL must look like 'https://github.com/<org>/<repo>'; "
                f"got {url!r}"
            )

        return ParsedRepo(
            provider=self.name,
            host="github.com",
            org_or_project=match.group("org"),
            repo=match.group("repo"),
            raw_url=url,
        )

    def auth_url(self, parsed: ParsedRepo, token: str | None) -> str:
        """Inject the PAT as the HTTPS password.

        Format that works for both classic and fine-grained tokens:
            https://<TOKEN>@github.com/<org>/<repo>.git
        """
        base = f"https://github.com/{parsed.org_or_project}/{parsed.repo}.git"
        if token:
            return base.replace("https://", f"https://{token}@", 1)
        return base
