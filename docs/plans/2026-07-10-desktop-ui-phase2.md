# Desktop Cockpit Phase 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task.

**Goal:** Forward Books settlement cards, the reversal FunnelBar, the Streamlit
Performance/Health parity surfaces, cost/facet wiring, SSE push, and the Phase-2 heartbeat
upgrades — after which the Streamlit "Screener Performance" and "System Health" pages
retire.

**Architecture:** All new statistics ride the existing 10-key `Stat` contract (which does
NOT change — its key set is asserted closed in tests). Backend grows four seams: a
machine-readable experiment registry (`edge/experiments.json` + `pipeline/registry.py`),
a settlement engine (`cockpit/settlement.py`) built on the repo's two honest delta
primitives, a persisted reversal-funnel table (new model + Alembic migration + write site
in `notify/run.py`), and new read endpoints on `cockpit/api.py`. Frontend grows the
SettlementCard/FunnelBar/Sparkline components, facet + cost masthead wiring, and an SSE
wake channel that piggybacks on the existing polling guarantees. Design:
`docs/plans/2026-07-05-desktop-ui-design.md`.

**Tech Stack:** FastAPI + sse-starlette (new dep in the `[cockpit]` extra) + SQLAlchemy +
Alembic; Vite + React 19 + TS 6 (no chart library — sparklines are inline SVG).

**Conventions that bind every task:** TDD (red before green — run the failing test
first); `@dataclass(frozen=True, kw_only=True)` for every new dataclass; narrative module
docstrings stating the invariant; `ruff check src tests alembic` + bare `mypy` clean;
tests fully type-annotated; conventional commits with the Claude trailer; cockpit API
tests use tmp_path FILE sqlite (`.as_posix()`), never `:memory:` (create_app builds its
own engine from the URL). Frontend: `npm run build` (tsc -b is a hard gate), rebuild +
commit `static/` in the same commit as any UI source change (`emptyOutDir: true` wipes
it). New API routes must be registered BEFORE the static mount inside `create_app`.

---

## Scope decisions (read before executing — these are the judgment calls)

1. **`Stat` and `Heartbeat` wire shapes do not change.** `tests/cockpit/test_api.py`
   asserts both key sets as closed; the frontend treats them as closed. Everything new
   (settlement state, n_needed, rule text, sha) rides card-level fields, not Stat fields.
   Lamp shape coding is pure CSS on the existing `state` string.
2. **Experiment registry numbers are registration-time choices.** Seed values below:
   `target_ci_halfwidth_r = 0.15`, `mde_r = 0.10` for every experiment unless a doc
   states otherwise. These are defensible defaults, not derived truths — Oliver should
   review the seed JSON before merge. Legacy experiments are registered retroactively
   with `provenance` naming the original roster comment / doc and the sha of the commit
   that introduced the roster line (looked up via `git log -L`).
3. **Settlement rule (uniform):** a card is `settled-awaiting-decision` when the delta
   CI half-width ≤ `target_ci_halfwidth_r` AND `n_accrued ≥ MIN_LEADERBOARD_N (20)` AND
   the bound is trustworthy (`not thin_clusters`). `futile-awaiting-decision` when the
   delta UPPER bound < `mde_r` AND `n_accrued ≥ 20` AND the same trust bar holds (not
   thin, and the interval is a real bootstrap product, not an empty-side collapse) —
   NO verdict, settled or futile, is ever issued from an untrusted bound; an untrusted
   book stays `accruing` no matter what its numbers say. (Design doc: futility tests
   "upper bound < MDE", never "lower bound < 0".) Arms and variants use DIFFERENT delta
   machinery (paired vs clustered two-sample) — see Task 3; a card that applied one
   primitive to both kinds would be statistically dishonest by the repo's own rules.
4. **Cost stamping is a date-cutoff derivation, not a re-pricer and not a new column.**
   `realized_r` is baked net-at-exit; rows exited before the 0.05 default shipped are
   gross, and nothing on the row records which. A cohort's Stat gets
   `cost_level="0.05"` iff EVERY closed trade in it exited on/after `COST_STAMPED_FROM`;
   any pre-cutoff exit → `None` (the hollow "not measured" tick). Over time everything
   becomes stamped. The `@0.10` masthead button stays disabled with an honest tooltip —
   no data exists at that level (a re-replay job is out of scope; see non-goals).
5. **`corpus_id` stays `None` everywhere in Phase 2.** Every Phase-2 aggregate is
   forward-book-derived, and forward rows have no corpus (stamping one would be a
   category error). Adding `cost_level`/`corpus_id` to `Verdict` + threading `as_of`
   through `run_reflection` belongs to Phase 3 with the Playbooks screen.
6. **Facet toggle = `research` (default) vs `gold`.** Gold filters trades with
   `t.would_surface` truthy — mirroring `reflect.py`'s `forward_gold` slice exactly
   (`None` and `False` both excluded). The gold book is deliberately thin (stamping
   started ~2026-07); cards reading THIN on gold is expected, not a bug.
7. **Sparklines are inline SVG, not uPlot.** The design doc named uPlot, but a
   ~60-point polyline needs no dependency; zero new bundle bytes, tokens-only styling.
   Revisit if Phase 3 needs real charts (candles → lightweight-charts, as designed).
8. **DISARM stays disabled** (Phase 3, per the index.css comment). No six-actions work
   in Phase 2. The Needs-Your-Hand strip ships minimally: client-side, derived from
   already-fetched forward-books data (settled/futile cards) — no new endpoint.
9. **One branch, one squash PR:** `feat/cockpit-phase2`, titled
   `feat(cockpit): Phase 2 -- forward books, funnel, performance parity, SSE`. If review
   size becomes unwieldy, split at the Task 8/9 boundary (backend PR + frontend PR).

---

### Task 1: Experiment registry (`pipeline/registry.py` + `edge/experiments.json`)

The design's "pre-registered stopping rule verbatim with its git commit hash" has no
source today — stopping rules live in code comments and doc prose. Create the registry.

**Files:**
- Create: `src/swing_screener/pipeline/registry.py`
- Create: `edge/experiments.json`
- Test: `tests/pipeline/test_registry.py`

