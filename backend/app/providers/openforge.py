"""OpenForge (Tuleap) provider.

URL pattern:
    https://openforge.gov.in/plugins/git/<project>/<repo>.git

Auth: a `tlp.k1.…` personal access key (generated under user preferences
in OpenForge). The same key works for HTTPS clones AND the REST API, so
we don't need separate credentials.

Reference: https://docs.tuleap.com/user-guide/integration/rest/quick-start/auth.html
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.providers.base import BaseProvider, ParsedRepo, ProviderError


# Allow URLs with or without the trailing .git
_PATH_RE = re.compile(r"^/plugins/git/(?P<project>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$")


class OpenForgeProvider(BaseProvider):
    name = "openforge"
    token_env = "OPENFORGE_TOKEN"

    def parse(self, url: str) -> ParsedRepo:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            raise ProviderError(f"OpenForge URL has no host: {url!r}")
        if "openforge" not in host and "tuleap" not in host:
            # Tolerated for self-hosted Tuleap instances during testing.
            # Real validation happens at clone time when the host has to resolve.
            pass

        match = _PATH_RE.match(parsed.path or "")
        if not match:
            raise ProviderError(
                "OpenForge path must look like "
                "'/plugins/git/<project>/<repo>[.git]'; "
                f"got {parsed.path!r}"
            )

        return ParsedRepo(
            provider=self.name,
            host=host,
            org_or_project=match.group("project"),
            repo=match.group("repo"),
            raw_url=url,
            extra={"tuleap_path": parsed.path or ""},
        )

    def auth_url(self, parsed: ParsedRepo, token: str | None) -> str:
        """Inject the tlp.k1 token as the HTTPS password.

        Tuleap accepts the access key in place of a password over HTTPS:
            https://review-bot:<TLP_TOKEN>@host/plugins/git/<project>/<repo>.git
        Username can be anything when using an access key.
        """
        path = parsed.extra.get("tuleap_path", "")
        if not path.endswith(".git"):
            path = path.rstrip("/") + ".git"

        if token:
            return f"https://review-bot:{token}@{parsed.host}{path}"
        return f"https://{parsed.host}{path}"
