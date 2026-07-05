# Desktop Cockpit Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task.

**Goal:** A double-clickable local desktop app (pywebview window over a local FastAPI
backend) showing the Mission Control masthead, the heartbeat rail, and first Stat-object
readouts — while the Streamlit dashboard keeps running untouched.

**Architecture:** New package `src/swing_screener/cockpit/` (Stat contract, heartbeat
state machine, FastAPI app factory, launcher) + new frontend workspace `cockpit-ui/`
(Vite + React + TS) whose build output is committed to `cockpit/static/` so a fresh clone
needs zero Node. Design: `docs/plans/2026-07-05-desktop-ui-design.md`.

**Tech Stack:** FastAPI + uvicorn + pywebview (Python `[cockpit]` extra); Vite + React +
TypeScript frontend; existing SQLAlchemy repo/performance modules as the only data layer.

**Conventions that bind every task:** TDD (red before green — run the failing test
first); every module gets the repo's constraint-stating docstrings; `ruff check` + `mypy`
clean (the package is inside `packages=["swing_screener"]`, so CI type-checks it);
conventional commits ending with the Claude Code trailer used all over this repo.

---

### Task 1: `[cockpit]` extra

**Files:** Modify: `pyproject.toml` (optional-dependencies section)

**Step 1:** Add to `[project.optional-dependencies]`:

```toml
cockpit = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "pywebview>=5.2",
]
```

**Step 2:** `.\.venv\Scripts\python -m pip install -e ".[dev,cockpit]"` — expect clean
install.

**Step 3:** Commit: `feat(cockpit): add desktop-app dependency extra`

---

### Task 2: The Stat contract (`stats.py`)

The load-bearing rule: **the API never returns a bare float for a statistic.**

**Files:**
- Create: `src/swing_screener/cockpit/__init__.py` (empty)
- Create: `src/swing_screener/cockpit/stats.py`
- Test: `tests/cockpit/__init__.py` (empty), `tests/cockpit/test_stats.py`

**Step 1: Write the failing tests**

```python
"""The Stat contract: no statistic leaves the API as a bare number.

North Star #2 as a type: a Stat always carries n, clusters, both CI bounds, its cost
level, and its corpus id. Unknown provenance is an explicit None (the UI renders a
hollow 'not measured' tick), never a silent default.
"""

from swing_screener.analytics.performance import summarize
from swing_screener.cockpit.stats import Stat, stat_from_summary
from swing_screener.db.models import PaperTrade


def _trade(ticker: str, r: float) -> PaperTrade:
    return PaperTrade(ticker=ticker, timeframe="1d", horizon="medium",
                      status="closed", fill_status="filled", realized_r=r)


def test_stat_carries_full_provenance() -> None:
    s = Stat(value=0.057, n=9697, n_clusters=511, ci_low=0.028, ci_high=0.086,
             cost_level="0.05", corpus_id="pinned-20260703", facet="research",
             unit="R", thin=False)
    d = s.as_dict()
    for key in ("value", "n", "n_clusters", "ci_low", "ci_high", "cost_level",
                "corpus_id", "facet", "unit", "thin"):
        assert key in d


def test_stat_from_summary_maps_performance_fields() -> None:
    trades = [_trade(t, r) for t, r in
              [("AAA", 1.0), ("AAA", -1.0), ("BBB", 0.5), ("CCC", -0.2),
               ("DDD", 0.3), ("EEE", 0.1), ("FFF", -0.4), ("GGG", 0.8),
               ("HHH", 0.2), ("III", -0.1)]]
    summary = summarize(trades)
    s = stat_from_summary(summary, cost_level=None, corpus_id=None, facet="research")
    assert s.value == summary.expectancy_r
    assert s.n == summary.n_closed
    assert s.n_clusters == summary.n_clusters
    assert s.ci_low == summary.expectancy_ci_low
    assert s.cost_level is None          # honest unknown, not a guessed default
    assert s.thin == summary.thin_clusters
```

**Step 2:** Run: `.\.venv\Scripts\python -m pytest tests/cockpit/test_stats.py -q`
Expected: FAIL — `ModuleNotFoundError: swing_screener.cockpit`.