**Step 1: Write the failing tests**

```python
"""The experiment registry: the machine-readable pre-registration record.

Every forward experiment (exit arm or screen variant) has exactly one registry entry
carrying its hypothesis, stopping rule verbatim, MDE, and the git sha it was registered
under -- the solo pre-registration-theater mitigation. The registry and the code rosters
(pipeline/arms.py, pipeline/variants.py) must never drift: a roster entry without a
registry row (or vice versa) is a test failure, not a rendering surprise.
"""

from pathlib import Path

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.arms import BASELINE, build_arms
from swing_screener.pipeline.registry import Experiment, load_experiments
from swing_screener.pipeline.variants import DEFAULT_VARIANT, build_screen_variants

REPO_EDGE = Path(__file__).resolve().parents[2] / "edge"


def test_experiment_round_trip(tmp_path: Path) -> None:
    exp = Experiment(
        name="rev_highvol", kind="variant", play_type="reversal", control="default",
        hypothesis="h", stopping_rule="rule text", mde_r=0.10,
        target_ci_halfwidth_r=0.15, registered_at="2026-07-10",
        registered_sha="abc123", doc_ref="docs/x.md", provenance="roster comment",
        status="active",
    )
    (tmp_path / "experiments.json").write_text(
        "[" + exp.as_json() + "]", encoding="utf-8"
    )
    loaded = load_experiments(tmp_path)
    assert loaded == [exp]


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_experiments(tmp_path) == []


def test_registry_matches_code_rosters() -> None:
    """Bidirectional lockstep with the real committed registry."""
    base = StrategyConfig()
    roster = (set(build_arms(base)) - {BASELINE}) | (
        set(build_screen_variants(base)) - {DEFAULT_VARIANT}
    )
    registered = {e.name for e in load_experiments(REPO_EDGE)}
    assert registered == roster
```

**Step 2:** Run: `.\.venv\Scripts\python -m pytest tests/pipeline/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: swing_screener.pipeline.registry`.

**Step 3: Implement `registry.py`** — mirror `pipeline/proposed.py`'s frozen-dataclass +
JSON round-trip pattern:

```python
"""Machine-readable experiment pre-registration (edge/experiments.json).

An Experiment row is the settlement card's source of truth: the stopping rule renders
VERBATIM with the sha it was registered under. Fields are never inferred at render time
-- a rule invented when the card is drawn is pre-registration theater, the exact failure
this file exists to prevent. Legacy experiments carry provenance describing where the
original wording lived (roster comment / study doc) and the sha that introduced the
roster line.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class Experiment:
    name: str                    # must equal the PaperTrade.arm or .variant string
    kind: str                    # 'arm' | 'variant'
    play_type: str               # 'continuation' | 'reversal' | 'all' (delta scope)
    control: str                 # 'baseline' (arms) | 'default' (variants)
    hypothesis: str
    stopping_rule: str           # rendered verbatim on the card
    mde_r: float                 # futility: corrected upper bound < mde_r
    target_ci_halfwidth_r: float # settlement: delta CI half-width <= this
    registered_at: str           # ISO date
    registered_sha: str          # git sha stamped at registration
    doc_ref: str
    provenance: str
    status: str                  # 'active' | 'retired'
    decided_at: str | None = None
    decision: str | None = None

    def as_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def load_experiments(edge_dir: Path) -> list[Experiment]:
    path = edge_dir / "experiments.json"
    if not path.exists():
        return []
    return [Experiment(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
```

