"""Local Streamlit dashboard over the screener's SQLite store.

Configuration comes from the centralized :func:`load_settings` (DB URL from
``SWING_DB_URL`` default ``sqlite:///local.db``; quote cache directory from
``SWING_CACHE_DIR`` default ``.cache``, resolved absolute so a container with a
different cwd still works) so tests can point them at temp locations. Streamlit
runs this file top-to-bottom on every rerun, so :func:`render` is called
unconditionally at the bottom and reads settings fresh each run.

Each page is a small ``_render_*`` function taking an open :class:`Session`,
dispatched from the ``PAGES`` registry via the sidebar radio navigation.
Quotes are fetched through ``quotes.latest_closes`` (a module attribute) so a
test can monkeypatch that seam and avoid the network.
"""

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pandas as pd
import streamlit as st
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from swing_screener.analytics import performance
from swing_screener.analytics.pl import position_pl, total_unrealized_pl
from swing_screener.dashboard import ui
from swing_screener.data import quotes
from swing_screener.db import repo
from swing_screener.db.models import AnalystCall, ExitEvent, PaperTrade, Signal, Trade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.reflect import analyst_calibration
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import load_settings
from swing_screener.signals.actionability import classify as classify_actionability
from swing_screener.storage.blob import resolve_chart_bytes, resolve_pdf_bytes


@st.cache_resource
def _cached_engine(db_url: str) -> Engine:
    """Build the engine once per session (keyed by db_url). Exceptions are NOT cached,
    so a transient DB-down still re-attempts and the connection guard can report it."""
    return get_engine(db_url)


# Verifying connectivity costs a round-trip (on Azure SQL, validating a pooled connection)
# on EVERY rerun. Cache the SUCCESS briefly so a burst of interactions doesn't reconnect each
# time. st.cache_data caches returns but NOT exceptions, so a DB that goes down is still
# detected on the next rerun (and recovery shows within the TTL); the per-view error_boundary
# stays the immediate safety net if the DB drops mid-window.
_CONN_CHECK_TTL_SECONDS = 30


@st.cache_data(ttl=_CONN_CHECK_TTL_SECONDS, show_spinner=False)
def _connection_ok(db_url: str) -> bool:
    """True if the cached engine can open a connection; raises (uncached) if it cannot."""
    with _cached_engine(db_url).connect():
        pass
    return True


def _cache_dir() -> Path:
    return load_settings().cache_dir


def _resolve_chart_image(chart_path: str | None) -> bytes | None:
    """Resolve a signal's ``chart_path`` to bytes for ``st.image`` (None skips it).

    Delegates to :func:`swing_screener.storage.blob.resolve_chart_bytes`, which
    owns the blob-key-vs-local-path branching. Bytes now even for local files
    (``st.image`` accepts both; the cockpit API needs bytes).
    """
    return resolve_chart_bytes(chart_path)


def _resolve_pdf_bytes(key: str | None) -> bytes | None:
    """Resolve a report's ``pdf_blob_key`` to raw bytes for ``st.download_button``.

    Delegates to :func:`swing_screener.storage.blob.resolve_pdf_bytes`; ``None``
    on any miss just hides the button.
    """
    return resolve_pdf_bytes(key)


