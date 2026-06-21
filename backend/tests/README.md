# Tests

Layered structure. Run from `backend/`:

```bash
uv run pytest                          # everything fast (unit + fast integration)
uv run pytest tests/unit                # unit only
uv run pytest -m integration            # only integration tests
uv run pytest -m e2e                    # only end-to-end tests
uv run pytest -m eval                   # only review-quality eval (uses Bedrock — costs money)
uv run pytest -m "not eval"             # everything except eval
```

## Layout

```
tests/
├── conftest.py             # shared fixtures: mock_bedrock, synthetic_repo
├── unit/                   # pure-function tests, no I/O — fast, deterministic
├── integration/            # Postgres+pgvector via testcontainers, mocked Bedrock
├── e2e/                    # full flow against the running stack
└── eval/                   # golden-diff corpus + quality scoring for the reviewer
    └── golden_diffs/       # diff + meta.json pairs (Phase 1 Week 5)
```

## Fixtures

- **`mock_bedrock`** — `MagicMock` standing in for the Bedrock runtime client. Pre-shaped responses for `converse` and `invoke_model`. Use in any test that would otherwise call Claude or Cohere.
- **`synthetic_repo`** — builds a real local git repo under a `tmp_path`, returns its path. Use for tests exercising the cloner, diff parser, or provider URL parsing.

## Markers

- `@pytest.mark.integration` — needs Docker + testcontainers
- `@pytest.mark.e2e` — needs the full stack running
- `@pytest.mark.eval` — calls real Bedrock; budget-guarded

Unmarked tests are unit tests by default.