**Step 3: Implement `src/swing_screener/cockpit/stats.py`**

```python
"""The Stat contract: the cockpit API's ONLY numeric currency.

The frontend has no renderer for a bare float -- every statistic crosses the wire as a
Stat carrying its n, cluster count, both CI bounds, cost level, corpus id, and facet
(North Star #2 enforced by type, not by review). ``cost_level`` / ``corpus_id`` are
``None`` when the source aggregate does not yet persist them -- the UI renders a hollow
'not measured' tick for those; inventing a default here would be presenting a biased
number as real.
"""

from dataclasses import asdict, dataclass

from swing_screener.analytics.performance import PerformanceSummary


@dataclass(frozen=True)
class Stat:
    value: float
    n: int
    n_clusters: int
    ci_low: float
    ci_high: float
    cost_level: str | None
    corpus_id: str | None
    facet: str
    unit: str
    thin: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def stat_from_summary(
    summary: PerformanceSummary,
    *,
    cost_level: str | None,
    corpus_id: str | None,
    facet: str,
    unit: str = "R",
) -> Stat:
    """Wrap a PerformanceSummary's expectancy as a Stat (bounds are the summary's own
    hardened clustered bounds -- nothing is recomputed here)."""
    return Stat(
        value=summary.expectancy_r,
        n=summary.n_closed,
        n_clusters=summary.n_clusters,
        ci_low=summary.expectancy_ci_low,
        ci_high=summary.expectancy_ci_high,
        cost_level=cost_level,
        corpus_id=corpus_id,
        facet=facet,
        unit=unit,
        thin=summary.thin_clusters,
    )
```

(Check `PerformanceSummary`'s real field names in
`src/swing_screener/analytics/performance.py` before running — they are the source of
truth, not this plan.)

**Step 4:** Re-run the test — expect PASS.

**Step 5:** `ruff check src/swing_screener/cockpit tests/cockpit && mypy` — clean.

**Step 6:** Commit: `feat(cockpit): Stat contract -- no statistic leaves the API bare`

---

### Task 3: Heartbeat state machine (`heartbeats.py`)

**Files:**
- Create: `src/swing_screener/cockpit/heartbeats.py`
- Test: `tests/cockpit/test_heartbeats.py`

**Step 1: Write the failing tests** (pure state machine first, DB assembly second)

```python
"""Heartbeats: Up / Late / Down from explicit Period + Grace; UNKNOWN is a first-class
state (an unconfigured or dead poller must never read as green)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from swing_screener.cockpit.heartbeats import Heartbeat, beat_state, collect_heartbeats
from swing_screener.db.models import EmailLog
from swing_screener.db.session import get_engine

NOW = datetime(2026, 7, 5, 16, 0, tzinfo=UTC)


def test_state_machine_up_late_down_unknown() -> None:
    period, grace = timedelta(hours=24), timedelta(hours=1)
    up = NOW - timedelta(hours=20)
    late = NOW - timedelta(hours=24, minutes=30)
    down = NOW - timedelta(hours=26)
    assert beat_state(up, NOW, period, grace) == "up"
    assert beat_state(late, NOW, period, grace) == "late"
    assert beat_state(down, NOW, period, grace) == "down"
    assert beat_state(None, NOW, period, grace) == "unknown"


def test_collect_reads_email_log_for_digests() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(EmailLog(sent_at=NOW - timedelta(hours=8), kind="daily",
                       subject="x", run_date=NOW.date()))
        s.commit()
        beats = {b.name: b for b in collect_heartbeats(s, now=NOW)}
    assert beats["daily digest"].state == "up"
    assert beats["weekly digest"].state == "unknown"     # no row ever -> unknown
    assert beats["GH · optimizer"].state == "unknown"     # poller not configured
    assert all(isinstance(b, Heartbeat) for b in beats.values())
```

**Step 2:** Run — expect FAIL (`ModuleNotFoundError`).