def _render_candidates(session: Session) -> None:
    ui.page_header("Today's Candidates")
    run_date = repo.latest_run_date(session)
    signals = repo.latest_signals(session, run_date) if run_date else []
    if not signals:
        ui.empty_state("No candidates yet — run the screener.")
        return
    st.caption(f"Latest run: {run_date}")

    # Play-type filter in the main area (the sidebar radio is the page nav).
    options = ["All", "Continuation", "Reversal"]
    choice = st.segmented_control("Play type", options, default="All")
    if choice == "Continuation":
        signals = [s for s in signals if s.play_type == "continuation"]
    elif choice == "Reversal":
        signals = [s for s in signals if s.play_type == "reversal"]
    if not signals:
        ui.empty_state(f"No {choice} plays in this run.")
        return

    # Live actionability: a signal is computed at the trigger close but read later, by
    # which point price may have run past the entry (a chase) or broken the stop. Tag
    # each pick against its latest close so already-ran picks can be flagged + hidden.
    prices = quotes.latest_closes([s.ticker for s in signals], cache_dir=_cache_dir())
    act = {
        s.id: classify_actionability(
            entry_floor=s.entry_floor, entry_ceiling=s.entry_ceiling,
            stop=s.stop, price=prices.get(s.ticker),
        )
        for s in signals
    }

    hide_ran = st.checkbox(
        "Hide plays that already ran (price past entry or stop)", value=True,
        help="Keeps only setups whose latest price is still in or below the entry zone. "
             "Picks with no live quote are kept.",
    )
    if hide_ran:
        kept = [s for s in signals if act[s.id].status in ("actionable", "unknown")]
        if not kept:
            ui.empty_state("Every pick in this run has already run past its entry. "
                           "Untick the filter to see them all.")
            return
        signals = kept

    # Staleness: a "repeat" is a setup first seen on an earlier run (its streak started
    # before today). Hiding them keeps the list to genuinely new triggers.
    def _is_repeat(s: Signal) -> bool:
        return (s.first_seen_date is not None and run_date is not None
                and s.first_seen_date < run_date)

    hide_repeats = st.checkbox(
        "Hide repeats (first seen on an earlier run)", value=True,
        help="Shows only setups that first appeared in the latest run, so the same play "
             "isn't surfaced day after day. Picks with no first-seen date are kept.",
    )
    if hide_repeats:
        kept = [s for s in signals if not _is_repeat(s)]
        if not kept:
            ui.empty_state("Every pick here is a repeat from an earlier run. "
                           "Untick the filter to see them all.")
            return
        signals = kept

    _STATUS_LABEL = {"actionable": "✅ actionable", "extended": "🏃 already ran",
                     "broken": "⛔ stopped", "unknown": "· no quote"}
    df = pd.DataFrame(
        [
            {
                "rank": s.rank,
                "ticker": s.ticker,
                "status": _STATUS_LABEL[act[s.id].status],
                "past_entry_r": act[s.id].dist_r,
                "play_type": s.play_type,
                "strength": s.strength,
                "timeframe": s.timeframe,
                "horizon": s.horizon,
                "score": s.score,
                "rsi": s.rsi,
                "atr": s.atr,
                "extension": s.extension_atr,
                "first_seen": s.first_seen_date,
                "mtf_aligned": s.mtf_aligned,
                "oversold": s.oversold,
                "quality_tier": s.quality_tier,
                "volatility_tier": s.volatility_tier,
                "entry_floor": s.entry_floor,
                "entry_ceiling": s.entry_ceiling,
                "stop": s.stop,
                "target": s.target,
            }
            for s in signals
        ]
    )
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "rank": st.column_config.NumberColumn("Rank", format="%d"),
            "status": st.column_config.TextColumn("Status"),
            "past_entry_r": st.column_config.NumberColumn(
                "Past entry (R)", format="%.2f",
                help="How far the latest price sits above the entry ceiling, in R. "
                     "≤ 0 means there's still room to enter; > 0 means it ran.",
            ),
            "play_type": st.column_config.TextColumn("Play"),
            "score": st.column_config.ProgressColumn(
                "Score", min_value=0.0, max_value=1.0, format="%.2f"
            ),
            "rsi": st.column_config.NumberColumn("RSI", format="%.0f"),
            "atr": st.column_config.NumberColumn("ATR", format="%.2f"),
            "extension": st.column_config.NumberColumn(
                "Ext (ATR)", format="%.2f",
                help="How far the trigger ran above EMA20, in ATR. Higher = closer to a "
                     "chase (continuation only).",
            ),
            "first_seen": st.column_config.DateColumn("First seen", format="MMM DD"),
            "entry_floor": st.column_config.NumberColumn("Entry ▼", format="$%.2f"),
            "entry_ceiling": st.column_config.NumberColumn("Entry ▲", format="$%.2f"),
            "stop": st.column_config.NumberColumn("Stop", format="$%.2f"),
            "target": st.column_config.NumberColumn("Target", format="$%.2f"),
        },
    )

    for s in signals:
        image = _resolve_chart_image(s.chart_path)
        if image is not None:
            with st.expander(f"{s.ticker} chart"):
                st.image(image, width="stretch")


