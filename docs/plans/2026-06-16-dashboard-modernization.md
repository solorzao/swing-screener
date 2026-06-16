# Dashboard Modernization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Modernize the local Streamlit dashboard into a clean light-SaaS app with sidebar
navigation, rich tables/charts, an inline close-trade action, views for every model table,
and no raw stack traces ever reaching the screen.

**Architecture:** Single-script Streamlit app (`dashboard/app.py`) keeps its testable
`_render_*(session)` page-body seams. A new `dashboard/ui.py` holds shared presentation
helpers (theme/CSS, page header, empty state, error boundary, formatters, Altair charts).
Navigation is a styled `st.sidebar.radio` (driveable by `AppTest`, unlike `st.navigation`).
Backend changes are limited to ODBC driver auto-detection and two read-only repo helpers.

**Tech Stack:** Streamlit 1.58, Altair 6.2 (bundled — no Plotly dependency), SQLAlchemy 2,
pandas 3, pytest + `streamlit.testing.v1.AppTest`.

## Design deviations (from the approved design doc)

- **Charts use Altair, not Plotly** — Altair ships with Streamlit; Plotly is not installed.
  Avoids a new dependency. Same UX outcome (tooltips, palette, axis control).
- **Navigation uses `st.sidebar.radio`, not `st.navigation`** — `AppTest` can switch radio
  pages but exposes no API to switch `st.navigation` pages, which would break per-view tests.

## Conventions

- Run tests: `./.venv/Scripts/python -m pytest <path> -v`
- Lint/type before each commit: `./.venv/Scripts/python -m ruff check src tests` and
  `./.venv/Scripts/python -m mypy`
- Commit message style: conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
- Branch: `feat/dashboard-modernization` (already created; design doc already committed).

---

## Phase A — Backend robustness & data access (no UI)

### Task 1: ODBC driver auto-detection

Fixes the `IM002` crash: the URL hardcodes "ODBC Driver 18 for SQL Server" but this machine
only has Driver 17. Auto-pick the best installed SQL Server driver.

**Files:**
- Modify: `src/swing_screener/db/session.py`
- Test: `tests/db/test_session_mssql.py`

**Step 1: Write failing tests** (append to `tests/db/test_session_mssql.py`)

```python
from swing_screener.db.session import _best_sql_server_driver, _resolve_driver


def test_best_driver_picks_highest_numbered():
    avail = ["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]
    assert _best_sql_server_driver(avail) == "ODBC Driver 18 for SQL Server"


def test_best_driver_none_when_absent():
    assert _best_sql_server_driver(["Microsoft Access Driver (*.mdb)"]) is None


def test_resolve_driver_keeps_requested_when_installed():
    avail = ["ODBC Driver 18 for SQL Server"]
    assert _resolve_driver("ODBC Driver 18 for SQL Server", avail) == "ODBC Driver 18 for SQL Server"


def test_resolve_driver_falls_back_to_installed_when_requested_missing():
    avail = ["ODBC Driver 17 for SQL Server"]
    assert _resolve_driver("ODBC Driver 18 for SQL Server", avail) == "ODBC Driver 17 for SQL Server"


def test_resolve_driver_keeps_requested_when_nothing_installed():
    assert _resolve_driver("ODBC Driver 18 for SQL Server", []) == "ODBC Driver 18 for SQL Server"
```

**Step 2: Run — expect FAIL** (`ImportError`/`AttributeError`).
`./.venv/Scripts/python -m pytest tests/db/test_session_mssql.py -v`

**Step 3: Implement** in `session.py` (add helpers; use them in `_mssql_odbc_url`)

```python
def _available_odbc_drivers() -> list[str]:
    """Installed ODBC drivers, or [] if pyodbc is unavailable (optional azure extra)."""
    try:
        import pyodbc
    except Exception:
        return []
    return list(pyodbc.drivers())


def _best_sql_server_driver(available: list[str]) -> str | None:
    """Highest-numbered 'ODBC Driver NN for SQL Server' among `available`, else None."""
    def version(name: str) -> int:
        for tok in name.split():
            if tok.isdigit():
                return int(tok)
        return -1
    cands = [d for d in available
             if d.lower().startswith("odbc driver") and d.lower().endswith("for sql server")]
    return max(cands, key=version) if cands else None


def _resolve_driver(requested: str, available: list[str]) -> str:
    """Use `requested` if installed; else the best installed SQL Server driver; else `requested`."""
    if requested in available:
        return requested
    return _best_sql_server_driver(available) or requested
```

