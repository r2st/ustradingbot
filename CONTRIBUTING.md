# Contributing

Thanks for working on the US Trading Bot. This is a **trading system** — bugs
translate directly into financial loss — so correctness, tests, and fail-safe
defaults matter more than velocity. Please read this before opening a change.

## Ground rules

- **Paper trading is the default and must stay that way.** Never change a
  default that would put real capital at risk without an explicit, reviewed
  opt-in (`BROKER`, `IBKR_PORT`, `TRADING_MODE`).
- **Fail safe.** The AI veto defaults to REJECT on any error; position sizing
  returns zero (skip) rather than a 1-share floor; every entry ships with a stop
  and target. Preserve these invariants.
- **Never commit secrets.** `.env`, the `keys/` directory, and anything under
  `data_store/` are gitignored. Don't add credentials to code, tests, or docs.

## Development setup

The project targets **Python 3.11+**. Use a virtualenv — the system/anaconda
Python usually lacks the dependencies.

```bash
python -m pip install -e ".[dev]"     # runtime + test deps
# optional extras:
#   pip install -e ".[live]"    # ib_insync for live IBKR trading
#   pip install -e ".[alpaca]"  # Alpaca market-data provider
```

Run the engine and dashboard locally (paper mode, zero setup):

```bash
python engine.py
uvicorn dashboard.app:app --host 127.0.0.1 --port 8501   # then http://127.0.0.1:8501
```

## Running the tests

Always run the suite through the project virtualenv:

```bash
.venv/bin/python -m pytest -q
```

The suite is fast (~20–30s, ~1650 tests) and must be **green before every
commit**. Add tests for every behavioural change:

- Pure logic (signals, risk, analytics, tax) → unit tests with synthetic data,
  no network or filesystem beyond `tmp_path`.
- Routers → `fastapi.testclient.TestClient` with the auth/env fixtures in
  `tests/conftest.py`.
- Anything touching money or exits → an explicit fail-safe test (what happens on
  bad input, a provider outage, or a missing key).

### Coverage

```bash
./scripts/coverage.sh                 # term + HTML (htmlcov/) + XML reports
./scripts/coverage.sh tests/test_tax.py   # scope to specific tests
```

Coverage config lives in `.coveragerc`. The script sets `COVERAGE_CORE=sysmon`
so measurement works alongside numpy on recent CPython.

## Code style

- Follow the surrounding code: type hints, module docstrings, and structured
  logging via `structlog` (`log = structlog.get_logger(__name__)`).
- Keep exception handling **narrow, or log at WARN/ERROR** so failures are
  visible — never swallow silently (audit B11).
- New HTTP endpoints: guard with `Depends(require_auth)`, parse bodies with
  `dashboard.http_util.parse_json_body` (or a `dashboard.schemas` Pydantic
  model), and rely on the shared error envelope in `dashboard.middleware`.
- Persist user data with atomic writes (temp file + `os.replace`) as in
  `config/watchlist.py` and `alerts/price_alerts.py`.

## Commits & branches

- Write imperative, scoped commit subjects: `feat(dashboard): …`,
  `fix(analyst): …`, `docs: …`, `test: …`.
- Keep a commit focused; don't bundle unrelated changes.
- Update `CHANGELOG.md` under **Unreleased** for anything user-visible.

## Deployment

Production runs on a single host with **no git repo** — ship with
`git archive` (see `docs/deployment.md` and the systemd units under `deploy/`).
Watch for `.env` config drift between the repo template and the server.