def _render_active(session: Session) -> None:
    ui.page_header("Active Trades")
    trades = repo.get_open_trades(session)
    if not trades:
        ui.empty_state("No open trades.")
        return

    prices = quotes.latest_closes(
        [t.ticker for t in trades], cache_dir=_cache_dir()
    )

    rows = []
    for t in trades:
        price = prices.get(t.ticker)
        if price is None:
            # Missing quote — keep numeric P/L fields as None (blanks via the
            # Styler) rather than a "—" string, so the column stays numeric.
            rows.append(
                {
                    "ticker": t.ticker,
                    "entry": t.entry_price,
                    "size": t.size,
                    "price": None,
                    "unrealized_$": None,
                    "unrealized_%": None,
                    "R": None,
                    "stop": t.stop,
                    "target": t.target,
                    "status": "—",
                }
            )
            continue

        try:
            pl = position_pl(
                entry=t.entry_price,
                stop=t.stop,
                target=t.target,
                size=t.size,
                current_price=price,
            )
        except ValueError:
            # malformed trade (non-positive risk) — show the price but no P/L
            # rather than letting one bad row crash the whole tab.
            rows.append(
                {
                    "ticker": t.ticker, "entry": t.entry_price, "size": t.size,
                    "price": price, "unrealized_$": None, "unrealized_%": None,
                    "R": None, "stop": t.stop, "target": t.target, "status": "⚠️",
                }
            )
            continue
        if price <= t.stop:
            badge = "🔴"
        elif price >= t.target:
            badge = "🟡"
        else:
            badge = "🟢"
        rows.append(
            {
                "ticker": t.ticker,
                "entry": t.entry_price,
                "size": t.size,
                "price": price,
                "unrealized_$": pl.unrealized_pl,
                # store the FRACTION; ui.fmt_pct multiplies by 100 when formatting.
                "unrealized_%": pl.unrealized_pct,
                "R": pl.r_multiple,
                "stop": t.stop,
                "target": t.target,
                "status": badge,
            }
        )
    df = pd.DataFrame(
        rows,
        columns=["ticker", "entry", "size", "price", "unrealized_$",
                 "unrealized_%", "R", "stop", "target", "status"],
    )

    def _money(v: object) -> str:
        return ui.fmt_money(v) if isinstance(v, (int, float)) and pd.notna(v) else "—"

    def _pct(v: object) -> str:
        return ui.fmt_pct(v) if isinstance(v, (int, float)) and pd.notna(v) else "—"

    def _r(v: object) -> str:
        return f"{v:+.2f}R" if isinstance(v, (int, float)) and pd.notna(v) else "—"

    def _color_pl(v: object) -> str:
        if isinstance(v, (int, float)) and pd.notna(v):
            return f"color: {ui.pl_color(v)}"
        return ""

    styler = (
        df.style.format(
            {
                "entry": _money,
                "price": _money,
                "stop": _money,
                "target": _money,
                "unrealized_$": _money,
                "unrealized_%": _pct,
                "R": _r,
                "size": "{:.0f}",
            }
        ).map(_color_pl, subset=["unrealized_$", "unrealized_%", "R"])
    )
    st.dataframe(styler, width="stretch", hide_index=True)

    st.subheader("Close a trade")
    # `trades` are the same open trades shown above; carry id + ticker on the label.
    options = {f"#{t.id} · {t.ticker}": (t.id, t.ticker) for t in trades}
    selected_label = st.selectbox("Trade", list(options))
    with st.form("close_trade"):
        exit_date = st.date_input("Exit date", value=date.today())
        exit_price = st.number_input("Exit price", min_value=0.0, value=0.0)
        exit_reason = st.text_input("Exit reason", value="manual")
        submitted = st.form_submit_button("Close trade")

    if submitted:
        if exit_price <= 0:
            st.warning("Exit price must be positive.")
        else:
            trade_id, ticker = options[selected_label]
            repo.close_trade(
                session,
                trade_id,
                exit_date=exit_date,
                exit_price=exit_price,
                exit_reason=exit_reason.strip() or "manual",
            )
            st.success(f"Closed #{trade_id} {ticker}.")
            st.rerun()


