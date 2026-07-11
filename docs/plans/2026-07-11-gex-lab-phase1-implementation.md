# GEX Options Lab — Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build Phase 1 of the GEX options lab: morning GEX map + day plan, cockpit checklist grader + journal, nightly settlement from completed 5-minute bars, and the Robinhood CSV import with review-and-tag — entirely local, zero changes to the equity screener's behavior.

**Architecture:** A firewalled sibling vertical `src/swing_screener/options/` (pure engines + thin repo functions) with four new DB tables on the shared Base/Alembic chain, one new FastAPI router factory included by `create_app`, and a "GEX Lab" view in the cockpit React app. Design doc: `docs/plans/2026-07-11-gex-options-lab-design.md` — read it first; its firewall rules are binding (no writes to equity tables, no new `StrategyConfig` fields, session-clustered stats).

**Tech stack:** Python 3.12, pandas/numpy (no scipy — Black-Scholes gamma needs only `math`), SQLAlchemy 2.x typed declarative, Alembic, FastAPI, React 19 + Vite (no new JS deps).

**Environment:**
- Worktree: `C:/Users/Oliver/source/repos/swing-screener/.claude/worktrees/gex-lab-phase1`, branch `feat/gex-options-lab`. All paths below are relative to the worktree root.
- Python: `C:/Users/Oliver/source/repos/swing-screener/.venv/Scripts/python.exe` (main checkout's venv; pytest's `pythonpath=["src"]` makes the worktree's source win). Call it `$PY` below.
- Test: `$PY -m pytest tests/options/ -q` per task; full `$PY -m pytest -q` (baseline: 1105 passed) before each commit.
- Lint gates before every commit: `$PY -m ruff check src tests alembic` and `$PY -m mypy` (mypy checks the installed package `swing_screener`; line length 100).
- UI: `npm run lint` / `npm run build` inside `cockpit-ui/` (Node 20). The Vite build lands in `src/swing_screener/cockpit/static/` and **must be committed** — CI has a byte-drift check.

**Coordination hazards (read before starting):**
1. An unexecuted cockpit Phase-3 plan exists (`docs/plans/2026-07-11-desktop-ui-phase3.md`) that restructures `App.tsx` into screens. This plan touches `App.tsx` too (view switcher). Whichever executes second rebases; keep this plan's `App.tsx` diff minimal (Task 16).
2. Alembic head was `d7e4b2f9a1c6` when this plan was written. At execution time run `$PY -m alembic heads` (from worktree root) and use the *current* single head as `down_revision`.
3. **Never commit `C:/Users/Oliver/OneDrive/Desktop/robinhood_report.csv`** — it contains account-transfer history. The test fixture is an anonymized reconstruction (Task 10).

**Codebase conventions you must follow (extracted verbatim 2026-07-11):**
- Models: SQLAlchemy 2.x typed declarative in `src/swing_screener/db/models.py`. Bounded strings always (`String(16/32/...)` — "NVARCHAR(max) is un-indexable" on Azure). Nullable = `Mapped[T | None] = mapped_column(default=None)`. Snake_case-plural `__tablename__`. PK `id: Mapped[int] = mapped_column(primary_key=True)`.
- Repo: plain functions taking `session: Session` first, in `src/swing_screener/db/repo.py` style; idempotent inserts catch `IntegrityError`, roll back, return the existing row (see `add_execution_log`).
- DB tests: **no fixtures** — inline `engine = get_engine("sqlite:///:memory:")` + `with Session(engine) as s:` (`get_engine` runs `create_all` for non-mssql URLs).
- Settings: env-first, read at call time (`load_settings()`), `SWING_*` names, `_TRUE = {"1","true","yes","on"}` for flags, paths resolved absolute. Tests monkeypatch env then call `load_settings()`.
- CLI: no console_scripts — `python -m swing_screener.<module>` with argparse `main()`; defaults from `load_settings()`.
- Cockpit API: everything is closure-scoped inside `create_app(db_url, *, edge_dir=None, static_dir=None, login_spawner=None)`. New routes = a **router factory** called inside `create_app` with the `_session` dependency passed in, included **before** the `app.mount("/", StaticFiles...)` at the end. Write endpoints must 403 unless `request.headers.get("x-cockpit") == "1"`. Every *statistic* is a 10-key Stat dict (`value, n, n_clusters, ci_low, ci_high, cost_level, corpus_id, facet, unit, thin_clusters`); GEX *levels* are plain values (sanctioned exception — they are facts, not estimates). New tables the UI renders need watermarks in `_change_token` or SSE never wakes. Errors: never leak URLs/paths — the app-level `SQLAlchemyError` handler already covers router endpoints.
- Cockpit API tests (`tests/cockpit/test_api.py`): `_db_url(tmp_path)` → `get_engine(url)` to seed schema → `TestClient(create_app(url, edge_dir=tmp_path))`. Seed data with `Session(get_engine(url))` before building the client. `_HDR = {"X-Cockpit": "1"}` for writes.
- UI: React 19 only. `usePolling(fetcher, POLL_MS, wake)` — a changed fetcher does NOT refetch; key-remount the subtree instead. One `useEventWake()` in App. `PanelBody` is the panel template. POST fetchers send `{'X-Cockpit': '1'}`.

---

## Task 1: Lab charter

**Files:**
- Create: `docs/OPTIONS_LAB.md`
- Modify: `docs/NORTH_STAR.md` (one line only)

**Step 1:** Write `docs/OPTIONS_LAB.md` — the lab's own charter, ~60 lines, mirroring NORTH_STAR.md's tone. Required content: Purpose (learn and validate the GEX day-trading method with paper trades and imported real trades; produce evidence, not adrenaline); Scope (SPY/QQQ watchlist + any-ticker ad-hoc analysis; single-leg long calls/puts; day-trade horizon); Principles inherited verbatim from the North Star (evidence gates promotion; honest uncertainty — session-clustered CIs; deterministic levels are ground truth; human gate — no automation of order flow; small, interpretable, reversible); Lab-specific rules (primary metric = R-multiples on the underlying; the `robinhood` book is premium-denominated and never pools with paper stats; checklist grade is recorded before outcome is known); Non-goals (real-money execution, multi-leg strategies, intraday GEX drift, non-index prediction claims). Reference the strategy source guide and the design doc.

**Step 2:** In `docs/NORTH_STAR.md`, find the non-goals section (contains "Not day-trading — swing horizons only") and append one line immediately after that bullet: `  - (The GEX options lab is governed by its own charter, [OPTIONS_LAB.md](OPTIONS_LAB.md) — this document does not apply to it, nor it to this document. Suite-level structure: [ARCHITECTURE.md](ARCHITECTURE.md).)` Do not change anything else. `docs/OPTIONS_LAB.md` should likewise link to `docs/ARCHITECTURE.md` (the module contract it satisfies).

**Step 3:** Commit: `git add docs/OPTIONS_LAB.md docs/NORTH_STAR.md && git commit -m "docs: OPTIONS_LAB charter; cross-reference from North Star"`

---

## Task 2: DB models — the four lab tables

**Files:**
- Modify: `src/swing_screener/db/models.py` (append four classes at the end)
- Test: `tests/options/__init__.py` (empty), `tests/options/test_models.py`

**Step 1: Write the failing test** (`tests/options/test_models.py`):

```python
from datetime import date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import BrokerFill, GexSnapshot, OptionPaperTrade, OptionSetup
from swing_screener.db.session import get_engine


def test_lab_tables_roundtrip() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        snap = GexSnapshot(
            underlying="SPY", ts=datetime(2026, 7, 13, 9, 10), spot=560.25,
            call_wall=565.0, put_wall=550.0, gamma_flip=557.5,
            net_gex=1.2e9, regime="positive", profile_json="[]", source="computed",
        )
        s.add(snap)
        s.commit()
        setup = OptionSetup(
            ts=datetime(2026, 7, 13, 10, 5), underlying="SPY", direction="long",
            gex_snapshot_id=snap.id, regime="positive", pivot_level=557.5,
            pattern="bull flag at flip", grade="A+", status="taken",
            entry=558.0, stop=556.5, target=565.0,
            chk_daily_bias_clear=True, chk_daily_stack_ordered=True, chk_m5_agrees=True,
            chk_gex_levels_marked=True, chk_price_at_pivot=True, chk_regime_match=True,
            chk_pattern_clean=True, chk_volume_confirming=True, chk_risk_sized=True,
            chk_stop_structural=True, chk_rr_at_least_2=True, chk_confirmation_candle=True,
        )
        s.add(setup)
        s.commit()
        trade = OptionPaperTrade(
            setup_id=setup.id, account="options-lab", strategy="gex",
            underlying="SPY", direction="long",
            opened_at=datetime(2026, 7, 13, 10, 6), entry=558.0, stop=556.5, target=565.0,
        )
        fill = BrokerFill(
            import_hash="abc123", activity_date=date(2026, 7, 10), underlying="PATH",
            occ_symbol="PATH  260717C00013000", trans_code="BTO",
            quantity=5, price=0.05, amount=-25.20, raw='{"x": 1}', source="robinhood",
        )
        s.add_all([trade, fill])
        s.commit()
        assert trade.status == "open"
        assert trade.exit_reason is None
        assert fill.id is not None


def test_broker_fill_import_hash_unique() -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        kw = dict(
            import_hash="dup", activity_date=date(2026, 7, 10), underlying="PATH",
            occ_symbol="PATH  260717C00013000", trans_code="BTO",
            quantity=5, price=0.05, amount=-25.20, raw="{}", source="robinhood",
        )
        s.add(BrokerFill(**kw))
        s.commit()
        s.add(BrokerFill(**kw))
        with pytest.raises(IntegrityError):
            s.commit()
```

**Step 2:** Run `$PY -m pytest tests/options/test_models.py -q` — expect FAIL (ImportError: cannot import name 'GexSnapshot').

**Step 3: Implement.** Append to `src/swing_screener/db/models.py` (after the last existing model). Follow house style exactly — bounded strings, `Mapped[T | None] = mapped_column(default=None)`, comments only where a constraint isn't visible in code:

```python
class GexSnapshot(Base):
    """One computed GEX map per (underlying, snapshot time). Options-lab table:
    lifecycle columns are DateTime, not Date -- the lab trades inside the session
    (see docs/OPTIONS_LAB.md; equity tables stay day-keyed)."""

    __tablename__ = "gex_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    spot: Mapped[float]
    call_wall: Mapped[float | None] = mapped_column(default=None)
    put_wall: Mapped[float | None] = mapped_column(default=None)
    gamma_flip: Mapped[float | None] = mapped_column(default=None)
    net_gex: Mapped[float | None] = mapped_column(default=None)
    regime: Mapped[str] = mapped_column(String(16), default="unknown")
    # Per-strike profile as JSON. Text, not a bounded string: a dense SPY chain
    # exceeds any indexable bound, and this column is display-only (never filtered).
    profile_json: Mapped[str] = mapped_column(Text, default="[]")
    thin_chain: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(16), default="computed")


class OptionSetup(Base):
    """One journaled setup, graded against the 12-point A+ checklist at decision time."""

    __tablename__ = "option_setups"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # long | short (calls vs puts)
    gex_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("gex_snapshots.id"), default=None
    )
    regime: Mapped[str] = mapped_column(String(16), default="unknown")
    pivot_level: Mapped[float | None] = mapped_column(default=None)
    pattern: Mapped[str] = mapped_column(String(256), default="")
    entry: Mapped[float | None] = mapped_column(default=None)
    stop: Mapped[float | None] = mapped_column(default=None)
    target: Mapped[float | None] = mapped_column(default=None)
    chk_daily_bias_clear: Mapped[bool] = mapped_column(default=False)
    chk_daily_stack_ordered: Mapped[bool] = mapped_column(default=False)
    chk_m5_agrees: Mapped[bool] = mapped_column(default=False)
    chk_gex_levels_marked: Mapped[bool] = mapped_column(default=False)
    chk_price_at_pivot: Mapped[bool] = mapped_column(default=False)
    chk_regime_match: Mapped[bool] = mapped_column(default=False)
    chk_pattern_clean: Mapped[bool] = mapped_column(default=False)
    chk_volume_confirming: Mapped[bool] = mapped_column(default=False)
    chk_risk_sized: Mapped[bool] = mapped_column(default=False)
    chk_stop_structural: Mapped[bool] = mapped_column(default=False)
    chk_rr_at_least_2: Mapped[bool] = mapped_column(default=False)
    chk_confirmation_candle: Mapped[bool] = mapped_column(default=False)
    grade: Mapped[str] = mapped_column(String(8), default="no_trade")
    status: Mapped[str] = mapped_column(String(16), default="idea", index=True)
    notes: Mapped[str] = mapped_column(String(2048), default="")


class OptionPaperTrade(Base):
    """One options-lab trade: a paper trade opened from a setup, or one imported
    Robinhood flat-to-flat episode. Never mixed with the equity paper_trades table."""

    __tablename__ = "option_paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    setup_id: Mapped[int | None] = mapped_column(ForeignKey("option_setups.id"), default=None)
    account: Mapped[str] = mapped_column(String(16), default="options-lab", index=True)
    strategy: Mapped[str] = mapped_column(String(16), default="gex", index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8), default="long")
    opened_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    entry: Mapped[float | None] = mapped_column(default=None)
    stop: Mapped[float | None] = mapped_column(default=None)
    target: Mapped[float | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    exit_price: Mapped[float | None] = mapped_column(default=None)
    exit_reason: Mapped[str | None] = mapped_column(String(16), default=None)
    realized_r: Mapped[float | None] = mapped_column(default=None)
    hold_minutes: Mapped[int | None] = mapped_column(default=None)
    # Imported-episode fields (nullable for paper trades). OCC symbols are 21 chars.
    occ_symbol: Mapped[str | None] = mapped_column(String(24), default=None, index=True)
    strike: Mapped[float | None] = mapped_column(default=None)
    expiry: Mapped[date | None] = mapped_column(default=None)
    right: Mapped[str | None] = mapped_column(String(4), default=None)
    contracts: Mapped[int | None] = mapped_column(default=None)
    entry_premium: Mapped[float | None] = mapped_column(default=None)
    exit_premium: Mapped[float | None] = mapped_column(default=None)
    premium_pnl: Mapped[float | None] = mapped_column(default=None)
    # Idempotency for import commits: hash of the episode's first fill. Unique so
    # re-committing the same review is a no-op, nullable so paper trades skip it.
    import_key: Mapped[str | None] = mapped_column(String(64), default=None, unique=True)
    needs_review: Mapped[bool] = mapped_column(default=False)


class BrokerFill(Base):
    """One immutable imported broker transaction (Robinhood activity CSV line).
    Fills persist even when their episode is skipped at review -- dedup and
    re-review both need them; episode pairing is re-runnable from fills."""

    __tablename__ = "broker_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_hash: Mapped[str] = mapped_column(String(64), unique=True)
    activity_date: Mapped[date] = mapped_column(index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    occ_symbol: Mapped[str] = mapped_column(String(24), index=True)
    trans_code: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int]
    price: Mapped[float | None] = mapped_column(default=None)
    amount: Mapped[float | None] = mapped_column(default=None)
    raw: Mapped[str] = mapped_column(String(1024), default="")
    source: Mapped[str] = mapped_column(String(16), default="robinhood")
```

(`Text` is already imported in models.py; `date`/`datetime` too.)

**Step 4:** Run `$PY -m pytest tests/options/ -q` — expect PASS. Run `$PY -m ruff check src tests` and `$PY -m mypy`.

**Step 5:** Commit: `feat(gex-lab): four lab tables — GexSnapshot, OptionSetup, OptionPaperTrade, BrokerFill`

---

## Task 3: Alembic migration

**Files:**
- Create: `alembic/versions/<newid>_gex_lab_tables.py`

**Step 1:** Run `$PY -m alembic heads` from the worktree root. Confirm exactly ONE head (expected `d7e4b2f9a1c6`; if different, use what you see).

**Step 2:** Author the migration by hand following the house style of `alembic/versions/d7e4b2f9a1c6_reversal_funnels.py` (12-hex hand-picked id, typed `revision`/`down_revision` attrs, docstring explaining why, `op.create_table` with `sa.Column`, unique indexes via `op.create_index("uq_...", unique=True)`). Pick revision id `f2a9c4e7b1d8`. Create all four tables mirroring the models exactly (SQLite got them via create_all; this migration is for Azure SQL, where Alembic owns the schema). Include: unique index on `broker_fills.import_hash`, unique index on `option_paper_trades.import_key`, plain indexes matching every `index=True` column in the models, FKs (`option_setups.gex_snapshot_id → gex_snapshots.id`, `option_paper_trades.setup_id → option_setups.id`). `downgrade()` drops indexes then tables in reverse dependency order.

**Step 3: Verify the chain and the migration.** `$PY -m alembic heads` → exactly one head = `f2a9c4e7b1d8`. Then verify upgrade runs clean on a scratch DB: check how `alembic/env.py` resolves the URL (it uses the same settings/env pattern) and run `SWING_DB_URL=sqlite:///<tmp>/scratch.db $PY -m alembic upgrade head` (PowerShell: `$env:SWING_DB_URL='sqlite:///C:/Users/Oliver/AppData/Local/Temp/scratch-gex.db'; $PY -m alembic upgrade head`). Expect all migrations to apply with no error. Delete the scratch file after.

**Step 4:** `$PY -m pytest -q` (full suite still green — migrations aren't imported by tests, this is a guard). Commit: `feat(gex-lab): alembic migration f2a9c4e7b1d8 for the four lab tables`

---

## Task 4: GexConfig

**Files:**
- Create: `src/swing_screener/options/__init__.py` (empty), `src/swing_screener/options/config.py`
- Test: `tests/options/test_config.py`

**Step 1: Failing test:**

```python
import dataclasses

import pytest

from swing_screener.options.config import GexConfig


def test_defaults_and_frozen() -> None:
    cfg = GexConfig()
    assert cfg.watchlist == ("SPY", "QQQ")
    assert cfg.ema_spans == (9, 21, 50)
    assert cfg.max_expiries >= 1
    assert 0 < cfg.risk_free_rate < 0.10
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.risk_free_rate = 0.99  # type: ignore[misc]
```

**Step 2:** Run → FAIL (module missing).

**Step 3: Implement** `src/swing_screener/options/config.py` in `StrategyConfig`'s style (frozen dataclass, grouped sections, provenance comments). Fields:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class GexConfig:
    # Morning-plan universe. Ad-hoc analysis accepts any ticker; only these get
    # an automatic plan. Tuple, not list: the config is frozen all the way down.
    watchlist: tuple[str, ...] = ("SPY", "QQQ")

    # EMA stack (the strategy's 9/21/50; independent from StrategyConfig's 20/50
    # by design -- lab knobs never live in the equity config).
    ema_spans: tuple[int, int, int] = (9, 21, 50)
    # A stack only counts as directional when the fast EMA has moved in the stack
    # direction over this many bars (slope confirmation).
    slope_lookback: int = 3

    # GEX computation
    max_expiries: int = 4          # nearest N expiries in the map (0DTE + weeklies)
    risk_free_rate: float = 0.04   # BS r; precision is irrelevant for wall RANKING
    # Strikes further than this fraction from spot are noise for wall detection.
    strike_window_pct: float = 0.15

    # Chain-liquidity guard (warn, never block -- design doc "Any-ticker analysis").
    min_total_oi: int = 10_000       # sum of OI across the windowed chain
    min_populated_strikes: int = 10  # strikes with OI > 0 inside the window
```

**Step 4:** Run test → PASS. ruff + mypy. Commit: `feat(gex-lab): GexConfig`

---

## Task 5: Black-Scholes gamma + GEX levels (`options/gex.py`) — pure

**Files:**
- Create: `src/swing_screener/options/gex.py`
- Test: `tests/options/test_gex.py`

**Step 1: Failing tests.** Chain frames use lowercase columns `expiry` (date), `strike` (float), `right` ("C"/"P"), `open_interest` (int), `iv` (float):

```python
from datetime import date

import pandas as pd

from swing_screener.options.config import GexConfig
from swing_screener.options.gex import bs_gamma, compute_gex


def _chain(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_bs_gamma_peaks_at_the_money() -> None:
    atm = bs_gamma(spot=100, strike=100, iv=0.2, t_years=0.02)
    otm = bs_gamma(spot=100, strike=120, iv=0.2, t_years=0.02)
    assert atm > otm > 0


def test_bs_gamma_degenerate_inputs_are_zero() -> None:
    assert bs_gamma(100, 100, iv=0.0, t_years=0.02) == 0.0
    assert bs_gamma(100, 100, iv=0.2, t_years=0.0) == 0.0


def test_walls_flip_and_regime() -> None:
    exp = date(2026, 7, 17)
    asof = date(2026, 7, 13)
    rows = [
        # heavy call OI above spot at 105 -> call wall
        {"expiry": exp, "strike": 105.0, "right": "C", "open_interest": 50_000, "iv": 0.2},
        {"expiry": exp, "strike": 102.0, "right": "C", "open_interest": 5_000, "iv": 0.2},
        # heavy put OI below spot at 95 -> put wall
        {"expiry": exp, "strike": 95.0, "right": "P", "open_interest": 40_000, "iv": 0.25},
        {"expiry": exp, "strike": 98.0, "right": "P", "open_interest": 8_000, "iv": 0.22},
    ]
    levels = compute_gex(_chain(rows), spot=100.0, asof=asof, cfg=GexConfig())
    assert levels.call_wall == 105.0
    assert levels.put_wall == 95.0
    assert levels.gamma_flip is not None and 95.0 < levels.gamma_flip < 105.0
    assert levels.regime in {"positive", "negative"}
    # spot 100 with big call gamma above and put gamma below: cumulative net GEX
    # crosses zero between the walls; at spot the book here is put-dominated below,
    # call-dominated above -- regime must match sign of net GEX at spot side.
    assert levels.net_gex != 0.0


def test_empty_chain_yields_unknowns() -> None:
    levels = compute_gex(_chain([]), spot=100.0, asof=date(2026, 7, 13), cfg=GexConfig())
    assert levels.call_wall is None and levels.put_wall is None
    assert levels.gamma_flip is None
    assert levels.regime == "unknown"
```

**Step 2:** Run → FAIL.

**Step 3: Implement** `src/swing_screener/options/gex.py`:

```python
"""Naive dealer-gamma (GEX) model: Black-Scholes gamma x open interest.

Convention (the standard public-dashboard model): dealers are long gamma on
customer-sold calls and short gamma on customer-bought puts, so per-strike
dollar GEX = gamma * OI * 100 * spot^2 * 0.01 with calls positive and puts
negative. This ignores actual dealer positioning -- documented assumption,
see docs/OPTIONS_LAB.md. Levels are for structure, not prophecy.
"""

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from swing_screener.options.config import GexConfig

_TRADING_DAYS = 252.0


def bs_gamma(spot: float, strike: float, iv: float, t_years: float,
             r: float = 0.04) -> float:
    """Black-Scholes gamma. No scipy: gamma needs only the normal PDF."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + iv * iv / 2.0) * t_years) / (iv * math.sqrt(t_years))
    pdf = math.exp(-d1 * d1 / 2.0) / math.sqrt(2.0 * math.pi)
    return pdf / (spot * iv * math.sqrt(t_years))


@dataclass(frozen=True)
class StrikeGamma:
    strike: float
    call_gex: float
    put_gex: float

    @property
    def net(self) -> float:
        return self.call_gex + self.put_gex


@dataclass(frozen=True)
class GexLevels:
    spot: float
    call_wall: float | None
    put_wall: float | None
    gamma_flip: float | None
    net_gex: float
    regime: str  # positive | negative | unknown
    profile: tuple[StrikeGamma, ...] = field(default=())


def compute_gex(chain: pd.DataFrame, spot: float, asof: date, cfg: GexConfig) -> GexLevels:
    if chain.empty:
        return GexLevels(spot=spot, call_wall=None, put_wall=None,
                         gamma_flip=None, net_gex=0.0, regime="unknown")
    df = chain.copy()
    lo, hi = spot * (1 - cfg.strike_window_pct), spot * (1 + cfg.strike_window_pct)
    df = df[(df["strike"] >= lo) & (df["strike"] <= hi)]
    if df.empty:
        return GexLevels(spot=spot, call_wall=None, put_wall=None,
                         gamma_flip=None, net_gex=0.0, regime="unknown")

    def _t(expiry: date) -> float:
        # Calendar-day fraction of a trading year; floor at half a day so 0DTE
        # still carries gamma instead of dividing by zero.
        days = max((expiry - asof).days, 0.5)
        return days / 365.0

    df["gamma"] = [
        bs_gamma(spot, row.strike, row.iv, _t(row.expiry), cfg.risk_free_rate)
        for row in df.itertuples()
    ]
    # Dollar gamma per 1% move; calls +, puts -.
    sign = df["right"].map({"C": 1.0, "P": -1.0})
    df["gex"] = df["gamma"] * df["open_interest"] * 100.0 * spot * spot * 0.01 * sign

    per_strike = df.groupby("strike")["gex"].agg(
        call_gex=lambda s: s[s > 0].sum(), put_gex=lambda s: s[s < 0].sum()
    ).reset_index().sort_values("strike")

    profile = tuple(
        StrikeGamma(strike=float(r.strike), call_gex=float(r.call_gex),
                    put_gex=float(r.put_gex))
        for r in per_strike.itertuples()
    )
    calls_above = [p for p in profile if p.call_gex > 0 and p.strike >= spot]
    puts_below = [p for p in profile if p.put_gex < 0 and p.strike <= spot]
    call_wall = max(calls_above, key=lambda p: p.call_gex).strike if calls_above else None
    put_wall = min(puts_below, key=lambda p: p.put_gex).strike if puts_below else None

    # Gamma flip: strike where cumulative net GEX (ascending strikes) crosses zero,
    # linearly interpolated between the bracketing strikes.
    flip: float | None = None
    cum = 0.0
    prev_strike, prev_cum = None, None
    for p in profile:
        cum += p.net
        if prev_cum is not None and prev_cum < 0 <= cum and prev_strike is not None:
            frac = -prev_cum / (cum - prev_cum) if cum != prev_cum else 0.0
            flip = prev_strike + frac * (p.strike - prev_strike)
            break
        prev_strike, prev_cum = p.strike, cum

    net = float(sum(p.net for p in profile))
    if flip is not None:
        regime = "positive" if spot >= flip else "negative"
    else:
        regime = "positive" if net > 0 else "negative" if net < 0 else "unknown"
    return GexLevels(spot=spot, call_wall=call_wall, put_wall=put_wall,
                     gamma_flip=flip, net_gex=net, regime=regime, profile=profile)
```

(If the groupby-with-lambdas form fights mypy/pandas typing, compute call/put sums as two separate groupbys and merge — behavior over cleverness.)

**Step 4:** Run tests → PASS. ruff + mypy. Commit: `feat(gex-lab): Black-Scholes gamma and GEX level computation`

---

## Task 6: Chain snapshot + liquidity guard (`options/chain.py`)

**Files:**
- Create: `src/swing_screener/options/chain.py`
- Test: `tests/options/test_chain.py`

Seam pattern copied from `data/fetch.py`'s `_download`: one thin mockable function does all the network I/O.

**Step 1: Failing tests:**

```python
from datetime import date

import pandas as pd

from swing_screener.options.chain import ChainSnapshot, assess_liquidity, snapshot_chain
from swing_screener.options.config import GexConfig


def _fake_raw(ticker: str, max_expiries: int):
    frame = pd.DataFrame([
        {"expiry": date(2026, 7, 17), "strike": 100.0, "right": "C",
         "open_interest": 20_000, "iv": 0.2},
        {"expiry": date(2026, 7, 17), "strike": 95.0, "right": "P",
         "open_interest": 15_000, "iv": 0.25},
    ])
    return 100.0, frame


def test_snapshot_chain_uses_seam() -> None:
    snap = snapshot_chain("SPY", cfg=GexConfig(), fetch=_fake_raw)
    assert isinstance(snap, ChainSnapshot)
    assert snap.spot == 100.0
    assert list(snap.frame.columns) == ["expiry", "strike", "right", "open_interest", "iv"]


def test_liquidity_guard_flags_thin_chain() -> None:
    cfg = GexConfig()
    _, frame = _fake_raw("X", 1)
    report = assess_liquidity(frame, spot=100.0, cfg=cfg)
    assert report.thin is True          # 2 strikes, 35k OI < min_populated_strikes
    assert any("strikes" in r for r in report.reasons)


def test_liquidity_guard_passes_dense_chain() -> None:
    cfg = GexConfig()
    rows = [
        {"expiry": date(2026, 7, 17), "strike": 90.0 + i, "right": "C",
         "open_interest": 2_000, "iv": 0.2}
        for i in range(20)
    ]
    report = assess_liquidity(pd.DataFrame(rows), spot=100.0, cfg=cfg)
    assert report.thin is False and report.reasons == []
```

**Step 2:** Run → FAIL.

**Step 3: Implement.** `ChainSnapshot` frozen dataclass: `underlying: str`, `spot: float`, `asof: datetime` (naive US/Eastern now — take an injectable `now` callable defaulting to a module `_now_eastern()` copied in spirit from `data/fetch.py`), `frame: pd.DataFrame`. `_fetch_chain_raw(ticker, max_expiries)` is the real seam: `t = yf.Ticker(ticker)`; spot from `t.fast_info["lastPrice"]` (fall back to `t.history(period="1d")["Close"].iloc[-1]` if missing/None); expiries from `t.options[:max_expiries]`; per expiry `oc = t.option_chain(exp)`, take `oc.calls`/`oc.puts` frames, keep `strike`, `openInterest`→`open_interest` (fillna 0, int), `impliedVolatility`→`iv` (fillna 0.0), add `right` = "C"/"P" and `expiry` = parsed date; concat. Wrap the whole raw fetch in the retry/backoff/jitter loop pattern from `fetch_bars` (3 tries, exponential + jitter, return raises `RuntimeError` after exhaustion — chain snapshots are user-triggered, so failure must be visible, not None). `snapshot_chain(ticker, *, cfg, fetch=_fetch_chain_raw, now=None) -> ChainSnapshot` normalizes column order. `assess_liquidity(frame, spot, cfg) -> LiquidityReport(thin: bool, reasons: list[str])`: window strikes to ±`strike_window_pct`; reasons appended when windowed total OI < `min_total_oi` ("total OI {n} < {min}") or populated strikes < `min_populated_strikes` ("{n} populated strikes < {min}"); `thin = bool(reasons)`. Module has NO network access at import; only `_fetch_chain_raw` touches yfinance.

**Step 4:** Tests → PASS. ruff + mypy (yfinance is untyped — mirror however fetch.py handles it; add `# type: ignore[...]` only if fetch.py does). Commit: `feat(gex-lab): chain snapshot seam + liquidity guard`

---

## Task 7: EMA stack bias (`options/bias.py`) — pure

**Files:**
- Create: `src/swing_screener/options/bias.py`
- Test: `tests/options/test_bias.py`

**Step 1: Failing tests** (use the `bars` conftest fixture / `make_bars` for frames, or plain `pd.Series`):

```python
import pandas as pd

from swing_screener.options.bias import StackState, stack_state
from swing_screener.options.config import GexConfig


def _trend(start: float, step: float, n: int = 120) -> pd.Series:
    return pd.Series([start + step * i for i in range(n)])


def test_uptrend_is_bullish() -> None:
    s = stack_state(_trend(100, 0.5), cfg=GexConfig())
    assert s.direction == "bullish"
    assert s.spacing_pct > 0


def test_downtrend_is_bearish() -> None:
    assert stack_state(_trend(200, -0.5), cfg=GexConfig()).direction == "bearish"


def test_chop_is_tangled() -> None:
    chop = pd.Series([100 + (1 if i % 2 else -1) for i in range(120)])
    assert stack_state(chop, cfg=GexConfig()).direction == "tangled"


def test_too_short_series_is_tangled() -> None:
    assert stack_state(pd.Series([1.0, 2.0]), cfg=GexConfig()).direction == "tangled"
```

**Step 2:** Run → FAIL.

**Step 3: Implement.** Reuse `swing_screener.indicators.trend.ema` (signature: `ema(series: pd.Series, span: int) -> pd.Series`). `StackState` frozen dataclass: `direction: str` ("bullish"/"bearish"/"tangled"), `spacing_pct: float` (gap between fast and slow EMA as % of close — momentum proxy per the guide's "wider spacing = stronger momentum"), `emas: tuple[float, float, float]`. Logic: need `len(series) >= max(span)*2` else tangled; compute the three EMAs from `cfg.ema_spans`; last values `f, m, s`; bullish iff `f > m > s` AND fast EMA rose over `cfg.slope_lookback` bars; bearish iff `f < m < s` AND fast EMA fell; else tangled. `spacing_pct = abs(f - s) / close * 100`.

**Step 4:** PASS, ruff, mypy. Commit: `feat(gex-lab): 9/21/50 EMA stack state`

---

## Task 8: Checklist (`options/checklist.py`) — pure

**Files:**
- Create: `src/swing_screener/options/checklist.py`
- Test: `tests/options/test_checklist.py`

**Step 1: Failing tests:**

```python
from swing_screener.options.checklist import CHECKLIST_ITEMS, grade


def test_twelve_items_in_four_blocks() -> None:
    assert len(CHECKLIST_ITEMS) == 12
    assert {i.block for i in CHECKLIST_ITEMS} == {"bias", "structure", "trigger", "risk"}
    assert all(i.key.startswith("chk_") for i in CHECKLIST_ITEMS)


def test_all_true_is_a_plus() -> None:
    assert grade({i.key: True for i in CHECKLIST_ITEMS}) == "A+"


def test_missing_only_confirmation_candle_is_b() -> None:
    items = {i.key: True for i in CHECKLIST_ITEMS}
    items["chk_confirmation_candle"] = False
    assert grade(items) == "B"


def test_any_other_miss_is_no_trade() -> None:
    items = {i.key: True for i in CHECKLIST_ITEMS}
    items["chk_regime_match"] = False
    assert grade(items) == "no_trade"


def test_unknown_key_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        grade({"chk_bogus": True})
```

**Step 2:** FAIL. **Step 3: Implement.** `ChecklistItem` frozen dataclass (`key`, `label`, `block`). `CHECKLIST_ITEMS: tuple[ChecklistItem, ...]` — the 12 items from the guide, keys matching the `OptionSetup.chk_*` columns exactly (`chk_daily_bias_clear`, `chk_daily_stack_ordered`, `chk_m5_agrees` in block "bias"; `chk_gex_levels_marked`, `chk_price_at_pivot`, `chk_regime_match` in "structure"; `chk_pattern_clean`, `chk_volume_confirming` in "trigger"; `chk_risk_sized`, `chk_stop_structural`, `chk_rr_at_least_2`, `chk_confirmation_candle` in "risk"), labels = the guide's plain-English one-liners. `grade(items: dict[str, bool]) -> str`: validate every provided key exists and every checklist key is provided (KeyError otherwise); all true → "A+"; false set == {confirmation candle} → "B" (the guide: "No confirmation candle = B grade at best"); anything else → "no_trade".

**Step 4:** PASS, lint, commit: `feat(gex-lab): the 12-point A+ checklist`

---

## Task 9: Day plan (`options/plan.py`) + persistence

**Files:**
- Create: `src/swing_screener/options/plan.py`
- Test: `tests/options/test_plan.py`

**Step 1: Failing tests:**

```python
from datetime import date, datetime

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.db.models import GexSnapshot
from swing_screener.db.session import get_engine
from swing_screener.options.config import GexConfig
from swing_screener.options.gex import GexLevels
from swing_screener.options.plan import build_plan, save_snapshot


def _levels(regime: str) -> GexLevels:
    return GexLevels(spot=100.0, call_wall=105.0, put_wall=95.0, gamma_flip=99.0,
                     net_gex=1e9, regime=regime)


def _daily_up() -> pd.Series:
    return pd.Series([100 + 0.5 * i for i in range(120)])


def test_negative_gamma_plus_trend_is_breakout_day() -> None:
    plan = build_plan("SPY", _daily_up(), _levels("negative"), GexConfig())
    assert plan.bias == "bullish"
    assert plan.call == "breakout"


def test_positive_gamma_plus_trend_is_range_day() -> None:
    assert build_plan("SPY", _daily_up(), _levels("positive"), GexConfig()).call == "range"


def test_tangled_daily_is_stand_down() -> None:
    chop = pd.Series([100 + (1 if i % 2 else -1) for i in range(120)])
    assert build_plan("SPY", chop, _levels("negative"), GexConfig()).call == "stand_down"


def test_save_snapshot_persists_levels_and_profile() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = save_snapshot(s, underlying="SPY", ts=datetime(2026, 7, 13, 9, 10),
                            levels=_levels("positive"), thin=False)
        assert row.id is not None
        got = s.get(GexSnapshot, row.id)
        assert got is not None and got.regime == "positive" and got.call_wall == 105.0
```

**Step 2:** FAIL. **Step 3: Implement.** `DayPlan` frozen dataclass: `underlying`, `bias` (from `stack_state(daily_close, cfg)`), `regime`, `call` ("breakout"/"range"/"stand_down"), `levels: GexLevels`, `spacing_pct`. Matrix: tangled → stand_down; directional + negative regime → breakout; directional + positive → range; regime unknown → stand_down (no map, no trade). `save_snapshot(session, *, underlying, ts, levels, thin, source="computed") -> GexSnapshot`: serializes `levels.profile` to `profile_json` via `json.dumps([dataclasses.asdict(p) for p in profile])`, adds, commits, refreshes, returns.

**Step 4:** PASS, lint, commit: `feat(gex-lab): day-plan matrix + snapshot persistence`

---

## Task 10: Robinhood CSV parser (`options/broker_import.py`, part 1)

**Files:**
- Create: `src/swing_screener/options/broker_import.py`, `tests/options/fixtures/robinhood_sample.csv`
- Test: `tests/options/test_broker_import.py`

**Step 1: Build the anonymized fixture** `tests/options/fixtures/robinhood_sample.csv` — a hand-written reconstruction (fictional P&L, no transfer amounts matching reality) that preserves EVERY quirk of the real export:

```csv
"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity","Price","Amount"
"7/10/2026","7/10/2026","7/13/2026","AAA","Option Expiration for AAA 7/10/2026 Call $12.00","OEXP","30S","",""
"7/10/2026","7/10/2026","7/13/2026","AAA","AAA 7/17/2026 Call $13.00","BTO","5","$0.05","($25.20)"
"7/10/2026","7/10/2026","7/13/2026","","ACH Deposit","ACH","","","$0.20"
"7/9/2026","7/9/2026","7/10/2026","AAA","AAA 7/10/2026 Call $12.00","BTO","30","$0.02","($60.40)"
"7/2/2026","7/2/2026","7/6/2026","AAA","AAA 7/2/2026 Put $11.50","STC","2","$0.18","$35.90"
"7/1/2026","7/1/2026","7/2/2026","AAA","AAA 7/2/2026 Put $11.50","BTO","2","$0.15","($30.08)"
"7/1/2026","7/1/2026","7/1/2026","","Instant bank transfer - account ending in 0000","RTP","","","$47.20"
"6/25/2026","6/25/2026","6/26/2026","BBB","BBB 8/21/2026 Call $360.00","STC","1","$12.55","$1,254.92"
"6/25/2026","6/25/2026","6/26/2026","BBB","BBB 8/21/2026 Call $360.00","BTO","1","$13.20","($1,320.04)"
"6/24/2026","6/24/2026","6/25/2026","AAA","AAA 8/21/2026 Call $15.00","STC","15","$0.13","$194.34"
"6/24/2026","6/24/2026","6/25/2026","AAA","AAA 8/21/2026 Call $15.00","STC","15","$0.13","$194.34"
"6/23/2026","6/23/2026","6/24/2026","AAA","AAA 8/21/2026 Call $15.00","BTO","30","$0.14","($421.12)"
"6/18/2026","6/18/2026","6/22/2026","","ACH CANCEL","ACH","","","($2.00)"
"5/26/2026","5/26/2026","5/27/2026","CCC","CCC 5/29/2026 Call $16.00","STO","5","$0.08","$39.80"
""
"","","","","","","","","","The data provided is for informational purposes only."
```

(Quirks covered: OEXP with `30S` quantity and empty price/amount; ACH/RTP noise rows with empty Instrument; parenthesized comma amounts; identical duplicate fill lines that are DISTINCT transactions; an STO row; blank line; 10-column disclaimer trailer.)

**Step 2: Failing tests:**

```python
from datetime import date
from pathlib import Path

from swing_screener.options.broker_import import parse_activity_csv

FIXTURE = Path(__file__).parent / "fixtures" / "robinhood_sample.csv"


def _fills():
    return parse_activity_csv(FIXTURE.read_text(encoding="utf-8"))


def test_noise_and_trailer_rows_are_skipped() -> None:
    fills = _fills()
    assert all(f.trans_code in {"BTO", "STC", "STO", "BTC", "OEXP"} for f in fills)
    assert len(fills) == 11  # 10 trade rows + 1 OEXP; ACH/RTP/blank/disclaimer skipped


def test_description_parses_to_occ_fields() -> None:
    f = next(f for f in _fills() if f.trans_code == "BTO" and f.quantity == 5)
    assert f.underlying == "AAA"
    assert f.expiry == date(2026, 7, 17)
    assert f.right == "C"
    assert f.strike == 13.0
    assert f.occ_symbol == "AAA   260717C00013000"


def test_money_parsing_handles_parens_and_commas() -> None:
    big = next(f for f in _fills() if f.price == 13.20)
    assert big.amount == -1320.04
    win = next(f for f in _fills() if f.price == 12.55)
    assert win.amount == 1254.92


def test_oexp_quantity_strips_suffix_and_has_no_price() -> None:
    oexp = next(f for f in _fills() if f.trans_code == "OEXP")
    assert oexp.quantity == 30
    assert oexp.price is None and oexp.amount is None
    assert oexp.expiry == date(2026, 7, 10)


def test_duplicate_lines_get_distinct_hashes() -> None:
    fills = _fills()
    dupes = [f for f in fills if f.trans_code == "STC" and f.quantity == 15]
    assert len(dupes) == 2
    assert dupes[0].import_hash != dupes[1].import_hash


def test_unrecognized_header_fails_loudly() -> None:
    import pytest
    with pytest.raises(ValueError, match="unrecognized"):
        parse_activity_csv('"Date","Stuff"\n"1/1/2026","x"\n')
```

**Step 3:** Run → FAIL. **Step 4: Implement** part 1 of `broker_import.py`:

- `FillRecord` frozen dataclass: `import_hash, activity_date, underlying, occ_symbol, expiry, right, strike, trans_code, quantity, price, amount, raw` (raw = JSON of the original row dict).
- `_EXPECTED_HEADER = ["Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code", "Quantity", "Price", "Amount"]` — parse with `csv.reader(io.StringIO(text))`; if the first row != expected, `raise ValueError(f"unrecognized Robinhood CSV header: {row!r}")` (the design's fail-loudly rule).
- Row filter: skip rows with `len(row) != 9` (trailer), all-empty rows, empty `Instrument`, or `Trans Code` not in the trade set.
- `_DESC_RE = re.compile(r"^(?:Option Expiration for )?(?P<u>[A-Z][A-Z0-9.]*) (?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}) (?P<right>Call|Put) \$(?P<strike>[\d,.]+)$")` — rows whose description doesn't match are skipped with a `log.warning` (unknown instrument format ≠ crash; but a matching code with unparseable money DOES raise).
- `_money(s) -> float | None`: empty → None; strip `$`, `,`; `(x)` → `-x`.
- `_qty(s) -> int`: strip a trailing `S`, int().
- OCC symbol: `f"{u:<6}{exp:%y%m%d}{right}{int(round(strike * 1000)):08d}"` (right is "C"/"P").
- `import_hash = hashlib.sha256("|".join([...all 9 raw fields..., str(line_number)]).encode()).hexdigest()` — **include the CSV line number** so byte-identical duplicate fills (real: two 15-lot STCs) stay distinct, while re-importing the same file reproduces identical hashes. Document: re-exporting a LONGER date range shifts line numbers, so dedup also needs the fields themselves — hash = fields + position *within the day's identical-row group* is overkill; fields + line number is accepted Phase-1 behavior and re-imports of overlapping exports may re-add rows whose line numbers moved. Mitigation: `store_fills` (Task 11) also dedups on exact-field-match count per day. Keep it simple; note the limitation in the module docstring.

**Step 5:** PASS, lint, commit: `feat(gex-lab): Robinhood activity-CSV parser pinned to the real export format`

---

## Task 11: Episode pairing + fill persistence (`broker_import.py`, part 2)

**Files:**
- Modify: `src/swing_screener/options/broker_import.py`
- Test: append to `tests/options/test_broker_import.py`

**Step 1: Failing tests:**

```python
from swing_screener.options.broker_import import Episode, pair_episodes, store_fills


def test_same_day_scalp_pairs_into_one_closed_episode() -> None:
    fills = _fills()
    episodes = {e.occ_symbol: e for e in pair_episodes(fills)}
    scalp = episodes["BBB   260821C00360000"]
    assert scalp.status == "closed"
    assert scalp.contracts == 1
    assert scalp.pnl == 1254.92 - 1320.04
    assert scalp.opened_on == scalp.closed_on == date(2026, 6, 25)


def test_scale_out_aggregates_to_one_episode_with_vwap() -> None:
    eps = [e for e in pair_episodes(_fills()) if e.occ_symbol == "AAA   260821C00015000"]
    assert len(eps) == 1
    e = eps[0]
    assert e.status == "closed"
    assert e.contracts == 30
    assert e.entry_premium == 0.14
    assert e.exit_premium == 0.13


def test_expiration_closes_episode_at_zero() -> None:
    e = next(e for e in pair_episodes(_fills()) if e.occ_symbol == "AAA   260710C00012000")
    assert e.status == "closed"
    assert e.exit_reason == "expired"
    assert e.exit_premium == 0.0


def test_unclosed_buy_is_an_open_episode() -> None:
    e = next(e for e in pair_episodes(_fills()) if e.occ_symbol == "AAA   260717C00013000")
    assert e.status == "open"


def test_short_open_is_flagged_needs_review() -> None:
    e = next(e for e in pair_episodes(_fills()) if e.occ_symbol == "CCC   260529C00016000")
    assert e.needs_review is True


def test_store_fills_is_idempotent() -> None:
    from sqlalchemy.orm import Session
    from swing_screener.db.session import get_engine

    engine = get_engine("sqlite:///:memory:")
    fills = _fills()
    with Session(engine) as s:
        first = store_fills(s, fills)
        again = store_fills(s, fills)
    assert first.added == len(fills)
    assert again.added == 0 and again.skipped == len(fills)
```

**Step 2:** FAIL. **Step 3: Implement:**

- `Episode` frozen dataclass: `occ_symbol, underlying, expiry, right, strike, opened_on, closed_on, status ("open"|"closed"), contracts (max cumulative position), entry_premium (VWAP of opens), exit_premium (VWAP of closes; OEXP contributes price 0.0), pnl (sum of fill amounts; None while open), exit_reason ("sold"|"expired"|None), needs_review (bool), import_key (= first fill's import_hash), fill_hashes (tuple)`.
- `pair_episodes(fills) -> list[Episode]`: sort by `(occ_symbol, activity_date, code_rank, original index)` where `code_rank` puts OPENS (BTO) before CLOSES (STC/OEXP) within the same day — **this is load-bearing**: the export is date-only and lists same-day fills in arbitrary (newest-first) order, so a same-day round trip's STC line can precede its BTO line (the real file's GOOG scalp does exactly this); without the rank the walker sees a phantom short. Accepted Phase-1 limitation (document in the docstring): multiple *separate* same-day round trips in the same contract collapse into one episode. Then walk per symbol tracking net position; BTO/BTC add, STC/STO/OEXP reduce (OEXP reduces by its quantity at price 0). A position that starts with STO (net would go negative) → mark the whole symbol's episodes `needs_review=True` and do not compute pnl semantics beyond amount sums. Episode closes when net returns to 0; the next open starts a new episode. Episode still holding at end of fills → `status="open"`, `pnl=None`.
- VWAPs: sum(qty×price)/sum(qty) over the relevant side, rounded to 4 dp.
- `ImportStats` dataclass: `added: int, skipped: int`.
- `store_fills(session, fills) -> ImportStats`: per fill, `session.add(BrokerFill(...))` + commit; on `IntegrityError` rollback and count skipped (the `add_execution_log` idempotency pattern from `db/repo.py`).

**Step 4:** PASS, lint, commit: `feat(gex-lab): flat-to-flat episode pairing + idempotent fill storage`

---

## Task 12: Episode commit with tags (`broker_import.py`, part 3)

**Files:**
- Modify: `src/swing_screener/options/broker_import.py`
- Test: append to `tests/options/test_broker_import.py`

**Step 1: Failing test:**

```python
def test_commit_episodes_respects_tags_and_is_idempotent() -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from swing_screener.db.models import OptionPaperTrade
    from swing_screener.db.session import get_engine
    from swing_screener.options.broker_import import commit_episodes

    engine = get_engine("sqlite:///:memory:")
    episodes = pair_episodes(_fills())
    closed = [e for e in episodes if e.status == "closed"]
    tags = {e.import_key: "gex" for e in closed[:1]}
    tags.update({e.import_key: "other" for e in closed[1:2]})
    # everything else untagged -> skipped
    with Session(engine) as s:
        n = commit_episodes(s, episodes, tags)
        rows = list(s.scalars(select(OptionPaperTrade)))
        assert n == 2 and len(rows) == 2
        assert {r.strategy for r in rows} == {"gex", "other"}
        assert all(r.account == "robinhood" for r in rows)
        assert all(r.premium_pnl is not None for r in rows)
        # re-commit is a no-op
        assert commit_episodes(s, episodes, tags) == 0
```

**Step 2:** FAIL. **Step 3: Implement** `commit_episodes(session, episodes, tags: dict[str, str]) -> int`: for each episode whose `import_key` maps to "gex" or "other": build `OptionPaperTrade(account="robinhood", strategy=tag, underlying=..., direction="long" if not needs_review else "long", occ_symbol=..., strike, expiry, right, contracts, entry_premium, exit_premium, premium_pnl=e.pnl, opened_at=datetime combine(opened_on, 00:00), closed_at=combine(closed_on) if closed, status=e.status ("open" stays open), exit_reason=e.exit_reason, import_key=e.import_key, needs_review=e.needs_review)`. Insert with the IntegrityError-idempotency pattern keyed on the unique `import_key`; count only fresh inserts. Tags with value "skip" or missing → not committed.

**Step 4:** PASS, lint, commit: `feat(gex-lab): tagged episode commit into the robinhood book`

---

## Task 13: Settlement (`options/settle.py`)

**Files:**
- Create: `src/swing_screener/options/settle.py`
- Test: `tests/options/test_settle.py`

**Step 1: Failing tests.** 5m frames via `make_bars(rows, start="2026-07-13 09:30", freq="5min")` (the conftest helper takes start/freq):

```python
from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.settle import settle_open_trades
from tests.conftest import make_bars


def _trade(**kw) -> OptionPaperTrade:
    base = dict(account="options-lab", strategy="gex", underlying="SPY", direction="long",
                opened_at=datetime(2026, 7, 13, 9, 35), entry=100.0, stop=99.0, target=102.0)
    base.update(kw)
    return OptionPaperTrade(**base)


def _settle_one(trade, rows):
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(trade)
        s.commit()
        bars = make_bars(rows, start="2026-07-13 09:30", freq="5min")
        result = settle_open_trades(s, bars_by_underlying={"SPY": bars})
        s.commit()
        return s.get(OptionPaperTrade, trade.id), result


def test_target_touch_wins() -> None:
    rows = [dict(open=100, high=100.5, low=99.8, close=100.2),
            dict(open=100.2, high=102.5, low=100.0, close=102.2)]
    t, _ = _settle_one(_trade(), rows)
    assert t.status == "closed" and t.exit_reason == "target"
    assert t.exit_price == 102.0
    assert t.realized_r == 2.0


def test_stop_touch_loses_one_r() -> None:
    rows = [dict(open=100, high=100.2, low=98.9, close=99.0)]
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "stop" and t.realized_r == -1.0


def test_both_in_one_bar_is_worst_case_stop() -> None:
    rows = [dict(open=100, high=102.5, low=98.9, close=101.0)]
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "stop" and t.realized_r == -1.0


def test_neither_touched_settles_eod_flat() -> None:
    rows = [dict(open=100, high=100.6, low=99.6, close=100.5)] * 3
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "eod_flat"
    assert t.exit_price == 100.5
    assert t.realized_r == 0.5  # (100.5 - 100) / (100 - 99)


def test_short_direction_mirrors() -> None:
    rows = [dict(open=100, high=100.4, low=97.9, close=98.0)]
    t, _ = _settle_one(_trade(direction="short", stop=101.0, target=98.0), rows)
    assert t.exit_reason == "target" and t.realized_r == 2.0


def test_bars_before_open_are_ignored() -> None:
    rows = [dict(open=100, high=102.5, low=98.5, close=100.0),  # 09:30 bar: pre-open spike
            dict(open=100, high=100.4, low=99.6, close=100.2)]
    t, _ = _settle_one(_trade(opened_at=datetime(2026, 7, 13, 9, 35)), rows)
    assert t.exit_reason == "eod_flat"  # the 09:30 bar's touches don't count
```

**Step 2:** FAIL. **Step 3: Implement** `settle_open_trades(session, *, bars_by_underlying: dict[str, pd.DataFrame]) -> SettleResult`:

- Select open `OptionPaperTrade` rows with `account == "options-lab"` and non-null `entry/stop/target` (imported robinhood rows never enter here — their book settles from fills).
- For each: take its underlying's frame; **normalize the index to naive US/Eastern** — if `bars.index.tz is not None`, `bars.index = bars.index.tz_convert("America/New_York").tz_localize(None)` (yfinance 5m bars come tz-aware; fixtures are naive). Keep bars with `index >= opened_at` only. A bar "touches" stop/target by high/low range inclusion.
- Long: stop touch = `low <= stop`, target touch = `high >= target`. Short: mirrored. Both in one bar → stop (worst-case convention, matching the equity book's pessimistic fills). Neither across all bars → close at last bar's close, reason `eod_flat`.
- `realized_r`: risk = `abs(entry - stop)`; long r = `(exit - entry)/risk`, short r = `(entry - exit)/risk`. Stop exit fills AT the stop, target exit AT the target (structural levels, not intrabar guesses).
- Set `closed_at` = touched bar's timestamp (or last bar), `hold_minutes` = int((closed_at - opened_at).total_seconds() // 60), `status="closed"`. No commit inside (caller commits) — matches repo functions that mutate then let CLI commit; actually `db/repo.py` functions DO commit — follow repo style: commit at the end of `settle_open_trades`.
- `SettleResult` dataclass: `settled: int, skipped_no_bars: int`.
- Trades whose underlying has no frame → count `skipped_no_bars`, leave open (tomorrow's settle retries; a warning logs).

**Step 4:** PASS, lint, commit: `feat(gex-lab): worst-case settlement from completed 5m bars`

---

## Task 14: Journal repo functions (`options/journal.py`)

**Files:**
- Create: `src/swing_screener/options/journal.py`
- Test: `tests/options/test_journal.py`

**Step 1: Failing tests:**

```python
from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade, OptionSetup
from swing_screener.db.session import get_engine
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.journal import create_setup, list_setups, set_status


def _all_true() -> dict[str, bool]:
    return {i.key: True for i in CHECKLIST_ITEMS}


def test_create_setup_computes_grade_and_stores_items() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(
            s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY", direction="long",
            checklist=_all_true(), entry=558.0, stop=556.5, target=565.0,
            regime="positive", pivot_level=557.5, pattern="flag", notes="",
        )
        assert row.grade == "A+"
        assert row.chk_confirmation_candle is True
        assert row.status == "idea"


def test_taking_a_setup_opens_a_paper_trade() -> None:
    from sqlalchemy import select
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6))
        trades = list(s.scalars(select(OptionPaperTrade)))
        assert len(trades) == 1
        assert trades[0].setup_id == row.id
        assert trades[0].account == "options-lab" and trades[0].status == "open"


def test_skip_does_not_open_a_trade_and_double_take_is_noop() -> None:
    from sqlalchemy import select
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 10, 6))
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 7))
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 8))
        assert len(list(s.scalars(select(OptionPaperTrade)))) == 1


def test_list_setups_filters_by_day() -> None:
    from datetime import date
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 14, 10, 5), underlying="QQQ",
                     direction="long", checklist=_all_true())
        assert [x.underlying for x in list_setups(s, day=date(2026, 7, 13))] == ["SPY"]
        assert len(list_setups(s, day=None)) == 2
```

**Step 2:** FAIL. **Step 3: Implement:** `create_setup(session, *, ts, underlying, direction, checklist: dict[str, bool], entry=None, stop=None, target=None, regime="unknown", pivot_level=None, pattern="", notes="", gex_snapshot_id=None) -> OptionSetup` — validates checklist via `grade()` (which raises on bad keys), splats the 12 booleans onto the `chk_*` columns, commits, refreshes. `set_status(session, setup_id, status, *, at: datetime) -> OptionSetup` — validates `status in {"idea", "taken", "skipped"}`; on transition TO "taken" creates the `OptionPaperTrade(account="options-lab", strategy="gex", setup_id=..., underlying, direction, opened_at=at, entry/stop/target from the setup)` unless one already exists for the setup (query first — that's the double-take guard). `list_setups(session, *, day: date | None)` — filter by `ts` within [day 00:00, day+1) when given, newest first.

**Step 4:** PASS, lint, commit: `feat(gex-lab): journal — setups, status transitions, paper-trade opening`

---

## Task 15: Lab stats (`options/stats.py`)

**Files:**
- Create: `src/swing_screener/options/stats.py`
- Test: `tests/options/test_stats.py`

**Read first:** `src/swing_screener/analytics/performance.py` — find the clustered-bootstrap primitive (the mapping report: "bootstrap primitives already consume generic `{cluster: [R...]}` mappings") and the `Stat` dataclass in `src/swing_screener/cockpit/stats.py` (10-key wire contract). Reuse the primitive; do NOT copy its math and do NOT touch its constants (`_CLUSTER_FLOOR` stays equity-only).

**Step 1: Failing test:**

```python
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.stats import lab_summary


def _closed(day: int, r: float, grade_setup=None, **kw) -> OptionPaperTrade:
    opened = datetime(2026, 7, 1, 10, 0) + timedelta(days=day)
    base = dict(account="options-lab", strategy="gex", underlying="SPY", direction="long",
                opened_at=opened, closed_at=opened + timedelta(hours=1),
                entry=100.0, stop=99.0, target=102.0, status="closed", realized_r=r)
    base.update(kw)
    return OptionPaperTrade(**base)


def test_lab_summary_clusters_by_session() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # 3 sessions x 2 trades
        for day in range(3):
            s.add(_closed(day, 1.0))
            s.add(_closed(day, -0.5))
        s.commit()
        stat = lab_summary(s, account="options-lab")
    assert stat["n"] == 6
    assert stat["n_clusters"] == 3          # sessions, not tickers
    assert stat["unit"] == "R"
    assert stat["facet"] == "gex-lab"
    assert set(stat) >= {"value", "n", "n_clusters", "ci_low", "ci_high",
                         "cost_level", "corpus_id", "facet", "unit", "thin_clusters"}


def test_robinhood_book_is_never_pooled() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_closed(0, 5.0, account="robinhood", import_key="k1"))
        s.add(_closed(1, 1.0))
        s.commit()
        stat = lab_summary(s, account="options-lab")
    assert stat["n"] == 1
```

**Step 2:** FAIL. **Step 3: Implement** `lab_summary(session, *, account: str, strategy: str = "gex") -> dict`: select closed trades with non-null `realized_r` for that account+strategy; build `{opened_at.date(): [r, ...]}` cluster mapping; feed the performance.py clustered-CI primitive; wrap in a `Stat`-shaped dict (`cost_level="0.00"` — no cost model in the lab yet, `corpus_id="gex-lab-v1"`, `facet="gex-lab"`, `unit="R"`; `thin_clusters` per the primitive's threshold — pass through whatever it reports, or `n_clusters < 8` if the primitive doesn't). Add `by_grade(session, *, account) -> list[dict]` grouping joined setups by `grade` (join `OptionSetup` via `setup_id`), one Stat-shaped dict per grade bucket — same clustering. Exact reuse mechanics depend on performance.py's actual API — read it and adapt; the test contract above is what matters.

**Step 4:** PASS, lint, commit: `feat(gex-lab): session-clustered lab stats`

---

## Task 16: CLI (`options/run.py`)

**Files:**
- Create: `src/swing_screener/options/run.py`
- Test: `tests/options/test_run.py`

Follows `pipeline/run.py`'s shape: argparse, `main()`, defaults from `load_settings()`, `python -m swing_screener.options.run <subcommand>`. Subcommands: `plan`, `settle`, `analyze <ticker>`, `import-robinhood <csv>`.

**Step 1: Failing tests** — test the orchestration functions with seams, not argparse:

```python
from datetime import date, datetime

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.db.models import GexSnapshot
from swing_screener.db.session import get_engine
from swing_screener.options.chain import ChainSnapshot
from swing_screener.options.config import GexConfig
from swing_screener.options.run import run_plan


def _fake_snapshotter(ticker, cfg):
    frame = pd.DataFrame([
        {"expiry": date(2026, 7, 17), "strike": 105.0, "right": "C",
         "open_interest": 50_000, "iv": 0.2},
        {"expiry": date(2026, 7, 17), "strike": 95.0, "right": "P",
         "open_interest": 40_000, "iv": 0.25},
    ])
    return ChainSnapshot(underlying=ticker, spot=100.0,
                         asof=datetime(2026, 7, 13, 9, 10), frame=frame)


def _fake_daily(ticker):
    return pd.DataFrame({
        "open": range(100, 220), "high": range(101, 221), "low": range(99, 219),
        "close": range(100, 220), "volume": [1e6] * 120,
    }, index=pd.date_range("2026-01-01", periods=120))


def test_run_plan_persists_snapshots_and_returns_plans() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        plans = run_plan(s, cfg=GexConfig(watchlist=("SPY",)),
                         snapshotter=_fake_snapshotter, daily_bars=_fake_daily)
        assert len(plans) == 1
        assert plans[0].underlying == "SPY"
        rows = s.query(GexSnapshot).all()
        assert len(rows) == 1 and rows[0].underlying == "SPY"
```

**Step 2:** FAIL. **Step 3: Implement:**

- `run_plan(session, *, cfg, snapshotter=None, daily_bars=None) -> list[DayPlan]`: defaults wire the real things (`snapshotter` → `snapshot_chain`; `daily_bars` → closure over `fetch_bars(t, "1d", cache_dir=load_settings().cache_dir)`); per watchlist ticker: snapshot → `assess_liquidity` → `compute_gex` → `save_snapshot` (with `thin` flag) → `build_plan`; per-ticker failure isolation (log + continue, like the pipeline).
- `run_settle(session, *, cfg, bars_fetcher=None) -> SettleResult`: collect underlyings of open lab trades; fetch each's 5m bars post-close (`fetch_bars(t, "5m", cache_dir=..., period="5d")` — post-close the day cache key is safe by the completed-bar argument in the design); call `settle_open_trades`.
- `run_analyze(ticker, *, cfg, snapshotter=None) -> tuple[GexLevels, LiquidityReport]` — no DB write unless `--save` (keep `save` a bool arg; CLI flag).
- `run_import(session, csv_path, *, tag_all: str | None) -> str` — parse → `store_fills` → `pair_episodes` → print an aligned episode table (occ symbol, span, contracts, pnl, status) → if `tag_all` in {"gex","other"} commit all closed episodes with that tag, else print "review in the cockpit Import panel to tag episodes" and commit nothing.
- `main()`: argparse with `subparsers`; shared `--db` (resolution copied from `pipeline/run.py`'s `_resolve_db_url` — including the cloud-refuses-sqlite guard) and `--cache-dir`; `logging.basicConfig(level=logging.INFO)`.

**Step 4:** PASS, lint, commit: `feat(gex-lab): CLI — plan / settle / analyze / import-robinhood`

---

## Task 17: Cockpit API router (`cockpit/gex_api.py`)

**Files:**
- Create: `src/swing_screener/cockpit/gex_api.py`
- Modify: `src/swing_screener/cockpit/api.py` (three small edits: import + include_router before the static mount + `_change_token` watermarks + two keyword-only seam params on `create_app`)
- Test: `tests/cockpit/test_gex_api.py`

**Constraints (from the api.py extraction):** router must be a factory taking the closure-scoped session dependency: `build_gex_router(session_dep, *, snapshotter, daily_bars) -> APIRouter`. In `create_app`, add keyword-only params `gex_snapshotter=None, gex_daily_bars=None` (the `login_spawner` seam pattern) and call `app.include_router(build_gex_router(_session, snapshotter=gex_snapshotter or snapshot_chain, daily_bars=gex_daily_bars or <real closure>))` BEFORE `app.mount("/", ...)`. Write endpoints 403 unless `request.headers.get("x-cockpit") == "1"`. Stats wear the 10-key Stat dict; levels are plain values.

**Endpoints:**

| Method | Path | Body/Query | Returns |
|---|---|---|---|
| GET | `/api/gex/plan` | — | latest snapshot + plan per watchlist underlying: `{plans: [{underlying, ts, spot, call_wall, put_wall, gamma_flip, regime, bias, call, thin_chain}]}`; empty list when none |
| POST | `/api/gex/plan/build` | optional `{"ticker": "NVDA"}` for ad-hoc | runs `run_plan` (watchlist) or single-ticker analyze+save; returns the same plans payload |
| GET | `/api/gex/setups` | `?day=YYYY-MM-DD` optional | journal rows incl. the 12 `chk_*` booleans, grade, status, linked trade outcome if closed |
| POST | `/api/gex/setups` | full checklist form | created setup (validates via `create_setup`; 422 on bad checklist keys via ValueError→HTTPException) |
| POST | `/api/gex/setups/{id}/status` | `{"status": "taken"\|"skipped"}` | updated setup |
| POST | `/api/gex/import/parse` | `{"csv_text": "..."}` | `{episodes: [...], fills_added, fills_skipped}` — stores fills, commits NO episodes |
| POST | `/api/gex/import/commit` | `{"tags": {import_key: "gex"\|"other"\|"skip"}}` | `{committed: n}` — re-pairs episodes from stored fills, commits tagged |
| GET | `/api/gex/stats` | — | `{overall: Stat, by_grade: [...], robinhood: {gex: Stat, other: Stat}}` |

Note `import/parse` takes JSON `csv_text`, not multipart — the file is small, the UI reads it client-side with `FileReader`, and it avoids adding python-multipart as a dependency. `import/commit` re-derives episodes from `BrokerFill` rows (`select` all fills → `FillRecord`s → `pair_episodes`) so parse/commit can happen in different processes.

**Step 1: Failing tests** (`tests/cockpit/test_gex_api.py`, following `test_api.py`'s `_db_url`/`_client` helpers — copy those two helpers in, plus `_HDR = {"X-Cockpit": "1"}`; build clients with fake `gex_snapshotter`/`gex_daily_bars` seams):

Write tests for: (1) `GET /api/gex/plan` empty → `{"plans": []}`; (2) `POST /api/gex/plan/build` without the header → 403; with header + fake seams → 200 with one plan carrying the fake's walls, and a `GexSnapshot` row exists; (3) `POST /api/gex/setups` with an all-true checklist → 200, grade "A+", then `GET /api/gex/setups` returns it; (4) `POST .../status {"taken"}` → paper trade opened (assert via a second GET showing status); (5) import parse→commit round trip on the Task-10 fixture text: parse returns episodes and fills_added > 0; commit with one `"gex"` tag → `{"committed": 1}`; re-commit → 0; (6) `GET /api/gex/stats` with seeded closed trades returns a 10-key Stat under `overall`; (7) add `"/api/gex/plan"` and `"/api/gex/stats"` to the DB-down 503 loop tuple in the existing style (new test in this file with `raise_server_exceptions=False`).

**Step 2:** FAIL. **Step 3: Implement** the router factory + the three `api.py` edits. `_change_token` gains three watermarks: `max(GexSnapshot.id)`, `max(OptionSetup.id)`, `max(OptionPaperTrade.id)` — note in a comment that OptionPaperTrade closes are UPDATEs, so also include `max(OptionPaperTrade.closed_at)` (the ExitEvent.id lesson: closes must wake the UI). Keep every route handler thin — parse/validate, call the options-package function, shape the wire dict by hand (house rule: no `dataclasses.asdict` on the wire).

**Step 4:** `$PY -m pytest tests/cockpit/ tests/options/ -q` → PASS; full suite; ruff; mypy. Commit: `feat(gex-lab): cockpit GEX router — plan, journal, import, stats + SSE watermarks`

---

## Task 18: Cockpit UI — GEX Lab view

**Files:**
- Create: `cockpit-ui/src/components/PanelBody.tsx` (extraction), `cockpit-ui/src/components/GexLab.tsx`
- Modify: `cockpit-ui/src/App.tsx` (minimal: import PanelBody instead of local def; add `view` state + masthead toggle + conditional render), `cockpit-ui/src/lib/api.ts` (types + fetchers)
- Commit also: rebuilt `src/swing_screener/cockpit/static/**`

No UI test framework exists — the gates are `npm run lint`, `npm run build`, and the API contract already tested in Task 17. Keep TS types mirroring the wire shapes exactly.

**Step 1:** Extract `PanelBody` verbatim from `App.tsx` into `components/PanelBody.tsx` (export it and the `Polled` import it needs); update `App.tsx` to import it. `npm run lint && npm run build` → clean. Commit the refactor alone (with rebuilt static): `refactor(cockpit-ui): extract PanelBody for reuse`.

**Step 2:** `api.ts` additions — interfaces `GexPlan`, `GexSetup` (12 `chk_*` booleans + fields), `GexEpisode`, `GexStats`; fetchers `getGexPlans()`, `postGexBuild(ticker?)`, `getGexSetups(day?)`, `postGexSetup(form)`, `postGexSetupStatus(id, status)`, `postGexImportParse(csvText)`, `postGexImportCommit(tags)`, `getGexStats()` — POSTs carry `{'X-Cockpit': '1'}` and JSON bodies (follow `postAzureLogin`'s shape plus `body: JSON.stringify(...)`, `headers: {..., 'Content-Type': 'application/json'}`).

**Step 3:** `GexLab.tsx` — one exported component rendering the five panels in the existing grid idiom (`section.panel` + `panel-head` + `PanelBody`), all polls via `usePolling(fetcher, POLL_MS, wake)` with `wake` passed down from App:

1. **DAY PLAN** — table of plans (underlying, spot, call wall, put wall, flip, regime badge, bias, call); "thin chain — levels unreliable" warning line when `thin_chain`; "Build today's plan" button → `postGexBuild()` then bump a local refresh key; an "Analyze ticker" text input + button → `postGexBuild(ticker)`.
2. **CHECKLIST GRADER** — controlled form: underlying, direction (long/put toggle), entry/stop/target/pivot numeric inputs, pattern + notes text, 12 checkboxes grouped under the four block headings (labels from a local const mirroring `CHECKLIST_ITEMS`), live computed grade preview (A+/B/no_trade — same rule, duplicated client-side for instant feedback), submit → `postGexSetup`.
3. **JOURNAL** — today's setups list: time, underlying, grade chip, status; buttons taken/skipped on `idea` rows; settled outcome (exit reason + R) when the linked trade is closed.
4. **LAB STATS** — `StatChip` rows (reuse the existing `StatChip` component) for overall + by-grade + the robinhood gex/other comparison.
5. **IMPORT** — `<input type="file">` → `FileReader.readAsText` → `postGexImportParse` → review grid (one row per episode: occ symbol, span, contracts, P&L, open/closed, needs-review badge; a three-way select gex/other/skip per row, default "skip") → "Commit tagged" button → `postGexImportCommit` → result line.

Local component state only (form fields, parse results); no new libraries; hand-rolled table markup in the existing panel CSS vocabulary.

**Step 4:** `App.tsx` minimal edit, but **module-shaped** (suite direction, docs/ARCHITECTURE.md): a small registry, not a hardcoded toggle —

```tsx
const MODULES = [
  { key: 'swing', label: 'SWING' },
  { key: 'gex', label: 'GEX LAB' },
] as const
type ModuleKey = (typeof MODULES)[number]['key']
```

`const [view, setView] = useState<ModuleKey>('swing')`; the nav renders `MODULES.map(...)` as segmented buttons (existing `.mh-seg`/`.seg-on` classes) so module #3 is a one-entry addition; when `view === 'gex'` render `<GexLab wake={wake} />` INSTEAD of the swing grid (masthead stays). Keep the diff otherwise small — the unexecuted Phase-3 plan will restructure this file.

**Step 5:** `npm run lint && npm run build`; verify `git status` shows only expected static changes; **manual verify** — from the worktree: `$PY -m swing_screener.cockpit --browser --port 8901` against a scratch DB seeded by the Task-17 test helpers (or point `SWING_DB_URL` at a tmp sqlite), click through: view toggle, build plan (will fail politely without network — the PanelBody error line is the acceptance), grade + submit a setup, import the FIXTURE csv, tag, commit. Screenshot for the PR.

**Step 6:** Commit (source + static together): `feat(gex-lab): GEX Lab cockpit view — day plan, grader, journal, stats, import review`

---

## Task 19: Playbook scaffold + README + final gates

**Files:**
- Create: `edge/gex.md`
- Modify: `README.md`

**Step 1:** `edge/gex.md` — follow the existing `edge/<play_type>.md` shape (read `edge/continuation.md` for the section skeleton): thesis ("GEX pivots + EMA alignment + A+ discipline produce positive expectancy on SPY/QQQ day trades — unproven, n=0"), status "accruing — no verdicts until the reflection family is registered (Phase 1.5)", links to the charter and design doc. No verdicts.json yet — the deterministic grader integration is explicitly deferred.

**Step 2:** README — add a short "GEX options lab" subsection under the system map: the four CLI commands, the cockpit tab, the charter link, and the phrase "paper + imported trades only; no execution path exists."

**Step 3: Final gates, in order:**
1. `$PY -m pytest -q` → everything green (expect ~1105 + ~45 new).
2. `$PY -m ruff check src tests alembic` → clean.
3. `$PY -m mypy` → clean.
4. `cd cockpit-ui && npm run lint && npm run build && cd ..` then `git status --porcelain src/swing_screener/cockpit/static` → empty (no uncommitted drift).
5. `$PY -m alembic heads` → single head `f2a9c4e7b1d8`.

**Step 4:** Commit: `docs(gex-lab): playbook scaffold + README section`. Then STOP — use superpowers:finishing-a-development-branch (PR onto main; the branch already contains the design-doc commits).

---

## Explicitly deferred (do NOT build in Phase 1)

- Reflection-family registration / verdicts for the lab (needs ~20 closed trades first).
- `run.py validate` beyond printing levels (the dashboard cross-check is a human ritual).
- Azure job, heartbeats, email — the lab is local-only by design.
- Intraday polling, TTL bar cache, live alerts, premium tracking for paper trades (Phase 2).
- Setup↔imported-trade linking UI (design has it; ship the journal first — linking lands with Phase 1.5 when there are trades to link).