**Step 4: Seed `edge/experiments.json`** — nine entries (arms: `no_flip`,
`partial33_cond`, `partial33_chand`, `be_1r`; variants: `extguard_tight`,
`cont_volband`, `rev_highvol`, `rev_confirm1`, `rev_retrace786`). For each:
- `hypothesis` / `stopping_rule`: adapt the roster comments in `pipeline/variants.py` /
  `pipeline/arms.py` and the promotion-bar wording in
  `docs/plans/2026-06-25-continuation-edge-tournament-design.md` ("a positive,
  cost-robust subset: 95%-low > 0 at ~0.05 ATR"). Example for `rev_highvol`:
  `"Settle when the clustered 95% delta CI half-width vs default on the reversal book
  reaches 0.15R (n>=20, clusters>=8). Retire if the clustered upper bound < +0.10R.
  Promotion consideration only if the clustered lower bound > 0 net @0.05 ATR — the
  shadow book remains the promotion arbiter."`
- `registered_sha`: for each roster line run
  `git log -L "/'<name>'/",+1:src/swing_screener/pipeline/variants.py --format=%H -s | tail -1`
  (first commit that introduced the line; use the arms.py path for arms). Record the
  full 40-char sha.
- `play_type` scope: `rev_*` → `reversal`; `extguard_tight`, `cont_volband` →
  `continuation`; arms → `all` (they duplicate every fill).
- `mde_r=0.10`, `target_ci_halfwidth_r=0.15` unless a doc states otherwise.

**Step 5:** Re-run the tests — all three PASS (the lockstep test now runs against the
real seed file). **Step 6:** `ruff check src tests && mypy` — clean.

**Step 7:** Commit: `feat(registry): machine-readable experiment pre-registration --
stopping rules with shas`

---

### Task 2: Analytics primitives (trailing expectancy, paired upper bound, cost cutoff)

**Files:**
- Modify: `src/swing_screener/analytics/performance.py`
- Test: `tests/analytics/test_performance.py` (extend)

**Step 1: Failing tests** (in the existing file's style — build `PaperTrade` rows with
the `_trade`-style helpers already present there):

- `test_trailing_expectancy_windows_by_exit_date` — 30 closed trades with staggered
  `exit_date`s; `trailing_expectancy(trades, window=10)` returns `list[tuple[date,
  float]]` ordered ascending, one point per closing trade collapsed to the last value
  per date, each value = mean `realized_r` of the trailing `window` closes; fewer than
  `window` closes so far → mean of what exists. Empty input → `[]`.
- `test_paired_arm_delta_carries_upper_bound_and_stderr` — extend the existing
  paired-delta fixture; assert new fields `delta_ci_high` (== `mean_delta +
  1.96*stderr`, IID) and `stderr` exist and are floats. (The clustered machinery only
  hardens the LOWER bound — the upper stays IID; the settlement card labels it.)
- `test_cost_stamped_from_gates_cost_level` — see helper below; a list whose every
  closed trade has `exit_date >= COST_STAMPED_FROM` → `"0.05"`; one earlier exit →
  `None`; empty → `None`.

**Step 2:** Run — FAIL. **Step 3: Implement** (all pure, no I/O — this module is
contractually pure):

- `trailing_expectancy(trades: Iterable[PaperTrade], *, window: int = 20) ->
  list[tuple[date, float]]` — copy `equity_curve`'s filtering/ordering rules
  (`_is_closed_filled` + `exit_date is not None`, sort by `exit_date`); bootstrap-free
  point estimates only (a card grid calling the 1000-draw bootstrap per point would be
  ruinous).
- Extend `PairedArmDelta` with `delta_ci_high: float` and `stderr: float` (the function
  already computes stderr internally and discards it — return it). Fix any direct
  constructions in tests.
- `COST_STAMPED_FROM: date` constant next to `TIER_STAMPED_FROM` — the date the
  `fill_slippage_atr=0.05` default shipped to prod. Find it:
  `git log --format="%h %ad %s" --date=short -S "fill_slippage_atr" -- src/swing_screener/config.py`
  and use the merge date of the commit that set the default to 0.05 (2026-07 audit era).
- `cost_level_for(trades: Iterable[PaperTrade]) -> str | None` — `"0.05"` iff there is
  at least one closed-filled trade and every closed-filled trade's `exit_date` is
  non-None and on/after `COST_STAMPED_FROM`; a missing `exit_date` OR any pre-cutoff
  exit → `None`. (The exit_date-None rule matters twice: it is the honest answer for
  rows that can't prove their cost level, and it keeps the existing Phase-1 fixture
  assertions green — `tests/cockpit/test_api.py`'s `_trade` helper builds closed trades
  with no `exit_date`, and lines 119–121 assert `cost_level is None`.) Docstring must
  state the honesty rationale (mixed gross/net book; slippage applies at exit;
  momentum_flip/time_stop exits are never haircut so "net @0.05" means
  level-exits-only — don't overclaim in tooltips).

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(analytics): trailing expectancy, paired-delta upper bound, cost-level cutoff`

---

### Task 3: Settlement engine (`cockpit/settlement.py`)

**Files:**
- Create: `src/swing_screener/cockpit/settlement.py`
- Test: `tests/cockpit/test_settlement.py`

**Step 1: Failing tests** (seed an in-memory-style trade list; no DB needed — the
engine takes trade lists, the API layer loads them):

```python
"""Settlement: the Forward Books state machine.

Arms are same-sample (judged by paired_arm_delta -- pairing on the fill identity);
variants are separate books (judged by the clustered two-sample delta, called twice for
both bounds). Applying one primitive to both kinds is statistically dishonest by the
repo's own rules; the card also refuses to settle on an untrustworthy (thin) bound.
"""
```

- `test_variant_card_uses_two_sample_bounds` — two synthetic books (variant vs default,
  same play_type, ≥8 tickers each, n≥20); card.delta.ci_low/ci_high come from the
  clustered two-sample bootstrap (assert against direct calls with the same seed);
  `card.upper_bound_type == "clustered"`.
- `test_arm_card_uses_paired_delta` — arm book built by duplicating fills (same
  ticker/timeframe/play_type/variant/trigger_ts, different arm); `card.n_accrued ==
  n_pairs`; `card.upper_bound_type == "iid"`.
- `test_states` — accruing (wide CI), settled-awaiting-decision (tight CI, n≥20, not
  thin), futile-awaiting-decision (upper < mde), retired (registry status). Thin
  clusters → NEVER settled even with a tight interval.
- `test_n_needed_scales_inverse_square` — `n_needed ≈ max(ceil(n_accrued *
  (halfwidth_now / target)**2), MIN_LEADERBOARD_N)` (n_needed is the wider of the two
  binding constraints — a tight-but-small book must not read "settle today");
  `None` when `n_accrued < 5` or halfwidth is 0.
- `test_gold_facet_filters_would_surface_truthy` — `None` and `False` rows excluded.

**Step 2:** FAIL. **Step 3: Implement:**

```python
@dataclass(frozen=True, kw_only=True)
class SettlementCard:
    name: str
    kind: str                    # 'arm' | 'variant'
    play_type: str
    state: str  # 'accruing' | 'settled-awaiting-decision' | 'futile-awaiting-decision' | 'retired'
    n_accrued: int
    n_needed: int | None
    eta: str | None              # ISO date, from trailing-30d accrual rate
    stopping_rule: str
    registered_sha: str
    registered_at: str
    mde_r: float
    book: Stat
    control: Stat
    delta: Stat
    upper_bound_type: str        # 'clustered' (variants) | 'iid' (arms) — label, don't hide
    spark: list[tuple[str, float]]   # (ISO exit_date, trailing expectancy), last ~60 pts
    decision: str | None
```

`build_cards(experiments, *, book_loader, now)` where `book_loader(play_type, arm,
variant)` is injected (the API layer binds it to `load_closed_paper_trades(session,
...)`). Per experiment:
- **variant**: book = loader(play_type=scope, arm=BASELINE, variant=name); control =
  same with variant=DEFAULT_VARIANT. Delta value = `summarize(book).expectancy_r -
  summarize(control).expectancy_r`; bounds via `clustered_two_sample_delta_low(...,
  lower_pct=2.5)` and `(..., lower_pct=97.5)`. Input shape (verified): each side is a
  `Mapping[str, Sequence[float]]` — ticker → that ticker's `realized_r` values, NOT a
  flat trade list; build the groupings the same way `propose.py` does.
- **arm**: trades = loader(play_type=None if scope=='all' else scope, arm=None,
  variant=DEFAULT_VARIANT) then `paired_arm_delta(trades, arm=name)`. n_accrued =
  n_pairs (smaller than either arm's n_closed — the card's sub-line explains why:
  "pairs where both legs closed").
- Delta Stat: `value=mean_delta, n=n_accrued, n_clusters, ci_low, ci_high,
  cost_level=cost_level_for([*book, *control]), corpus_id=None, facet=facet, unit="R",
  thin_clusters` — the cost stamp covers BOTH sides (a delta claiming net@0.05 with a
  gross control side is an overclaim). Book/control Stats via `stat_from_summary`.
- Facet `gold`: filter every loaded list to `t.would_surface` truthy BEFORE any math.
- States per scope decision 3; retired experiments still produce cards (state
  'retired', decision text shown) — falsified history stays legible.
- `eta`: closes in the trailing 30 days of the book's exit_dates / 30 per day → days to
  n_needed → ISO date from `now`. Rate 0 or n_needed None → None.
- Guard JSON: any `inf`/`nan` from empty summaries must not reach the wire (empty book
  → still emit the card, state 'accruing', n_accrued 0, delta Stat n=0 — StatChip's
  n<5 badge handles display).

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): settlement engine -- honest per-kind deltas, states, n-needed math`

---

### Task 4: Persist the reversal funnel (model + migration + write site)

**Files:**
- Modify: `src/swing_screener/db/models.py` (new `ReversalFunnel` model)
- Create: `alembic/versions/<newrev>_reversal_funnels.py`
- Modify: `src/swing_screener/db/repo.py` (save/load pair)
- Modify: `src/swing_screener/notify/run.py` (write after the tuple is built)
- Test: `tests/db/test_repo.py` (extend), `tests/notify/test_run.py` (extend),
  `tests/test_alembic_offline.py` (extend EXPECTED_TABLES)

**Step 1: Failing tests**

- `test_save_reversal_funnel_idempotent_per_run_date` — save twice for the same
  run_date with different counts; one row remains, carrying the second write's values
  (forced digest resends re-execute the block).
- `test_latest_reversal_funnel_returns_newest` — two run_dates; latest wins; empty
  table → None.
- In the notify tests: drive a daily digest (existing fixtures) and assert a funnel row
  exists for the run_date with all five counts + overflow; drive a WEEKLY digest and
  assert no row (the funnel block is daily-only).

**Step 2:** FAIL. **Step 3: Implement:**

Model (bounded strings — Azure SQL cannot index NVARCHAR(max); the schema test sweeps
every table automatically; ints/dates need no bounding):

```python
class ReversalFunnel(Base):
    """One row per daily digest: the reversal funnel snapshot.

    fresh/actionable/surfaced are digest-time state (cooldown, live quotes, sector cap)
    and are UNRECOVERABLE later -- this table is the only record. detected/confirmed are
    recomputable from signals until a re-screen rewrites the run_date.
    """
    __tablename__ = "reversal_funnels"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date)          # unique via uq index
    detected: Mapped[int]
    confirmed: Mapped[int]
    fresh: Mapped[int]                                    # strength bar + cooldown + pool cap (20)
    actionable: Mapped[int]                               # after already-ran drop
    surfaced: Mapped[int]                                 # after sector cap + top-5
    overflow_tickers: Mapped[str] = mapped_column(String(512), default="")
    pool_n: Mapped[int] = mapped_column(default=20)
    confirmed_only: Mapped[bool] = mapped_column(default=True)
    premium_only: Mapped[bool] = mapped_column(default=False)
    already_ran_checked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime | None] = mapped_column(default=None)
```

Migration: hand-written, modeled on `b8d4f1a6c3e2_market_reports.py` (create_table) +
`f4c1e8a2b6d9` (unique index `uq_reversal_funnels_run_date`). `down_revision` = the
CURRENT head — check with `.\.venv\Scripts\python -m alembic heads` (verified
`c8f3a6d1e9b4` on 2026-07-10, nothing chains off it; another PR may advance it before
this executes). NOT NULL ints get
`server_default="0"`; booleans `server_default=sa.false()` / `sa.true()` to match model
defaults. Downgrade drops index then table. (Alembic is in the `[azure]` extra — keep
imports lazy; the migration file itself is ruff-checked in CI.)

Repo pair in `db/repo.py`: `save_reversal_funnel(session, *, run_date, detected,
confirmed, fresh, actionable, surfaced, overflow_tickers, pool_n, confirmed_only,
premium_only, already_ran_checked)` — delete-then-insert on run_date (mirror the
EmailLog dedup posture; boolean filters must render `== True/False`, never `.is_()`);
`latest_reversal_funnel(session) -> ReversalFunnel | None`.

Write site in `notify/run.py`, inside the `kind == "daily"` block immediately after
`reversal_overflow` is built (~line 609) and BEFORE `_build_picks` — the only scope
where all six values exist:

```python
repo.save_reversal_funnel(
    session, run_date=run_date, detected=detected, confirmed=confirmed_n,
    fresh=n_fresh, actionable=n_actionable, surfaced=len(reversal_sigs),
    overflow_tickers=",".join(reversal_overflow)[:512],
    pool_n=sel.REVERSAL_POOL_N,
    confirmed_only=scfg.reversal_surface_confirmed_only,
    premium_only=scfg.reversal_surface_premium_only,
    already_ran_checked=latest_closes_fn is not None,
)
```

Scope note (verified 2026-07-10): all of `detected`, `confirmed_n`, `n_fresh`,
`n_actionable`, `reversal_sigs`, `reversal_overflow`, `scfg`, and `latest_closes_fn`
are in scope at that point, and `repo` is already imported (`from swing_screener.db
import repo`, line ~33). A LOCAL variable named `surfaced` (a set of tickers) already
exists in that block — the `surfaced=` keyword argument doesn't collide, but don't
introduce another local with that name.

Do NOT widen the email tuple — `body.py` destructures `detected, confirmed, *stages`
and a 5-tuple raises ValueError; the persisted record is independent of the email
rendering. Do not touch the `SCREEN_RUN_COMPLETE` log line (greped by an Azure alert).

Extend `EXPECTED_TABLES` in `tests/test_alembic_offline.py` with `reversal_funnels` and
add its unique-index assertion.

**Step 4:** PASS (`pytest tests/db tests/notify tests/test_alembic_offline.py -q` —
alembic test skips without the azure extra; run it locally with the extra installed if
available). **Step 5:** ruff (includes alembic/) + mypy. **Step 6:** Commit:
`feat(funnel): persist the reversal funnel per daily digest -- the stages are
unrecoverable otherwise`

**Note for docs (Task 13):** the table is NEW, so local sqlite gets it free via
`create_all` on any `get_engine` call; Azure gets it on the pipeline's next
self-migrating mssql run. No manual step for either.

---

### Task 5: API — facet/cost params, `/api/forward-books`, `/api/funnel`

**Files:**
- Modify: `src/swing_screener/cockpit/api.py`
- Test: `tests/cockpit/test_api.py` (extend)

**Step 1: Failing tests** (existing tmp_path file-sqlite + TestClient pattern):

- `test_cohorts_accepts_facet_param` — seed research trades, some `would_surface=True`;
  `GET /api/stats/cohorts?facet=gold` aggregates only truthy rows; default (`research`)
  unchanged; `facet` echoed in each Stat; unknown facet → 422. NOTE: the existing
  `test_cohort_stats_are_stat_objects` assertions (test_api.py:119–121 — `cost_level is
  None`, `corpus_id is None`, `facet == "research"`) must STAY GREEN untouched: its
  `_trade` fixture sets no `exit_date`, so `cost_level_for` returns None by the Task 2
  rule. If a new fixture sets post-cutoff exit_dates, assert `"0.05"` there instead —
  never weaken the original.
- `test_cohort_stats_carry_cost_level_after_cutoff` — trades all exiting post-cutoff →
  `stat.cost_level == "0.05"`; add one pre-cutoff exit → `null`.
- `test_forward_books_shape` — seed a variant book + default control + registry file in
  `tmp_path` edge_dir; `GET /api/forward-books` → `{"cards": [...]}` where every card
  carries the SettlementCard fields, every numeric is a 10-key Stat dict, `spark` is a
  list of `[iso_date, float]` pairs, and cards are ordered: awaiting-decision first,
  then accruing, then retired.
- `test_forward_books_empty_registry` — no experiments.json → `{"cards": []}` (200, not
  an error).
- `test_funnel_endpoint` — no rows → `{"funnel": null}`; after a save →
  `{"funnel": {run_date, detected, confirmed, fresh, actionable, surfaced,
  overflow: [tickers], pool_n, confirmed_only, premium_only, already_ran_checked}}`.

**Step 2:** FAIL. **Step 3: Implement** in `create_app` (routes BEFORE the static
mount; sessions via the existing `Depends(_session)`; the SQLAlchemyError→503 handler
covers stale-schema DBs automatically):

- Shared helper `_facet_filter(trades, facet)`; validate `facet` with a
  `Literal["research", "gold"]` query param (FastAPI 422s the rest).
- `/api/stats/cohorts`: thread `facet` + replace the hardcoded `cost_level=None` in
  `_cohort` with `cost_level_for(subset)` per cohort subset.
- `/api/forward-books`: `load_experiments(resolve_edge_dir(edge_dir))`, bind
  `book_loader` to `load_closed_paper_trades(session, ...)`, `build_cards(...)`,
  serialize dataclasses by hand like `_beat_dict` (deliberately no asdict on the wire).
- `/api/funnel`: `latest_reversal_funnel(session)`; split `overflow_tickers` on commas,
  drop empties.

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): forward-books + funnel endpoints, facet param, cost stamps`

---

### Task 6: API — `/api/stats/performance` + `/api/gate` (Streamlit parity)

**Files:**
- Modify: `src/swing_screener/cockpit/api.py`
- Test: `tests/cockpit/test_api.py` (extend)

**Step 1: Failing tests** — port the SCENARIOS from `tests/dashboard/test_performance.py`
(multi-variant leaderboard, arm A/B, score calibration gating, regime breakdown, thin
flagging) against the new endpoint; they are the parity contract that lets Task 11
delete that file. Plus:

- `test_performance_window_cuts_after_arm_filter` — the 90d window applies to the
  arm==BASELINE subset on `opened_date` (rows with `opened_date=None` drop from
  windowed views, stay in "all").
- `test_performance_scopes_arms_to_default_variant` — the arm A/B and every downstream
  KPI/breakdown slice to `variant == DEFAULT_VARIANT` (the A/B is only honest within
  one screen variant).
- `test_profit_factor_inf_is_null` — all-winner book → `profit_factor: null` on the
  wire (JSON has no Infinity).
- `test_gate_endpoint` — `{"ready": bool, "countdown": str (gate_countdown verbatim),
  "execution_mode": str, "analyst_spend_today_usd": float}`.

**Step 2:** FAIL. **Step 3: Implement:**

`GET /api/stats/performance?play_type=all|continuation|reversal&window=all|90|180|365&facet=research|gold`
returning:

```
{
  "kpis": {"expectancy": Stat, "win_rate": f, "fill_rate": f,
            "profit_factor": f|null, "n_closed": i},
  "leaderboard": [{"variant": s, "stat": Stat, "win_rate": f, "fill_rate": f,
                    "n_total": i, "flag": "iid"|"thin"|"ok"}],   # leaderboard_order
  "arms": [{"arm": s, "stat": Stat, "n_pairs": i, "delta": Stat|null}],
  "breakdowns": {"timeframe": [...], "rank": [...], "score": [...],
                  "market_trend": [...], "market_vol": [...]},
                  # each entry {"key": s, "stat": Stat, "win_rate": f, "n_closed": i}
  "equity_curve": [["2026-07-01", 3.4], ...]
}
```

Replicate the Streamlit page's load-bearing order EXACTLY: (1) play_type filter on all
research trades; (2) leaderboard on the `arm == BASELINE` subset with the window cut
applied after the arm filter; (3) then scope to `variant == DEFAULT_VARIANT` for arms,
KPIs, breakdowns, equity curve. Score breakdown goes through `score_stamped()` (forward
book only); rank buckets use edges `[5, 10]`; regime rows skip `None`. All aggregate
Stats get `cost_level=cost_level_for(subset)`, `corpus_id=None`, the request's facet.
Win/fill rates and n counts ride as plain row fields (the Stat guards the edge claim —
same posture as the existing cohort rows' `key`/`strength`).

`GET /api/gate`: `autonomy_gate(session, edge_dir=resolve_edge_dir(edge_dir))` →
`ready` + `gate_countdown(report)` verbatim (do NOT reimplement the arithmetic — the
line format is pinned by tests/pipeline/test_autonomy_countdown.py); `execution_mode`
from `load_settings().execution_mode`; spend = sum of `est_cost_usd or 0.0` over
AnalystCall where `created_date == date.today()`.

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): performance + autonomy-gate endpoints -- Streamlit parity as Stats`

---

### Task 7: SSE wake channel (`/api/events`)

**Files:**
- Modify: `pyproject.toml` (`[cockpit]` extra += `sse-starlette>=2.1`)
- Modify: `src/swing_screener/cockpit/api.py`
- Test: `tests/cockpit/test_api.py` (extend)

**Step 1: Failing tests** — test the change-token function directly (the SSE loop stays
thin; an infinite stream is miserable to assert against):

- `test_change_token_moves_on_new_trade` — token before ≠ token after inserting a
  PaperTrade; stable when nothing changes.
- `test_change_token_survives_db_down` — bad engine → token is the sentinel
  `{"db": "down"}`, no exception (recovery then reads as a change).
- `test_events_route_exists` — GET `/api/events` responds with
  `text/event-stream` content-type (read the first bytes, then close — do not consume
  the infinite stream).

**Step 2:** FAIL. **Step 3: Implement:**

`_change_token(engine_factory) -> dict[str, str]`: short-lived Session per call; cheap
max-watermarks only — `max(Signal.run_date)`, `max(PaperTrade.id)`,
`max(EmailLog.sent_at)`, `max(MarketReport.run_date)`, `latest_reversal_funnel`
run_date, newest `*.verdicts.json` mtime via the existing heartbeat helper. Async
endpoint:

```python
@app.get("/api/events")
async def events() -> EventSourceResponse:
    async def stream() -> AsyncIterator[dict[str, str]]:
        last: dict[str, str] | None = None
        while True:
            token = await anyio.to_thread.run_sync(_safe_change_token)
            if token != last:
                last = token
                yield {"event": "change", "data": json.dumps(token)}
            await anyio.sleep(15)
    return EventSourceResponse(stream(), ping=15)
```

Constraints: the endpoint must be `async` (a sync generator would pin a threadpool
worker); DB reads hop through `anyio.to_thread` (starlette ships anyio); never hold a
Session across sleeps; add NO logging handlers (pythonw None-stream trap — uvicorn runs
with `log_config=None` for a reason). Data changes come from EXTERNAL processes
(scheduled jobs, git pulls) so server-side polling is the only correct driver. The
route is a GET under `/api` (covered by the dev vite proxy; no X-Cockpit header — it
mutates nothing).

**Step 4:** PASS. **Step 5:** ruff + mypy; confirm CI installs it (`[dev,cockpit]`) and
the Docker image does NOT (it installs `[azure]` — correct, the cockpit never ships in
the container). **Step 6:** Commit:
`feat(cockpit): SSE wake channel -- server-polled change tokens, 60s poll stays the floor`

---

### Task 8: Heartbeats — business-day calendar + GH pollers

**Files:**
- Modify: `src/swing_screener/cockpit/heartbeats.py`
- Create: `src/swing_screener/cockpit/gh.py`
- Test: `tests/cockpit/test_heartbeats.py` (extend), `tests/cockpit/test_gh.py`

**Step 1: Failing tests**

- `test_business_period_spans_weekend` — Friday-run beat is `up` on Sunday and `late`
  only after Monday's expected run + grace (the flat-72h window hid a missed Monday
  until Tuesday; that's the docstring's Phase-2 TODO).