def _render_entry(session: Session) -> None:
    ui.page_header(
        "Trade Entry",
        caption="Log a trade you actually took. This records it — it does not place an order.",
    )
    with st.form("trade_entry"):
        c1, c2 = st.columns(2)
        with c1:
            ticker = st.text_input("Ticker")
            timeframe = st.text_input("Timeframe", value="1d")
            horizon = st.text_input("Horizon", value="medium")
        with c2:
            entry_price = st.number_input("Entry price", min_value=0.0, value=0.0)
            size = st.number_input("Size", min_value=0.0, value=0.0)
            stop = st.number_input("Stop", min_value=0.0, value=0.0)
            target = st.number_input("Target", min_value=0.0, value=0.0)
        notes = st.text_area("Notes", value="")
        submitted = st.form_submit_button("Add trade")

    if submitted:
        if not ticker:
            st.warning("Ticker is required.")
        elif entry_price <= 0 or size <= 0:
            st.warning("Entry price and size must be positive.")
        elif stop >= entry_price:
            st.warning("Stop must be below the entry price (long).")
        elif target <= entry_price:
            st.warning("Target must be above the entry price (long).")
        else:
            trade = Trade(
                ticker=ticker.strip().upper(),
                timeframe=timeframe,
                horizon=horizon,
                entry_date=date.today(),
                entry_price=entry_price,
                size=size,
                stop=stop,
                target=target,
                notes=notes,
            )
            repo.add_trade(session, trade)
            st.success(f"Added trade for {trade.ticker}.")


def _render_closed(session: Session) -> None:
    ui.page_header("Closed Trades")
    trades = repo.get_closed_trades(session)
    if not trades:
        ui.empty_state("No closed trades.")
        return

    def _realized(t: Trade) -> float:
        exit_price = t.exit_price if t.exit_price is not None else t.entry_price
        return (exit_price - t.entry_price) * t.size

    rows = []
    cumulative = 0.0
    for t in trades:
        realized = _realized(t)
        cumulative += realized
        rows.append(
            {
                "ticker": t.ticker,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "entry": t.entry_price,
                "exit": t.exit_price,
                "size": t.size,
                "realized_$": round(realized, 2),
                "exit_reason": t.exit_reason or "",
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "ticker": st.column_config.TextColumn("Ticker"),
            "entry_date": st.column_config.DateColumn("Entry date"),
            "exit_date": st.column_config.DateColumn("Exit date"),
            "entry": st.column_config.NumberColumn("Entry", format="$%.2f"),
            "exit": st.column_config.NumberColumn("Exit", format="$%.2f"),
            "size": st.column_config.NumberColumn("Size", format="%.0f"),
            "realized_$": st.column_config.NumberColumn("Realized $", format="$%.2f"),
            "exit_reason": st.column_config.TextColumn("Exit reason"),
        },
    )
    st.metric("Cumulative realized P/L", ui.fmt_money(cumulative))

    # Realized equity curve: cumulative realized $ over time. Sort dated closes
    # ascending by exit_date and running-sum; skip undated closes. The generator
    # guarantees exit_date is not None, so the key can assert it for the type
    # checker (None is not sortable).
    dated = sorted(
        (t for t in trades if t.exit_date is not None),
        key=lambda t: cast(date, t.exit_date),
    )
    points: list[tuple[object, float]] = []
    running = 0.0
    for t in dated:
        running += _realized(t)
        points.append((t.exit_date, round(running, 2)))
    if points:
        st.markdown("**Realized equity curve**")
        st.altair_chart(ui.line(points, "Exit date", "Cumulative $"), width="stretch")


