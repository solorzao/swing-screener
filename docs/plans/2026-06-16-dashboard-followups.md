# Dashboard Follow-ups Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans / subagent-driven-development to implement task-by-task.

**Goal:** (1) Cache the dashboard DB engine so it isn't rebuilt every rerun; (2) persist
and enrich the screening universe (ticker/name/exchange + market_cap + avg_dollar_volume)
from the pipeline so the dashboard Universe view shows data.

**Architecture:** Pure data helpers + mockable yfinance seam in `data/fetch.py`; two new
read/write helpers in `db/repo.py`; two-phase wiring in `pipeline/run.py` (sync seed up
front, enrich per-ticker in the existing isolated loop); a `@st.cache_resource` engine
factory in `dashboard/app.py`. No dashboard view changes (Universe already reads the table).

**Tech Stack:** yfinance (fast_info), pandas, SQLAlchemy 2, Streamlit 1.58, pytest.

## Conventions
- Tests: `./.venv/Scripts/python -m pytest <path> -v`. Gates: `ruff check src tests`, `mypy`.
- Branch `feat/dashboard-followups` (design already committed). Conventional commits.
- yfinance/network is ALWAYS mocked in tests (mirror `tests/data/test_fetch.py`).

---

## Task 1: `avg_dollar_volume` pure helper

**Files:** Modify `src/swing_screener/data/fetch.py`; Test `tests/data/test_fetch.py`.

**Step 1 (failing test):**
```python
def test_avg_dollar_volume_means_close_times_volume():
    import pandas as pd
    from swing_screener.data.fetch import avg_dollar_volume
    df = pd.DataFrame({"close": [10.0, 20.0], "volume": [100.0, 100.0]})
    assert avg_dollar_volume(df) == 1500.0  # (10*100 + 20*100)/2
    assert avg_dollar_volume(df, window=1) == 2000.0  # last bar only
    assert avg_dollar_volume(pd.DataFrame({"close": [], "volume": []})) is None
```
**Step 3 (implement):**
```python
def avg_dollar_volume(frame: pd.DataFrame, window: int = 20) -> float | None:
    """Mean of close*volume over the last `window` bars; None if the frame is empty."""
    if frame is None or frame.empty:
        return None
    tail = frame.tail(window)
    return float((tail["close"] * tail["volume"]).mean())
```
Commit: `feat(data): add avg_dollar_volume helper`.

## Task 2: `fetch_market_cap` (mockable, cached, isolated)

**Files:** Modify `src/swing_screener/data/fetch.py`; Test `tests/data/test_fetch.py`.
Add `import json`. Add a mockable seam `_fast_info_market_cap(ticker) -> float | None` and
the public `fetch_market_cap` mirroring `fetch_bars`' retry/jitter/isolation.

**Step 1 (failing tests):**
```python
def test_fetch_market_cap_caches(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    calls = {"n": 0}
    def fake(ticker):
        calls["n"] += 1
        return 1_234.0
    monkeypatch.setattr(fetch, "_fast_info_market_cap", fake)
    a = fetch.fetch_market_cap("AAPL", cache_dir=tmp_path)
    b = fetch.fetch_market_cap("AAPL", cache_dir=tmp_path)
    assert a == 1234.0 and b == 1234.0
    assert calls["n"] == 1  # second served from cache

def test_fetch_market_cap_none_on_failure(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    monkeypatch.setattr(fetch, "_fast_info_market_cap",
                        lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert fetch.fetch_market_cap("AAPL", cache_dir=tmp_path, retries=2) is None

def test_fetch_market_cap_missing_value_returns_none_no_cache(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    monkeypatch.setattr(fetch, "_fast_info_market_cap", lambda t: None)
    assert fetch.fetch_market_cap("ETF", cache_dir=tmp_path) is None
    assert not (tmp_path / "marketcap").exists()  # a clean None is not cached
```
(The `_no_sleep` autouse fixture already patches `fetch.time.sleep`.)

