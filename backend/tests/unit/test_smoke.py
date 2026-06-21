"""Smoke tests proving the test infrastructure itself works.

If any of these fail, fix them before doing anything else — every other
test in the suite depends on the same plumbing.
"""

import pytest


def test_pytest_runs():
    """If this fails, pytest is not installed or not picking up tests."""
    assert 1 + 1 == 2


@pytest.mark.asyncio
async def test_pytest_asyncio_runs():
    """Proves pytest-asyncio is wired and @asyncio markers fire."""
    import asyncio

    await asyncio.sleep(0)
    assert True


def test_backend_app_is_importable():
    """Proves the conftest sys.path tweak lets tests import the app package."""
    from app.db.database import needs_ssl, normalise_dsn

    assert callable(needs_ssl)
    assert callable(normalise_dsn)


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://localhost:5434/x", False),
        ("postgresql://127.0.0.1:5432/x", False),
        ("postgresql://0.0.0.0:5432/x", False),
        ("postgresql://db.amazonaws.com:5432/x", True),
        ("postgresql://ep-foo.neon.tech/x", True),
    ],
)
def test_needs_ssl_detects_local_vs_cloud(dsn: str, expected: bool):
    """Locks the auto-detect logic — local hosts skip SSL, anything else uses it."""
    from app.db.database import needs_ssl

    assert needs_ssl(dsn) is expected