def _render_exits(session: Session) -> None:
    ui.page_header("Exit Log")
    events = list(
        session.scalars(
            select(ExitEvent).order_by(ExitEvent.created_date.desc())
        )
    )
    if not events:
        ui.empty_state("No exit events.")
        return

    # Filter controls (main area). Default = every reason selected + "All" book +
    # "All" account, so the unfiltered view is unchanged. The Account facet splits the
    # research grid from the curated intent book -- both record under is_paper=True, so
    # the Book radio alone can't separate them.
    reasons = sorted({e.reason for e in events})
    selected = st.multiselect("Reason", reasons, default=reasons)
    book = st.radio("Book", ["All", "Paper", "Real"], horizontal=True, index=0)
    account_choice = st.radio(
        "Account", ["All", *sorted({e.account for e in events})], horizontal=True, index=0
    )

    events = [e for e in events if e.reason in selected]
    if book == "Paper":
        events = [e for e in events if e.is_paper is True]
    elif book == "Real":
        events = [e for e in events if e.is_paper is False]
    if account_choice != "All":
        events = [e for e in events if e.account == account_choice]

    if not events:
        ui.empty_state("No exit events match the filters.")
        return

    df = pd.DataFrame(
        [
            {
                "date": e.created_date,
                "trade_id": e.trade_id,
                "is_paper": e.is_paper,
                "account": e.account,
                "tier": e.tier,
                "reason": e.reason,
                "message": e.message,
            }
            for e in events
        ]
    )
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={"date": st.column_config.DateColumn("Date")},
    )


def _render_overview(session: Session) -> None:
    run_date = repo.latest_run_date(session)
    caption = f"Latest run: {run_date}" if run_date else "No screener run yet"
    ui.page_header("Overview", caption=caption)

    open_trades = repo.get_open_trades(session)

    # Unrealized P/L across open positions, guarded so a missing quote or one
    # malformed trade (non-positive risk) never crashes the landing page.
    prices = (
        quotes.latest_closes([t.ticker for t in open_trades], cache_dir=_cache_dir())
        if open_trades
        else {}
    )
    total_unrealized = total_unrealized_pl(open_trades, prices)

    candidates = len(repo.latest_signals(session, run_date)) if run_date else 0
    # headline win rate is the live screen (default variant) on the baseline exit arm, so
    # neither the extra exit arms nor the screen-variant books move the landing-page number.
    # Pinned to the research grid so the curated intent book never moves the headline.
    baseline_trades = list(session.scalars(
        select(PaperTrade).where(PaperTrade.account == "research",
                                 PaperTrade.arm == BASELINE,
                                 PaperTrade.variant == DEFAULT_VARIANT)))
    win_rate = performance.summarize(baseline_trades).win_rate

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open positions", len(open_trades))
    c2.metric("Unrealized P/L", ui.fmt_money(total_unrealized))
    c3.metric("Today's candidates", candidates)
    c4.metric("Screener win rate", f"{win_rate * 100:.0f}%")

    st.subheader("Recent activity")
    events = list(
        session.scalars(select(ExitEvent).order_by(ExitEvent.created_date.desc()))
    )[:5]
    if not events:
        ui.empty_state("No recent activity yet.")
        return
    rows = [
        {
            "date": e.created_date,
            "tier": e.tier,
            "reason": e.reason,
            "message": e.message,
        }
        for e in events
    ]
    st.dataframe(rows, width="stretch")