**Step 3: Implement.** `Heartbeat` frozen dataclass
(`name, state, last: datetime | None, period_s: int, grace_s: int, detail: str`).
`beat_state(last, now, period, grace)` returns `"unknown" | "up" | "late" | "down"`
(`last is None` → unknown; `now - last <= period` → up; `<= period + grace` → late; else
down). `collect_heartbeats(session, *, now)` assembles the Phase-1 roster:

| name | source | period / grace |
|---|---|---|
| evening screen | max `Signal.run_date` (as end-of-day UTC) | 24h+2d weekend slack / 45m |
| daily digest | latest `EmailLog` kind=daily `sent_at` | 24h+2d weekend slack / 45m |
| weekly digest | `EmailLog` kind=weekly | 7d / 3h |
| monthly digest | `EmailLog` kind=monthly | 31d / 2d |
| market weather | max `MarketReport.run_date` | 7d / 3h |
| reflection verdicts | newest `edge/*.verdicts.json` mtime via `settings.resolve_edge_dir` | 7d / 3h |
| GH · optimizer / GH · reflection / GH · CI | none in Phase 1 | UNKNOWN, detail "poller not configured" |

Weekend slack: for the two weekday jobs use `period = 72h` flat in Phase 1 and note the
refinement (business-day calendar) as a Phase-2 TODO in the docstring — a Friday run must
not read LATE on Sunday.

**Step 4:** Re-run — PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): heartbeat state machine -- Up/Late/Down/UNKNOWN from Period+Grace`

---

### Task 4: FastAPI app factory (`api.py`)

**Files:**
- Create: `src/swing_screener/cockpit/api.py`
- Test: `tests/cockpit/test_api.py`

**Step 1: Failing tests** (FastAPI `TestClient`; seed an on-disk tmp sqlite via the
existing `get_engine`, pass its URL to `create_app`):

- `test_health_reports_connection_label_without_credentials` — GET `/api/health` →
  `{"connected": true, "label": "Local SQLite · <file>"}`; label must never contain the
  raw URL (mirror the Streamlit chip's guarantee; port the small label helper, do not
  import streamlit).
- `test_heartbeats_endpoint_returns_states` — GET `/api/heartbeats` → list where every
  item has `name/state/last/period_s/grace_s/detail` and unknown states are present.
- `test_cohort_stats_are_stat_objects` — seed a handful of closed research
  `PaperTrade`s (reuse Task 2's `_trade` pattern with `account="research"`,
  `play_type="reversal"`, `strength="confirmed"`), GET `/api/stats/cohorts` → every
  numeric entry carries the full Stat key set (assert the same 10 keys as Task 2) with
  `cost_level is None` (not yet persisted — honest unknown).
- `test_db_down_is_a_friendly_503` — `create_app("sqlite:///Z:/nope/x.db")` (or an
  mssql URL with no driver): `/api/health` → 200 with `connected: false` and an error
  summary; `/api/heartbeats` → 503 JSON, never a stack trace.

**Step 2:** Run — FAIL. **Step 3:** Implement `create_app(db_url: str) -> FastAPI` with
a per-request `Session` dependency, the four routes above, and
`app.state.db_url` for the launcher. `/api/stats/cohorts` = `breakdown(load_research_
paper_trades(session), "play_type")` + per-(play_type, strength) split, each wrapped by
`stat_from_summary(..., cost_level=None, corpus_id=None, facet="research")`.

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): FastAPI app factory -- health, heartbeats, first Stat endpoints`

---

### Task 5: Static serving + launcher (`__main__.py`)

**Files:**
- Create: `src/swing_screener/cockpit/__main__.py`
- Modify: `src/swing_screener/cockpit/api.py` (mount static)
- Test: `tests/cockpit/test_launcher.py`

**Step 1: Failing tests:** `_pick_free_port()` returns a bindable localhost port;
`create_app` serves `/` from `cockpit/static/index.html` when present (write a stub file
in tmp and point the app at it via a `static_dir` parameter defaulting to the packaged
path); `--browser` mode is selected when pywebview is unavailable (parse-args unit test —
never import pywebview at module scope).