**Step 3 (implement):**
```python
def _fast_info_market_cap(ticker: str) -> float | None:
    """Mockable wrapper: market cap via yfinance fast_info, or None if unavailable."""
    info = yf.Ticker(ticker).fast_info
    mc = None
    try:
        mc = info["market_cap"]  # FastInfo is mapping-like in modern yfinance
    except (KeyError, TypeError):
        mc = getattr(info, "market_cap", None)
    return float(mc) if mc else None


def fetch_market_cap(ticker: str, *, cache_dir: Path, today: date | None = None,
                     retries: int = 3, backoff: float = 0.5, jitter: float = 0.5) -> float | None:
    """Market cap for one ticker, cached per (ticker, day). None on persistent failure
    or when fast_info has no market cap. Never raises (per-ticker isolation)."""
    today = today or date.today()
    cache_file = Path(cache_dir) / "marketcap" / f"{ticker}_{today:%Y%m%d}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())["market_cap"]
        except Exception as err:
            log.warning("market-cap cache read failed for %s: %s", ticker, err)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            mc = _fast_info_market_cap(ticker)
            if mc is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"market_cap": mc}))
            return mc  # a clean None (no market cap) is returned but not cached
        except Exception as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, jitter))
    log.warning("market-cap fetch failed for %s after %d tries: %s", ticker, retries, last_err)
    return None
```
Commit: `feat(data): add fetch_market_cap (cached, isolated yfinance fast_info)`.

## Task 3: `repo.sync_universe`

**Files:** Modify `src/swing_screener/db/repo.py`; Test `tests/db/test_repo.py`.
Mirrors the seed: upsert ticker→name/exchange, delete tickers not in the seed, PRESERVE
existing `market_cap`/`avg_dollar_volume`. `entries` are `data.universe.UniverseEntry`
(attrs `.ticker`, `.name`, `.exchange`).

**Step 1 (failing test):** seed `Universe(ticker="OLD", market_cap=9.0)` and
`Universe(ticker="KEEP", name="Old", market_cap=5.0)`; sync entries `[KEEP(name="New"), NEW]`;
assert: OLD deleted, NEW inserted, KEEP.name=="New" and KEEP.market_cap preserved (5.0).

**Step 3 (implement):**
```python
def sync_universe(session: Session, entries: "Sequence[UniverseEntry]") -> None:
    incoming = {e.ticker: e for e in entries}
    existing = {u.ticker: u for u in session.scalars(select(Universe))}
    for ticker, e in incoming.items():
        row = existing.get(ticker)
        if row is None:
            session.add(Universe(ticker=ticker, name=e.name, exchange=e.exchange))
        else:
            row.name = e.name
            row.exchange = e.exchange  # metrics left untouched
    for ticker, row in existing.items():
        if ticker not in incoming:
            session.delete(row)
    session.commit()
```
Import `UniverseEntry` from `swing_screener.data.universe` (guard for an import cycle —
`data.universe` imports only stdlib, so `repo` importing it is safe; verify). Commit:
`feat(db): add sync_universe (mirror seed, preserve metrics)`.

## Task 4: `repo.apply_universe_metrics` (batch, single commit)

**Files:** Modify `repo.py`; Test `tests/db/test_repo.py`.
DEVIATION FROM DESIGN: the design named `set_universe_metrics` (per-ticker). Use a BATCH
function instead — the pipeline updates ~hundreds of tickers and a per-row commit would be
slow on Azure. Single function, single commit, None-skips.

**Step 1 (failing test):** seed `Universe(ticker="A", market_cap=1.0, avg_dollar_volume=2.0)`;
`apply_universe_metrics(s, {"A": {"market_cap": 50.0, "avg_dollar_volume": None}, "MISSING": {...}})`;
assert A.market_cap==50.0, A.avg_dollar_volume preserved==2.0 (None skipped), MISSING ignored.

**Step 3 (implement):**
```python
def apply_universe_metrics(
    session: Session, metrics: "Mapping[str, Mapping[str, float | None]]"
) -> None:
    """Update market_cap / avg_dollar_volume for known tickers; skip None values and
    unknown tickers. Single commit."""
    if not metrics:
        return
    rows = {u.ticker: u for u in session.scalars(
        select(Universe).where(Universe.ticker.in_(list(metrics))))}
    for ticker, vals in metrics.items():
        row = rows.get(ticker)
        if row is None:
            continue
        if vals.get("market_cap") is not None:
            row.market_cap = vals["market_cap"]
        if vals.get("avg_dollar_volume") is not None:
            row.avg_dollar_volume = vals["avg_dollar_volume"]
    session.commit()
```
(`Mapping` from `collections.abc`.) Commit: `feat(db): add apply_universe_metrics batch updater`.

## Task 5: Wire persistence + enrichment into `run_screen`

**Files:** Modify `src/swing_screener/pipeline/run.py`; Test `tests/pipeline/test_run.py`.
Import `fetch_market_cap` and `avg_dollar_volume` into the `run` namespace (so tests can
monkeypatch `run.fetch_market_cap`), and `from swing_screener.data.universe import load_universe`
(already imported).