- `test_business_period_spans_holiday` — a run before a listed market holiday doesn't
  read late until the day after the holiday + grace.
- `test_gh_beats_read_unknown_without_config` — no token/repo env → the three GH rows
  stay exactly as today: `state="unknown"`, `period_s == grace_s == 0`, detail
  "poller not configured" (the closed 6-key wire shape and the ROSTER test must keep
  passing untouched).
- `test_gh_poller_maps_runs_to_beats` — inject a fake poller returning
  `(completed_at, conclusion)`; optimizer/reflection beats become time-based 7d/3h;
  a CI beat with `conclusion="failure"` reads `down` regardless of recency.

**Step 2:** FAIL. **Step 3: Implement:**

- Holiday source (resolved 2026-07-10): no trading-calendar helper exists anywhere in
  the repo — `ops/eastern_gate.py` deliberately ignores holidays for the monthly digest
  ("an exchange-holiday calendar is not worth the dependency here"). Consistent with
  that stance, add a small STATIC `_MARKET_HOLIDAYS: frozenset[date]` (2026–2027 NYSE
  dates, ~20 entries) in heartbeats.py — no dependency — with a docstring noting the
  annual refresh obligation and citing the eastern-gate precedent. Without it, every
  market holiday would light a false LATE lamp, and the dark-cockpit doctrine says
  false lights erode the only thing the panel sells: trust in silence.
