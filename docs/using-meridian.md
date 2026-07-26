# Using Meridian — the operator's guide

Every other doc in this repo is organized by **component** (the screener, the cockpit, the
digests, the Azure deploy). This one is organized by **what you do**: the first two weeks,
the daily loop, the weekly loop, and the moments in between. It links into the component
docs for setup detail rather than repeating them.

The one thing to internalize before anything else: **Meridian runs itself; you decide.**
The system screens, forward-tests, measures, and proposes on its own schedule — but every
trade you take, every config change that ships, and every step toward automation is a
deliberate human act. Your job as the operator is a handful of short check-ins, not
babysitting.

> New to the vocabulary (Heiken Ashi, R-multiples, shadow book, conviction)? The cockpit
> has a built-in glossary: press `g` then `r` (the Reference screen) and open the
> **Glossary** panel — 112 terms, each linked to where it's used.

## The first two weeks (onboarding path)

1. **Install and run a first screen** — the [README Quick start](../README.md#quick-start)
   (venv + editable install + tests), then a small slice to sanity-check:

   ```powershell
   .\.venv\Scripts\python scripts\run_local.py --max-tickers 50
   ```

   Full flags, what a run actually does, and how to inspect results:
   [running-locally.md](running-locally.md).

2. **Launch the cockpit** and learn the three screens that matter first
   ([cockpit.md](cockpit.md) has the quickstart + Start-menu shortcut):

   ```powershell
   .\.venv\Scripts\python -m pip install -e ".[dev,cockpit]"
   .\.venv\Scripts\python -m swing_screener.cockpit --browser
   ```

   Press `1` for **Mission Control** (the home dashboard), `2` for **Candidates**
   (today's picks), `3` for **Positions & Ledger** (your trades). The other eleven
   screens can wait.

3. **Dry-run for 1–2 weeks.** Run the full screen after each close (or let the Azure
   jobs do it — step 5). Take no trades yet. The point is to let the **shadow book**
   accumulate real forward-test results — the evidence layer everything else stands on —
   and to let you watch how picks behave without money on the line.

4. **Turn on the email digests** so the morning summary comes to you instead of you going
   to it — [email-digests.md](email-digests.md) (local Gmail setup is four env vars; the
   optional Opus deep-analysis path is documented there too, off by default).

5. **(Optional) Deploy to Azure** so the whole cadence runs unattended —
   [azure-deploy.md](azure-deploy.md) is the one-time provisioning runbook. Local-only is
   a fully supported posture; you just run the pipeline and digests yourself.

6. **Start taking picks manually.** When a candidate convinces you, place the order
   yourself at your broker and log it in the cockpit (the loop below). Execution mode
   stays `off`; you are the adapter. Paper and (much later, gated) live Alpaca execution
   are described in the [README's execution section](../README.md#execution--money-safety)
   — they are the *last* steps, not the next ones.

## A day with Meridian

All times ET; the cadence is the [ten scheduled jobs](../README.md#what-runs-automatically-the-daily--weekly-cadence)
when deployed, or you running the equivalent commands locally.

### Morning (~8am): read the digest

The **daily digest** lands in your inbox: the top-3 picks with a one-line reason each,
the Reversal Plays list, and a detailed PDF (charts, levels, rationale). Most mornings
this is the whole check-in — if nothing grabs you, you're done.

### When a pick interests you: the cockpit pass

Open the cockpit and give it a few minutes:

- **Mission Control** (`1`) — the SINCE YOU LAST LOOKED recap shows what happened since
  your previous visit; the masthead lamps and Needs-Your-Hand strip surface anything
  actually waiting on you.
- **Candidates** (`2`) — the same picks as the email, but graded **live against the
  latest close**: ✅ actionable · 🏃 already ran · ⛔ stopped. This is the anti-chase
  check — a pick that was fresh at screen time may have run away overnight, and this
  screen is where you find out *before* placing anything.
- **Want a deeper read?** Request one on the **Analyst** screen (`6`) — a single-ticker
  Opus report (chart read + fundamentals + news + sentiment). In the cloud the hourly
  worker drains the queue; locally run `python -m swing_screener.notify.ondemand`.

### Taking a trade (the manual loop)

1. Place the order yourself at your broker (e.g. Robinhood).
2. **Log it** on Positions & Ledger (`3`) — the form prefills entry/stop/target from the
   signal, but never blocks you from taking it your way: if your entry or levels differ
   from the engine's plan, a server-stamped **override** note records the disagreement
   (kept, not lost — it feeds your journal).
3. Sizing: set `SWING_ACCOUNT_EQUITY` (user-level env var) and the prefill computes a
   conviction-scaled share count (1R = equity × 1%); unset, it shows R-multiples only.

### During the day: exit alerts

The hourly exit job (9am–4pm) watches open trades and emails an alert when a tier fires —
🔴 hard stop, 🟠 momentum flip, 🟡 target/time. When you close a position at the broker,
**close it** on screen `3` too; a close you performed yourself is deliberately excluded
from the alert query, so the system won't nag you about it.

### After the close (~4pm): the screen runs itself

The evening screen fires on its own (Azure) — or you run it locally:

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.run --db sqlite:///local.db --cache-dir .cache --chart-dir .charts
```

Nothing to do here except, if you like, a **notebook entry** on the Journal screen
(`g` `j`) while the day is fresh — the Personal Trade Coach reads what you write.

## The week

Meridian's self-improvement is weekly, and it all arrives as things **for you to judge**
— nothing auto-merges, nothing auto-promotes.

| When | What arrives | What you do |
|---|---|---|
| Sunday ~9am | **Market Weather** email — the weekly macro read (SPY regime, VIX, yields) | read it; it sets your posture for the week (also on screen `8`) |
| Sunday morning | up to two **GitHub PRs**: the optimizer's config-change proposal (only on a trusted out-of-sample winner) and the reflection's playbook update | review and merge — or close with a comment; these are the system *proposing*, you *deciding* |
| Friday ~4pm | the **weekly digest** | read |
| Saturday ~4pm | the **System Behavior Auditor's** weekly conduct report | skim + **acknowledge** on System Audit (`g` `a`); breaches (caps exceeded, disarms) come daily and deserve a real look |
| rolling | **Coach reviews** of your closed manual trades + the weekly Weaknesses Profile | read on Journal (`g` `j`); add your own edit beside the Coach's text; confirm or ignore its parked tag proposals |
| rolling | **Forward Books** (`4`) — when an experiment's stopping rule fires, its card goes decision-forcing | work the DECIDE box; the card hands you the exact retire checklist |
| rolling | **Playbooks** (`5`) — queued variant proposals + the reflection-due counter | approve/withdraw; approve *marks* — the promotion is a separate deliberate commit, and the response hands you the checklist |

Want a tuning recommendation *now* instead of waiting for Sunday? In Claude Code, run
**`/tune-screener`** — it runs the optimizer and explains in plain language whether a
gate change is worth making. And **Metrics** (`g` `m`) is the cross-book scoreboard when
you want the "how am I actually doing" glance. The monthly digest arrives on the last
business day of the month.

## The GEX lab day (module 2, if you use it)

The options lab is its own firewalled loop, run from the **GEX Lab** screen (`g` `x`)
or the CLI (`python -m swing_screener.options.run`):

- **Pre-market** — build the day plan: the SPY/QQQ dealer-gamma map + EMA bias →
  breakout / range / **stand-down** call for the session (`plan`, or the Build plan
  button).
- **During the session** — grade any setup against the 12-point A+ checklist (the
  server's grade is authoritative; the auto-grade button pre-fills the machine-checkable
  items), then mark it **taken** or **skipped**. Skipped A+ setups are data too.
- **Post-close** — `settle` sweeps open lab trades and resolves them to R-multiples from
  the session's completed 5-minute bars.
- **Periodically** — import your Robinhood activity CSV and review-tag which trades were
  GEX; they land in a separate premium book, never the swing book.

Charter: [modules/gex-lab.md](modules/gex-lab.md) · playbook: [edge/gex.md](../edge/gex.md).

## Leveling up the automation (when you're ready — not soon)

The ladder is `off` → `manual` → `paper` → `live`, and each rung is a human flip
([README: Execution & money safety](../README.md#execution--money-safety)). Two read-only
commands tell you where you stand at any time:

```powershell
# the advisory autonomy gate + the calibration countdown to its floors
.\.venv\Scripts\python -m swing_screener.pipeline.autonomy

# GO/NO-GO preflight for arming a live broker (reachable / funded / caps / gate)
.\.venv\Scripts\python -m swing_screener.pipeline.preflight
```

The gate cannot pass until enough scored analyst calls accrue — by design. When it ever
reads ready and preflight is GO, the deliberate flip is the
[arming runbook](runbooks/arming-alpaca-live.md). The kill switch is
`SWING_EXECUTION_MODE=off`; the cockpit's **DISARM** button is the venue sweep (cancels
entry-side orders, keeps bracket stops — it never closes positions and never flips the
mode).

## When something looks off

- **Start at Systems** (`9`) — the heartbeat rail shows every scheduled job's pulse.
  A **dashed UNKNOWN is honest**, not broken: sparse writers (exit alerts, on-demand
  analysis, the breach scan) only write when there's something to report, and the cockpit
  never fakes a green.
- **DB chip red while pointed at Azure** → the cached credential expired; click **Sign in
  to Azure** on the chip (or `az login`). [cockpit.md](cockpit.md#troubleshooting-expired-azure-credential)
- **`database error (OperationalError)` with a green DB chip** → your `local.db` predates
  a schema change; the fix (rebuild, or stamp-then-upgrade) is
  [cockpit.md](cockpit.md#troubleshooting-stale-localdb-and-the-phase-3-migration-trap).
- **A digest didn't arrive** → the Reference screen (`g` `r`) has the email log;
  re-running a digest for the same day is idempotent, so a retry can't double-send.
- **Azure job questions** (schedules, logs, operational notes) →
  [azure-deploy.md](azure-deploy.md).

## The document map

| You want to… | Read |
|---|---|
| understand what Meridian *is* and why | [README](../README.md), [NORTH_STAR.md](NORTH_STAR.md) |
| run the screener locally | [running-locally.md](running-locally.md) |
| use the cockpit (screens, actions, config, troubleshooting) | [cockpit.md](cockpit.md) |
| set up or tune the emails / the Opus analyst | [email-digests.md](email-digests.md) |
| deploy the unattended cadence to Azure | [azure-deploy.md](azure-deploy.md) |
| arm real money (much later, gated) | [runbooks/arming-alpaca-live.md](runbooks/arming-alpaca-live.md) |
| see what each strategy has actually proven | the playbooks: [edge/continuation.md](../edge/continuation.md), [edge/reversal.md](../edge/reversal.md), [edge/gex.md](../edge/gex.md) |
| the platform contract / module charters | [ARCHITECTURE.md](ARCHITECTURE.md), [modules/](modules/) |