def _render_universe(session: Session) -> None:
    ui.page_header("Universe", caption="The screening universe.")
    search = st.text_input("Search ticker", value="")
    rows = repo.list_universe(session, search or None)
    if not rows:
        ui.empty_state("No tickers match." if search else "Universe is empty.")
        return

    df = pd.DataFrame(
        [
            {
                "ticker": u.ticker,
                "name": u.name,
                "exchange": u.exchange,
                # pre-formatted to human-readable magnitudes ($36.2B); the raw ints
                # are unreadable as a NumberColumn (no thousands grouping / suffix).
                "market_cap": ui.fmt_compact_usd(u.market_cap),
                "avg_dollar_volume": ui.fmt_compact_usd(u.avg_dollar_volume),
            }
            for u in rows
        ]
    )
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "market_cap": st.column_config.TextColumn("Market cap"),
            "avg_dollar_volume": st.column_config.TextColumn("Avg $ vol"),
        },
    )
    st.caption(f"{len(rows)} tickers")


def _render_digests(session: Session) -> None:
    ui.page_header("Digest Log", caption="Emails the screener has sent.")
    rows = repo.list_email_log(session)
    if not rows:
        ui.empty_state("No digests sent yet.")
        return

    df = pd.DataFrame(
        [
            {
                "sent_at": e.sent_at,
                "kind": e.kind,
                "subject": e.subject,
                "run_date": e.run_date,
            }
            for e in rows
        ]
    )
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "sent_at": st.column_config.DatetimeColumn("Sent"),
            "run_date": st.column_config.DateColumn("Run date"),
        },
    )


def _render_analysis(session: Session) -> None:
    ui.page_header(
        "Deep Analysis",
        caption="Request an in-depth multi-timeframe read of a ticker. "
                "You'll get an email when it's ready; it also shows here.",
    )
    with st.form("request_analysis"):
        ticker = st.text_input("Ticker")
        submitted = st.form_submit_button("Request report")
    if submitted:
        if not ticker.strip():
            st.warning("Ticker is required.")
        else:
            repo.create_analysis_request(
                session, ticker=ticker.strip().upper(), requested_at=datetime.now()
            )
            st.success(f"Queued a report for {ticker.strip().upper()}.")
            st.rerun()

    if st.button("Refresh"):
        st.rerun()

    requests = repo.list_analysis_requests(session)
    if not requests:
        ui.empty_state("No analysis requests yet.")
        return

    badge = {"queued": "🕓 queued", "running": "⏳ running",
             "done": "✅ done", "failed": "⚠️ failed"}
    rows = [
        {
            "id": r.id,
            "ticker": r.ticker,
            "status": badge.get(r.status, r.status),
            "requested_at": r.requested_at,
            "summary": r.summary,
        }
        for r in requests
    ]
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={"requested_at": st.column_config.DatetimeColumn("Requested")},
    )

    done = [r for r in requests if r.status == "done"]
    failed = [r for r in requests if r.status == "failed"]
    if done:
        labels = {f"#{r.id} · {r.ticker}": r.id for r in done}
        sel = st.selectbox("View a completed report", list(labels))
        req = repo.get_analysis_request(session, labels[sel])
        if req is not None:
            st.markdown(f"**{req.ticker}** — {req.summary}")
            for key in (req.chart_blob_keys or "").split(","):
                key = key.strip()
                if not key:
                    continue
                image = _resolve_chart_image(key)
                if image is not None:
                    st.image(image, width="stretch")
            pdf_bytes = _resolve_pdf_bytes(req.pdf_blob_key)
            if pdf_bytes is not None:
                st.download_button(
                    "Download PDF",
                    data=pdf_bytes,
                    file_name=f"{req.ticker}_report.pdf",
                    mime="application/pdf",
                )
            elif req.pdf_blob_key:
                st.caption("PDF unavailable.")
    for r in failed:
        with st.expander(f"⚠️ {r.ticker} (#{r.id}) failed"):
            st.code(r.error or "unknown error")