- `_next_expected(last: datetime) -> datetime` — next business day (skip Sat/Sun +
  holidays) at the same anchor time; the two weekday beats (evening screen, daily
  digest) get `period = _next_expected(last) - last` instead of the flat 72h.
  `beat_state` itself stays pure and untouched (None→unknown, future→up semantics are
  boundary-pinned by existing tests).
- `collect_heartbeats(session, *, now, edge_dir=None, gh_latest=None)` — new optional
  DI param `gh_latest: Callable[[str], tuple[datetime, str] | None] | None`. When None
  (default): the three hard-coded UNKNOWN rows exactly as today. When provided:
  `'GH · optimizer'` ← `optimize.yml` (7d/3h), `'GH · reflection'` ← `reflect.yml`
  (7d/3h), `'GH · CI'` ← `ci.yml` (7d/3h, but conclusion `failure` forces `down` with
  the conclusion in `detail`).
- `cockpit/gh.py`: `latest_workflow_run(repo: str, workflow: str, token: str) ->
  tuple[datetime, str] | None` via stdlib `urllib.request` against
  `https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page=1&status=completed`;
  5-minute in-process TTL cache; ANY exception → None (an unreachable poller must
  degrade to unknown, never crash /api/heartbeats). `create_app` wires it when both
  `SWING_GH_TOKEN` and `SWING_GH_REPO` are set; otherwise passes None.