**Changes in `run_screen`:**
1. Load the FULL seed and sync it up front, then truncate for screening:
```python
    full_universe = load_universe(universe_path)
    with Session(engine) as s:
        repo.sync_universe(s, full_universe)
    universe = full_universe[:max_tickers] if max_tickers is not None else full_universe
```
(Place after `engine = get_engine(db_url)`. For mssql the migration has already run, so the
`universe` table exists.)
2. In the per-ticker loop, after `bars_by_tf = _fetch_all_timeframes(...)` and the
`if not bars_by_tf: continue` guard, accumulate metrics (inside the existing try/except, so
a failure stays isolated):
```python
            daily = bars_by_tf.get("1d")
            universe_metrics[entry.ticker] = {
                "avg_dollar_volume": avg_dollar_volume(daily) if daily is not None else None,
                "market_cap": fetch_market_cap(entry.ticker, cache_dir=cache_dir, today=today),
            }
```
Initialize `universe_metrics: dict[str, dict[str, float | None]] = {}` before the loop.
3. Inside the existing `with Session(engine) as s:` block (after `s.commit()` for signals, or
at the end), apply them: `repo.apply_universe_metrics(s, universe_metrics)`.

**Step 1 (failing test)** in `tests/pipeline/test_run.py`:
```python
def test_run_persists_and_enriches_universe(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)
    monkeypatch.setattr(run, "fetch_market_cap", lambda t, **kw: 2_000_000.0 if t == "AAPL" else None)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(universe_path=_write_universe(tmp_path, ["AAPL", "ZZZ"]), db_url=db,
                   cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=date(2024, 4, 1))
    with Session(get_engine(db)) as s:
        rows = {u.ticker: u for u in repo.list_universe(s)}
    assert set(rows) == {"AAPL", "ZZZ"}          # full seed persisted
    assert rows["AAPL"].market_cap == 2_000_000.0  # enriched (fetched ticker)
    assert rows["AAPL"].avg_dollar_volume is not None
    assert rows["ZZZ"].market_cap is None          # ZZZ never fetched -> no metrics
```
Run full `tests/pipeline/ -v` (the existing run tests must stay green — the new sync/enrich
must not change signal/shadow behavior). Commit:
`feat(pipeline): persist + enrich the universe table during a screen run`.

## Task 6: Cache the dashboard engine

**Files:** Modify `src/swing_screener/dashboard/app.py`; Test `tests/dashboard/test_connection.py`.
Add a cached factory and use it in `render()`:
```python
@st.cache_resource
def _cached_engine(db_url: str) -> Engine:
    return get_engine(db_url)
```
(`Engine` is already imported in app.py from Task 16.) In `render()` replace
`engine = get_engine(db_url)` (inside the connectivity `try`) with `engine = _cached_engine(db_url)`.
`@st.cache_resource` does not cache exceptions, so the DB-down guard still fires.

**Test safety:** add an autouse fixture to `tests/dashboard/test_connection.py` that clears the
resource cache so a cached engine can't leak between tests:
```python
@pytest.fixture(autouse=True)
def _clear_engine_cache():
    import streamlit as st
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()
```
Both existing connection tests must stay green. Add a test asserting the engine is built once
across reruns if feasible (monkeypatch `app.get_engine` to count calls, run, switch a page,
assert one construction) — if AppTest re-import semantics make the call-count assertion
flaky, skip it and rely on the two existing connection tests. Commit:
`perf(dashboard): cache the DB engine across reruns`.

## Task 7: Docs + final verification + PR
- Update `docs/dashboard.md` (Universe view now populated by the pipeline; metrics best-effort)
  and `docs/running-locally.md` if it documents the screen run's outputs.
- Full `./.venv/Scripts/python -m pytest -q`, `ruff check src tests`, `mypy` — all clean.
- Optionally trigger one real `run_screen --max-tickers N` against local sqlite to sanity-check
  enrichment end-to-end (network) — OK to skip if offline; tests cover the logic.
- Open a PR against main.

## Risks
- **yfinance `fast_info` accessor** varies by version — `_fast_info_market_cap` tries
  mapping-style then attribute-style and degrades to None; verify against the installed yfinance.
- **Cold-run latency:** one extra market-cap call per ticker; cached per day, isolated, never
  blocks a screen. Acceptable for a nightly batch.
- **`st.cache_resource` cross-test leakage:** mitigated by the autouse cache-clear fixture.