def _render_calibration(session: Session) -> None:
    """The analyst-calibration view: are the Opus conviction calls proving out?

    Reads the SCORED ``AnalystCall`` rows per play type and runs the same pure
    ``analyst_calibration`` helper the reflection uses, so the dashboard and the edge
    file agree by construction. Per conviction grade: how many scored calls + their mean
    realized R (does ``high`` out-earn ``low``?). Plus the nudge line: how the analyst's
    moves (final != baseline) fared vs. simply keeping the baseline."""
    ui.page_header(
        "Analyst Calibration",
        caption="Scored conviction calls: is the analyst's judgment earning R?",
    )
    calls = list(session.scalars(select(AnalystCall)))
    scored = [c for c in calls if c.scored_at is not None]
    if not scored:
        ui.empty_state(
            f"No scored analyst calls yet ({len(calls)} pending). "
            "Calls are scored once their shadow-book trade closes."
        )
        return

    for play_type in sorted({c.play_type for c in scored}):
        pt_calls = [c for c in calls if c.play_type == play_type]
        calib = analyst_calibration(pt_calls)
        st.markdown(f"**{play_type.capitalize()}**")
        rows = [
            {"conviction": grade, "scored_calls": n, "mean_r": mean_r}
            for grade, (n, mean_r) in calib["by_conviction"].items()
        ]
        st.dataframe(
            pd.DataFrame(rows),
            width="stretch",
            hide_index=True,
            column_config={
                "conviction": st.column_config.TextColumn("Conviction"),
                "scored_calls": st.column_config.NumberColumn("Scored calls"),
                "mean_r": st.column_config.NumberColumn("Mean R", format="%.2f"),
            },
        )
        nudge = calib["nudge_vs_baseline_r"]
        if nudge is not None:
            n, mean_r = nudge
            st.caption(f"Nudges (final != baseline): mean {mean_r:+.2f}R over n={n}.")
        else:
            st.caption("Nudges (final != baseline): none scored yet.")


# label -> renderer. Order defines sidebar order; first entry is the default
# landing page. Radio nav (not st.navigation) so AppTest can drive page switches.
PAGES: dict[str, Callable[[Session], None]] = {
    "Overview": _render_overview,
    "Today's Candidates": _render_candidates,
    "Deep Analysis": _render_analysis,
    "Active Trades": _render_active,
    "Trade Entry": _render_entry,
    "Closed Trades": _render_closed,
    "Analyst Calibration": _render_calibration,
    "Exit Log": _render_exits,
    "Universe": _render_universe,
    "Digest Log": _render_digests,
}


def render() -> None:
    ui.inject_css()
    db_url = load_settings().db_url  # read fresh each rerun (env-resolved)
    st.sidebar.title("📈 Swing Screener")
    choice = st.sidebar.radio("Navigate", list(PAGES), label_visibility="collapsed")

    # Acquire the engine and verify connectivity ONCE, guarded, so a DB-down
    # condition becomes a single clear full-page card + a red chip — instead of
    # every view tripping the generic error boundary. The chip never prints
    # credentials/host (see ui.connection_label).
    label = ui.connection_label(db_url)
    conn_error: Exception | None = None
    try:
        _connection_ok(db_url)  # cached briefly; raises (uncached) if the DB is unreachable
        connected = True
    except Exception as exc:  # noqa: BLE001 - surfaced as a friendly card below
        connected = False
        conn_error = exc
    st.sidebar.caption(f"{'🟢' if connected else '🔴'} {label}")

    if not connected:
        st.error("Can't reach the database.", icon="🔌")
        st.caption(f"Target: {label}. Check your connection / `az login` and reload.")
        with st.expander("Technical details"):
            st.code(f"{type(conn_error).__name__}: {conn_error}")
        return

    engine = _cached_engine(db_url)  # cached; _connection_ok already proved it connects
    renderer = PAGES[choice]
    with Session(engine) as session:
        with ui.error_boundary(choice):
            renderer(session)


render()