**Step 4:** PASS. **Step 5:** ruff + mypy. **Step 6:** Commit:
`feat(cockpit): business-day heartbeat calendar + optional GH workflow pollers`

---

### Task 9: Frontend — types, fetchers, SSE wake

No pytest here — the gate is `tsc -b` strictness + `npm run lint` + eyes on the app.

**Files:**
- Modify: `cockpit-ui/src/lib/api.ts`

**Steps:**
1. Mirror the new wire types exactly (`import type`; union string literals, never
   enums — `erasableSyntaxOnly` forbids them): `SettlementCard`, `ForwardBooks`,
   `Funnel`, `Performance` (+ row types), `Gate`. `Facet = 'research' | 'gold'`,
   `Window = 'all' | '90' | '180' | '365'`.
2. Parameterized fetchers: `getCohorts(facet)`, `getForwardBooks(facet)`,
   `getPerformance(playType, window, facet)`, `getGate()`, `getFunnel()`.
3. Extend `usePolling(fetcher, ms, wake?: number)` — a changed `wake` value triggers an
   immediate tick through the SAME seq-guard path (the two existing guarantees hold:
   inline lambdas never restart the interval; out-of-order responses never move state
   backwards; effect deps become `[ms, wake]`).
4. `useEventWake(): number` — `EventSource('/api/events')`; every `change` event
   increments a counter; EventSource auto-reconnects on error; the 60s interval remains
   the floor so SSE death degrades gracefully. The "data as of" clock keeps deriving
   from `Polled.lastFetched` (SSE only wakes fetches — the clock stays truthful).