**Step 2:** FAIL. **Step 3:** Implement: argparse (`--db`, `--port`, `--browser`);
uvicorn in a daemon thread on `127.0.0.1:<free port>`; then either
`webview.create_window("Swing Screener", url, width=1480, height=960)` +
`webview.start()` (imported lazily) or `webbrowser.open(url)` with a wait loop under
`--browser`. Single instance: bail politely if the port-file
(`%LOCALAPPDATA%/swing-screener/cockpit.port`) points at a live server.

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): double-clickable launcher -- uvicorn thread + pywebview window`

---

### Task 6: Frontend workspace (`cockpit-ui/`)

No pytest here — the gate is `npm run build` + eyes on the running app. Keep components
dumb; all logic lives behind the API.

**Step 1:** `npm create vite@latest cockpit-ui -- --template react-ts` (Node ≥ 20), then
in `cockpit-ui/vite.config.ts` set `build.outDir: "../src/swing_screener/cockpit/static"`,
`emptyOutDir: true`, and a dev proxy `/api → http://127.0.0.1:8901`.

**Step 2:** Implement, copying tokens verbatim from the validated mockup
(`docs/plans/2026-07-05-desktop-ui-design.md` + the session mockup file):
- `src/tokens.css` — the palette (`--bg:#0B0E13` etc.), Segoe UI Variable / Cascadia
  Mono stacks, `tabular-nums`.
- `src/components/StatChip.tsx` — value + graded 50/80/95 underbar + `n·clusters`
  badge + cost glyph; renders the n<5 badge / THIN dimming / hollow not-measured states
  from the Stat object alone.
- `src/components/HeartbeatRail.tsx` — tri-state lamp rows + UNKNOWN dashed state,
  Period+Grace in the row tooltip.
- `src/components/Masthead.tsx` — caution lamp (lit if any beat is late/down), DB label
  from `/api/health`, data-as-of clock; cost selector and DISARM render disabled with
  "Phase 2" tooltips (visible commitments, dead controls beat absent ones).
- `src/App.tsx` — masthead + rail + a "Cohorts" panel of StatChips from
  `/api/stats/cohorts`; 60 s polling (SSE is Phase 2).

**Step 3:** `npm run build` → `cockpit/static/` populated. Add `cockpit-ui/node_modules`
to `.gitignore`; the **built `static/` output is committed** (zero-Node clones run).

**Step 4:** Manual verify: `.\.venv\Scripts\python -m swing_screener.cockpit --browser`
→ masthead + live heartbeats + cohort chips against `local.db`.

**Step 5:** Commit: `feat(cockpit): Vite/React frontend -- masthead, heartbeat rail, StatChip`

---

### Task 7: Shortcut + docs

**Files:**
- Create: `scripts/make_cockpit_shortcut.ps1` (WScript.Shell → Start-menu `.lnk` to
  `venv\Scripts\pythonw.exe -m swing_screener.cockpit`, icon optional, `WorkingDirectory`
  = repo root)
- Create: `docs/cockpit.md` (quickstart: install extra → run shortcut script → double-
  click; dev loop: `npm run dev` + `--browser`; Azure switch is Phase 2)
- Modify: `docs/dashboard.md` (banner: superseded by the cockpit, phase-out plan link)

**Steps:** write, run the shortcut script once, double-click the shortcut, confirm the
window opens with live data. Commit: `docs(cockpit): quickstart + Start-menu shortcut script`

---

### Task 8: Ship

**Steps:** full `pytest -q` green; `ruff check` + `mypy` clean; push branch
`feat/cockpit-phase1`; PR titled `feat(cockpit): Phase 1 -- desktop shell, heartbeat
rail, Stat contract` with before/after startup story in the body; CI green; merge per
the repo's normal flow.

---

**Explicit non-goals for Phase 1** (they are Phases 2–3, per the design doc): settlement
cards, funnel, cost selector wiring, facet toggle, the six actions incl. DISARM, tray +
notifications, GH/Azure pollers, PyInstaller packaging, Azure DB switcher, retiring any
Streamlit page.