In `_mssql_odbc_url`, change the `driver = ...` line to resolve against installed drivers:

```python
    requested = first(u.query.get("driver", "ODBC Driver 18 for SQL Server"))
    driver = _resolve_driver(requested, _available_odbc_drivers())
```

**Step 4: Run — expect PASS.** Also run the whole file to confirm no regression on the
existing mssql tests.

**Step 5: Commit**
```bash
git add src/swing_screener/db/session.py tests/db/test_session_mssql.py
git commit -m "fix(db): auto-detect installed ODBC driver instead of hardcoding 18"
```

### Task 2: `repo.list_universe`

**Files:** Modify `src/swing_screener/db/repo.py`; Test `tests/db/test_repo.py`.

**Step 1: Failing test**
```python
def test_list_universe_filters_by_ticker(session):  # session fixture = in-memory engine
    from swing_screener.db.models import Universe
    session.add_all([Universe(ticker="AMD", name="Advanced Micro"),
                     Universe(ticker="NVDA", name="Nvidia")])
    session.commit()
    from swing_screener.db import repo
    assert [u.ticker for u in repo.list_universe(session)] == ["AMD", "NVDA"]
    assert [u.ticker for u in repo.list_universe(session, search="nv")] == ["NVDA"]
```
(If `tests/db/test_repo.py` has no `session` fixture, build the engine inline like the other
tests do: `engine = get_engine("sqlite:///:memory:"); with Session(engine) as session: ...`.)

**Step 2: Run — FAIL.**

**Step 3: Implement** (add `Universe` to the model import, add the function)
```python
def list_universe(session: Session, search: str | None = None) -> list[Universe]:
    stmt = select(Universe).order_by(Universe.ticker)
    if search:
        stmt = stmt.where(Universe.ticker.like(f"%{search.upper()}%"))
    return list(session.scalars(stmt))
```

**Step 4: Run — PASS.**

**Step 5: Commit** `feat(db): add list_universe read helper for the dashboard`.

### Task 3: `repo.list_email_log`

**Files:** Modify `repo.py`; Test `tests/db/test_repo.py`.

**Step 1: Failing test**
```python
def test_list_email_log_newest_first():
    from datetime import datetime
    from swing_screener.db.models import EmailLog
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([EmailLog(sent_at=datetime(2026,1,1), kind="daily", subject="old"),
                   EmailLog(sent_at=datetime(2026,1,2), kind="weekly", subject="new")])
        s.commit()
        from swing_screener.db import repo
        assert [e.subject for e in repo.list_email_log(s)] == ["new", "old"]
```

**Step 2: FAIL → Step 3: Implement**
```python
def list_email_log(session: Session) -> list[EmailLog]:
    return list(session.scalars(select(EmailLog).order_by(EmailLog.sent_at.desc())))
```
(Add `EmailLog` to imports.)

**Step 4: PASS → Step 5: Commit** `feat(db): add list_email_log read helper`.

---

## Phase B — Shared UI foundation

### Task 4: Light SaaS theme + chrome CSS

**Files:** Modify `.streamlit/config.toml`; Create `src/swing_screener/dashboard/ui.py`.

**Step 1:** Add a `[theme]` block to `.streamlit/config.toml` (keep existing `[server]`/`[browser]`):
```toml
[theme]
base = "light"
primaryColor = "#4F46E5"
backgroundColor = "#FFFFFF"
secondaryBackgroundColor = "#F5F6F8"
textColor = "#111827"
font = "sans serif"
```