5. `npm run build` must pass. Commit (source only — static/ regenerates in Task 10's
   final build): `feat(cockpit-ui): wire types, faceted fetchers, SSE wake hook`

---

### Task 10: Frontend — components + Mission Control layout

**Files:**
- Create: `cockpit-ui/src/components/Sparkline.tsx`, `SettlementCard.tsx`,
  `FunnelBar.tsx`, `FacetToggle.tsx`, `NeedsHandStrip.tsx`, `PerformancePanel.tsx`
- Modify: `cockpit-ui/src/components/Masthead.tsx`, `HeartbeatRail.tsx` (shapes via
  CSS only), `App.tsx`, `index.css`
- Regenerate: `src/swing_screener/cockpit/static/` (same commit)

**Steps (tokens.css only — no ad-hoc colors; every numeric renders through StatChip):**

1. `Sparkline` — inline SVG polyline (~150×28), `--blue` stroke, `--dim` zero-line,
   last-point dot; input `[iso_date, value][]`.
2. `SettlementCard` — header (name + kind + play_type), state chip (`accruing` dim /
   `settled-awaiting-decision` + `futile-awaiting-decision` amber glow, matching the
   hb-late lamp treatment / `retired` struck-through dim); SettlementBar = horizontal
   progress `n_accrued / n_needed` with dashed "peek wings" outside the settled zone
   while accruing (Statsig-style); delta StatChip (label "Δ vs control") + book/control
   StatChips; `upper_bound_type: 'iid'` renders a dotted-top marker on the upper wing +
   tooltip "upper bound: IID (unhardened)"; stopping rule verbatim in `--mono` with
   `registered_at` + short sha; ETA line ("~n_needed at current accrual → 2026-09-14",
   or "accrual stalled"); Sparkline underneath. n<5 books: StatChip already collapses
   to the no-read badge; the card keeps `n accrued: N` in its own markup (StatChip
   hides n below 5 by design).