**Step 2:** Create `dashboard/ui.py` with a CSS injector that hides default chrome and styles
metric cards + nav. (No unit test for CSS; it's covered by the app smoke test not raising.)
```python
"""Shared presentation helpers for the dashboard (theme, layout, formatting, charts)."""
import streamlit as st

ACCENT = "#4F46E5"
POS = "#16A34A"  # P/L positive (green)
NEG = "#DC2626"  # P/L negative (red)

_CSS = """
<style>
/* app-like chrome: hide Deploy button + main menu + footer */
[data-testid="stToolbar"], #MainMenu, footer {visibility: hidden;}
.block-container {padding-top: 2.5rem; max-width: 1200px;}
/* metric cards */
[data-testid="stMetric"] {background:#FFF;border:1px solid #E5E7EB;border-radius:12px;
  padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
/* sidebar nav radio -> nav-link look */
section[data-testid="stSidebar"] [role="radiogroup"] label {padding:6px 10px;border-radius:8px;}
section[data-testid="stSidebar"] [role="radiogroup"] label:hover {background:#EEF0FF;}
</style>
"""

def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
```

**Step 3:** No test beyond app smoke (Task 6). **Commit** `feat(dashboard): light SaaS theme + chrome CSS`.

### Task 5: UI helpers — page header, empty state, error boundary, formatters, charts

**Files:** Modify `dashboard/ui.py`; Test `tests/dashboard/test_ui.py` (new).

Pure helpers get unit tests; Streamlit-context helpers are exercised by the app smoke test.

**Step 1: Failing tests** (`tests/dashboard/test_ui.py`)
```python
from swing_screener.dashboard import ui

def test_pct_formats_with_sign():
    assert ui.fmt_pct(0.1234) == "+12.34%"
    assert ui.fmt_pct(-0.05) == "-5.00%"

def test_money_formats_currency():
    assert ui.fmt_money(1234.5) == "$1,234.50"
    assert ui.fmt_money(-12.0) == "-$12.00"

def test_pl_color_picks_semantic_color():
    assert ui.pl_color(3.0) == ui.POS
    assert ui.pl_color(-1.0) == ui.NEG
    assert ui.pl_color(0.0) == ui.NEG  # break-even is not a win
```

**Step 2: FAIL → Step 3: Implement** the pure formatters in `ui.py`:
```python
def fmt_pct(frac: float) -> str:
    return f"{frac * 100:+.2f}%"

def fmt_money(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"

def pl_color(value: float) -> str:
    return POS if value > 0 else NEG
```
Also add Streamlit-context helpers (no unit test; smoke-covered):
```python
from collections.abc import Iterator
from contextlib import contextmanager

def page_header(title: str, caption: str | None = None) -> None:
    st.header(title)
    if caption:
        st.caption(caption)

def empty_state(message: str) -> None:
    st.info(message, icon="📭")

@contextmanager
def error_boundary(view_name: str) -> Iterator[None]:
    """Render any exception as a calm card with collapsible details — never a stack trace."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — top of a view; we intentionally catch all
        st.error(f"Couldn't load **{view_name}**.", icon="⚠️")
        with st.expander("Technical details"):
            st.code(f"{type(exc).__name__}: {exc}")
```

**Step 4: PASS → Step 5: Commit** `feat(dashboard): shared UI helpers (formatters, error boundary, empty state)`.

---

## Phase C — App restructure to sidebar navigation

### Task 6: Replace tabs with styled sidebar radio nav + per-view error boundary

**Files:** Modify `dashboard/app.py`; Modify `tests/dashboard/test_app_smoke.py`.

**Approach:** Keep all `_render_*(session)` functions. Replace the `st.tabs(...)` block in
`render()` with a `PAGES` registry `{label: (group, renderer)}` and a `st.sidebar.radio`.
Wrap each renderer call in `ui.error_boundary(label)`. Call `ui.inject_css()` once. Replace
the raw `st.sidebar.caption(f"DB: {db_url}")` leak with a connection chip (Task 16 finalizes).

```python
PAGES: dict[str, tuple[str, Callable[[Session], None]]] = {
    "Overview": ("Main", _render_overview),
    "Today's Candidates": ("Signals", _render_candidates),
    "Active Trades": ("Trades", _render_active),
    "Trade Entry": ("Trades", _render_entry),
    "Closed Trades": ("Trades", _render_closed),
    "Screener Performance": ("Analytics", _render_performance),
    "Exit Log": ("Analytics", _render_exits),
    "Universe": ("Reference", _render_universe),
    "Digest Log": ("Reference", _render_digests),
}

def render() -> None:
    ui.inject_css()
    db_url = load_settings().db_url
    engine = get_engine(db_url)
    st.sidebar.title("📈 Swing Screener")
    choice = st.sidebar.radio("Navigate", list(PAGES), label_visibility="collapsed")
    renderer = PAGES[choice][1]
    with Session(engine) as session:
        with ui.error_boundary(choice):
            renderer(session)
```

(`_render_overview`, `_render_universe`, `_render_digests` are stubbed in this task — e.g.
`def _render_overview(session): ui.page_header("Overview")` — and fleshed out in Phase D so
the app imports/runs. Keep `TAB_LABELS` deleted or repurposed.)

**Step 1: Update smoke tests** — the `== 6 tabs` assertions no longer hold. Rewrite
`test_app_renders_on_empty_db` and `test_app_renders_with_seeded_data`:
```python
def test_app_renders_on_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert at.sidebar.radio[0].value == "Overview"  # default page

def test_every_page_renders_with_seeded_data(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dash.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 104.0})
    _seed(url)
    at = AppTest.from_file(APP).run()
    pages = ["Overview","Today's Candidates","Active Trades","Trade Entry",
             "Closed Trades","Screener Performance","Exit Log","Universe","Digest Log"]
    for p in pages:
        at.sidebar.radio[0].set_value(p).run()
        assert not at.exception, f"{p} raised"
```
Keep `test_active_trades_survives_malformed_trade` but drive it to the Active Trades page.

**Step 2: Run — FAIL** (renderers/registry not present).
**Step 3: Implement** the registry + stubs.
**Step 4: Run — PASS** (`tests/dashboard/test_app_smoke.py -v`).
**Step 5: Commit** `feat(dashboard): sidebar navigation + per-view error boundary`.

---

## Phase D — Per-view upgrades

> Each task: update the `_render_*` body, then extend the seeded smoke test to assert the new
> content/columns appear. Run `tests/dashboard/ -v` after each. Commit per task.

### Task 7: Overview (new landing)
KPI row via `st.columns` + `st.metric`: open positions (`len(get_open_trades)`), total
unrealized P/L (sum over open trades using `quotes.latest_closes` + `position_pl`, guarded),
today's candidate count (`latest_signals(latest_run_date)`), screener win rate
(`performance.summarize(all paper_trades).win_rate`). Each metric guarded so a missing quote
or empty table shows "—", never raises. Below: a "Latest run" caption + small recent-activity
list (most recent exit events). Commit `feat(dashboard): overview landing page`.

### Task 8: Today's Candidates
- Build a `pandas.DataFrame` including the hidden fields: `play_type`, `strength`, `rank`,
  `rsi`, `atr`, `oversold` plus existing columns.
- Play-type filter: `st.segmented_control` or `st.radio` (horizontal) over
  `["All","Continuation","Reversal"]`; filter rows.
- Render with `st.dataframe(df, column_config=...)`:
  ```python
  column_config={
    "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=1, format="%.2f"),
    "entry_floor": st.column_config.NumberColumn("Entry ▼", format="$%.2f"),
    "entry_ceiling": st.column_config.NumberColumn("Entry ▲", format="$%.2f"),
    "stop": st.column_config.NumberColumn("Stop", format="$%.2f"),
    "target": st.column_config.NumberColumn("Target", format="$%.2f"),
    "rsi": st.column_config.NumberColumn("RSI", format="%.0f"),
  }
  ```
- Move each chart into `with st.expander(f"{ticker} chart"):` instead of stacking images.
- Smoke assertion: after switching to the page, the seeded "AMD" appears and no exception.
- Commit `feat(dashboard): rich candidates table with play-type filter and chart expanders`.

### Task 9: Active Trades + inline close action
- Keep the live-P/L table; format with `column_config` (NumberColumn `$`/`%`, the 🟢/🟡/🔴
  badge stays as a text column). Add a "Dist to stop/target" `ProgressColumn` if straightforward.
- Below the table, a **Close trade** form: `st.selectbox` of open trades (label
  `f"{id} · {ticker}"`), `exit_date`, `exit_price`, `exit_reason`; on submit call
  `repo.close_trade(...)` then `st.success` + `st.rerun()`.
- New test in `tests/dashboard/test_app_smoke.py` or `tests/db/test_trade_repo.py`: closing
  via `repo.close_trade` moves a trade to `get_closed_trades`. (close_trade is already tested;
  add a UI-form smoke: submit the form button through AppTest and assert no exception.)
- Commit `feat(dashboard): close trades from the Active Trades view`.

### Task 10: Trade Entry polish
Two-column layout (`st.columns`) for the inputs, keep existing validation, add `st.help`
captions. Optional: a "Prefill from candidate" selectbox that fills ticker/stop/target from
today's signals. Smoke: submit a valid trade through AppTest → `st.success`. Commit
`feat(dashboard): cleaner trade-entry form`.

### Task 11: Closed Trades polish
`column_config` money/date formatting; keep cumulative metric; add an Altair equity curve of
realized P/L (running sum of `realized_$` by `exit_date`). Commit
`feat(dashboard): formatted closed-trades table + realized equity curve`.

### Task 12: Screener Performance — Altair charts
Replace `st.bar_chart`/`st.line_chart` with Altair helpers in `ui.py`:
```python
import altair as alt, pandas as pd
def bar(data: dict[str, float], x_title: str, y_title: str):
    df = pd.DataFrame({"k": list(data), "v": list(data.values())})
    return alt.Chart(df).mark_bar(color=ACCENT).encode(
        x=alt.X("k:N", title=x_title), y=alt.Y("v:Q", title=y_title),
        tooltip=["k","v"]).properties(height=240)
def line(points: list[tuple], x_title: str, y_title: str):
    df = pd.DataFrame(points, columns=["x","y"])
    return alt.Chart(df).mark_line(point=True, color=ACCENT).encode(
        x=alt.X("x:T", title=x_title), y=alt.Y("y:Q", title=y_title),
        tooltip=["x","y"]).properties(height=260)
```
Render with `st.altair_chart(ui.line(curve, "Date", "Cumulative R"), use_container_width=True)`.
Keep the 5 KPI metrics. Commit `feat(dashboard): Altair charts for screener performance`.

### Task 13: Exit Log filter
Add a `st.multiselect` over distinct `reason` values + an `is_paper` toggle; format the table.
Commit `feat(dashboard): filterable exit log`.

### Task 14: Universe view (new)
`_render_universe`: `st.text_input` search → `repo.list_universe(session, search)` → formatted
`st.dataframe` (market_cap, avg_dollar_volume as NumberColumn). Empty → `ui.empty_state`.
Extend `_seed` to add a `Universe` row; assert it appears. Commit `feat(dashboard): universe view`.

### Task 15: Digest Log view (new)
`_render_digests`: `repo.list_email_log(session)` → table (sent_at, kind, subject, run_date).
Empty → `ui.empty_state`. Commit `feat(dashboard): digest/email log view`.

---

## Phase E — Robustness & polish

### Task 16: Connection guard + clean status chip
- Wrap the `get_engine(db_url)` + first use in `render()` so a DB-down condition shows a
  friendly full-page card (reuse `ui.error_boundary` pattern at the top level) instead of a
  trace. Verify by pointing `SWING_DB_URL` at an unreachable mssql URL in a test and asserting
  `not at.exception` and that a friendly message renders.
- Replace the raw `DB: <full connection string>` sidebar caption with a chip:
  `🟢 Azure SQL · swing` for mssql URLs, `🟢 Local SQLite` otherwise — parse the URL, never
  print credentials/host query string.
- Commit `feat(dashboard): friendly DB-connection guard and clean status chip`.

### Task 17: Docs
Update `docs/dashboard.md`: new navigation/views, Azure-connect note (driver auto-detect means
Driver 17 or 18 both work locally), remove the stale "six tabs" framing. Commit
`docs: refresh dashboard guide for the modernized UI`.

### Task 18: Final verification
- `./.venv/Scripts/python -m pytest -q` (full suite green)
- `./.venv/Scripts/python -m ruff check src tests` (clean)
- `./.venv/Scripts/python -m mypy` (clean)
- Relaunch against Azure SQL and screenshot each page; confirm real history loads and no errors.
- Commit any doc/screenshot fixups. Then open a PR.

---

## Risks / notes

- **`AppTest` + radio nav**: verified working (default page renders; `set_value(...).run()`
  switches). Per-view tests rely on this.
- **`pyodbc` optional**: driver detection imports it lazily and returns `[]` if absent, so CI
  (no azure extra) is unaffected; `_resolve_driver`/`_best_sql_server_driver` are tested purely.
- **pandas 3 / Streamlit 1.58**: `st.dataframe` accepts a DataFrame; build frames explicitly to
  control column order and `column_config` keys.
- **Azure write caveat**: Trade Entry and the new Close action write to whatever `SWING_DB_URL`
  targets — i.e. production when pointed at Azure. Acceptable per the design.