3. `FunnelBar` — five proportional segments (detected→confirmed→fresh→actionable→
   surfaced) with hatched drop segments between stages (CSS `repeating-linear-gradient`
   with `--faint`), stage labels + counts, overflow-ticker chips beneath ("lost the
   top-5/sector race"), and the honest footnotes: fresh = "strength bar + cooldown +
   pool cap (20)"; `already_ran_checked: false` → actionable renders with an "(unchecked)"
   tag. Empty state: "no funnel recorded yet — accrues from the next daily digest".
4. `FacetToggle` — masthead segmented GOLD / RESEARCH, default `research`; lifts state
   into App like the cost selector; research facet renders the design's hatch cue as a
   small hatched tag next to the panel captions (not per-chip noise); gold panels get a
   "GOLD · thin until stamped history accrues" caption while young.
5. Masthead wiring — cost segment: `net @0.05` enabled + selected (styling for the
   selected `.mh-seg` state is net-new in index.css); `@0.10` stays disabled,
   tooltip "not measured — replay-only level, no re-priced book exists". DISARM stays
   disabled (tooltip "Phase 3"). New autonomy-gate chip from `/api/gate`: `READY` or
   `NOT READY · <first line of countdown>`, full countdown in the title tooltip;
   execution-mode chip text alongside (both dim, never green).
6. `NeedsHandStrip` — thin strip under the masthead listing settled/futile cards by
   name ("rev_confirm1 · settled — decide"); empty state: "nothing needs your hand".
   Client-side only, derived from the forward-books payload.
7. `PerformancePanel` — leaderboard table (rows: variant, StatChip, win/fill, n, flag
   badge reusing THIN/dotted conventions), arm A/B block (delta StatChips), KPI row,
   breakdown tables behind a segmented control (timeframe / rank / score / regime),
   equity curve as a wide Sparkline. Window + play_type segmented controls mirror the
   Streamlit semantics. Conditional rendering mirrors the old page: leaderboard only
   with >1 variant, arms only with >1 arm, score section only when >1 band has closes.
8. `HeartbeatRail` shapes (CSS only, no TSX logic): `hb-up` filled circle, `hb-late`
   triangle (clip-path), `hb-down` square, `hb-unknown` dashed hollow circle — red/amber
   now always paired with a shape (colorblind rule closes the Phase-1 deferral).
9. `App.tsx` — grid becomes `320px 1fr 380px`: SYSTEMS rail | FORWARD BOOKS (dominant,
   with FunnelBar below) | COHORTS + PERFORMANCE. All polls take `wake` from
   `useEventWake()`; facet/cost/window state lifts here; PanelBody stale/error/wait
   semantics unchanged; db-down card unchanged.
10. `npm run lint && npm run build` → static/ regenerates (hashed filenames change —
    commit them; `.gitattributes` keeps it byte-stable).
11. Manual verify: `.\.venv\Scripts\python -m swing_screener.cockpit --browser` against
    `local.db` — cards render, funnel empty-state, gate chip, facet flip, lamp shapes.
12. Commit (source + static together):
    `feat(cockpit-ui): forward books, funnel, performance panel, facet+cost wiring, SSE`

---

### Task 11: Retire the Streamlit Performance + Health pages

Only after Tasks 5–6 are green — the parity tests ported from
`tests/dashboard/test_performance.py` are the contract that nothing goes dark.

**Files:**
- Modify: `src/swing_screener/dashboard/app.py`, `src/swing_screener/dashboard/ui.py`
- Delete: `tests/dashboard/test_performance.py`
- Modify: `tests/dashboard/test_app_smoke.py`, `tests/dashboard/test_ui.py`
- Modify: `docs/dashboard.md`

**Steps:**
1. `app.py`: delete `_render_performance` (~lines 484–703), `_render_health`
   (~1002–1051), `_freshness_row`, `_FRESH_BADGE`, and the two PAGES entries
   (`"Screener Performance"`, `"System Health"`). Prune now-dead imports ONLY:
   `autonomy_gate`/`gate_countdown`, `pipeline.health._freshness`, `timedelta`. KEEP
   `performance`, `BASELINE`, `DEFAULT_VARIANT`, `AnalystCall` (Overview + Calibration
   still use them — verify with a grep before pruning anything).
2. `ui.py`: delete `bar` (dead after the page goes); drop its assertion at
   `tests/dashboard/test_ui.py:40`. `ui.line` stays (Closed Trades uses it).
3. Tests: delete `tests/dashboard/test_performance.py`; in `test_app_smoke.py` delete
   the two System-Health tests and update the exhaustive page-label assertion (lines
   ~28–31) to the surviving 10 labels.
4. `docs/dashboard.md`: remove the Screener Performance bullet, note both pages moved
   to the cockpit, and fix the stale "ten pages" count while in there (pre-existing
   drift — the doc never listed Analyst Calibration / System Health).
5. Full `pytest -q` green; `streamlit run src/swing_screener/dashboard/app.py` still
   boots (app.py must stay importable — tests/test_storage_blob.py imports it).
6. Commit: `feat(dashboard): retire Screener Performance + System Health -- cockpit
   owns them now`

---

### Task 12: CI job for cockpit-ui

Closes the gap PR #103 explicitly deferred ("no CI job builds cockpit-ui/ yet").

**Files:**
- Modify: `.github/workflows/ci.yml`

**Steps:**
1. Add a second job:

```yaml
  cockpit-ui:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: cockpit-ui/package-lock.json
      - run: npm ci
        working-directory: cockpit-ui
      - run: npm run lint
        working-directory: cockpit-ui
      - run: npm run build
        working-directory: cockpit-ui
      - name: committed static matches source
        run: git diff --exit-code -- src/swing_screener/cockpit/static
```

2. The byte-compare works because `.gitattributes` disables EOL normalization on
   `static/**` and `npm ci` pins exact dep versions. If the diff step ever flakes on
   hash nondeterminism across OSes, downgrade that one step to build-succeeds and file
   the flake — do not delete the job.
3. Push and confirm both jobs green on the PR.
4. Commit: `ci(cockpit-ui): lint + build + committed-static drift check`

---

### Task 13: Docs + ship

**Files:**
- Modify: `docs/cockpit.md`
- Create/verify: this plan's cross-links

**Steps:**
1. `docs/cockpit.md` additions: the new endpoints table; SSE behavior (server-polled
   change tokens, 60s poll floor); `SWING_GH_TOKEN` + `SWING_GH_REPO` for the GH
   heartbeat pollers (optional — rows read UNKNOWN without them); the funnel table
   note (new table: local sqlite auto-creates, Azure migrates on the pipeline's next
   run); the experiment registry (`edge/experiments.json` — how to register/retire an
   experiment, and that settlement decisions are human PRs editing the registry +
   roster together).
2. Full gate: `pytest -q`, `ruff check src tests alembic`, `mypy`,
   `npm run lint && npm run build` + no static drift.
3. Branch `feat/cockpit-phase2`, PR titled
   `feat(cockpit): Phase 2 -- forward books, funnel, performance parity, SSE`; body
   with before/after, the scope-decision list from this plan's header, and a "Known
   posture" section (corpus_id deferred to Phase 3; @0.10 disabled-honest; gold facet
   thin; funnel history accrues from ship date). CI green; squash-merge.

---

## Explicit non-goals for Phase 2 (they are Phase 3+, per the design doc)

The six actions (incl. DISARM wiring — the button stays dead), Playbooks / Analyst /
Execution-Safety / Market-Weather screens, ProvenancePopover, TierChip / ConvictionChip
/ DeltaDiff components, Zone B risk strips / Zone D today's-picks / Zone E ticker,
keyboard nav + command palette, tray + notifications, PyInstaller packaging,
`Verdict.cost_level/corpus_id` + corpus threading through `run_reflection`, dual-cost
replay aggregates (the @0.10 level), md-vs-json drift lamp, deleting `dashboard/`
entirely (remaining pages retire in Phase 3), and `[tool.setuptools.package-data]`
for `static/` (dev installs are editable; don't silently change the packaging posture).
