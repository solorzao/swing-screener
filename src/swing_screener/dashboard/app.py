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
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics import performance
from swing_screener.dashboard import quotes, ui
from swing_screener.dashboard.pl import position_pl, total_unrealized_pl
from swing_screener.db import repo
from swing_screener.db.models import ExitEvent, PaperTrade, Trade
from swing_screener.db.session import get_engine
from swing_screener.settings import load_settings
from swing_screener.storage.blob import blob_enabled, download_bytes


def _cache_dir() -> Path:
    return load_settings().cache_dir


def _resolve_chart_image(chart_path: str | None) -> bytes | str | None:
    """Resolve a signal's ``chart_path`` to something ``st.image`` can render.

    When blob storage is enabled, ``chart_path`` is a blob KEY (the filesystem is
    not shared across Azure executions): download the bytes, returning ``None`` on
    any failure so a missing/aged-out blob just skips the image. When disabled,
    keep the existing local behavior: return the path iff it exists, else ``None``.
    """
    if not chart_path:
        return None
    if blob_enabled():
        try:
            return download_bytes(chart_path)
        except Exception:
            return None
    if Path(chart_path).exists():
        return chart_path
    return None


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

    df = pd.DataFrame(
        [
            {
                "rank": s.rank,
                "ticker": s.ticker,
                "play_type": s.play_type,
                "strength": s.strength,
                "timeframe": s.timeframe,
                "horizon": s.horizon,
                "score": s.score,
                "rsi": s.rsi,
                "atr": s.atr,
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
            "play_type": st.column_config.TextColumn("Play"),
            "score": st.column_config.ProgressColumn(
                "Score", min_value=0.0, max_value=1.0, format="%.2f"
            ),
            "rsi": st.column_config.NumberColumn("RSI", format="%.0f"),
            "atr": st.column_config.NumberColumn("ATR", format="%.2f"),
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
            rows.append(
                {
                    "ticker": t.ticker,
                    "entry": t.entry_price,
                    "size": t.size,
                    "price": "—",
                    "unrealized_$": "—",
                    "unrealized_%": "—",
                    "R": "—",
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
                    "price": price, "unrealized_$": "—", "unrealized_%": "—",
                    "R": "—", "stop": t.stop, "target": t.target, "status": "⚠️",
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
                "unrealized_$": round(pl.unrealized_pl, 2),
                "unrealized_%": round(pl.unrealized_pct * 100, 2),
                "R": round(pl.r_multiple, 2),
                "stop": t.stop,
                "target": t.target,
                "status": badge,
            }
        )
    st.dataframe(rows, width="stretch")

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


def _render_performance(session: Session) -> None:
    ui.page_header("Screener Performance")
    paper_trades = list(session.scalars(select(PaperTrade)))
    if not paper_trades:
        ui.empty_state("No shadow-book data yet.")
        return

    summary = performance.summarize(paper_trades)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Fill rate", f"{summary.fill_rate * 100:.0f}%")
    c2.metric("Win rate", f"{summary.win_rate * 100:.0f}%")
    c3.metric("Expectancy R", f"{summary.expectancy_r:.2f}")
    pf = summary.profit_factor
    c4.metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}")
    c5.metric("Closed", str(summary.n_closed))

    by_tf = performance.breakdown(paper_trades, "timeframe")
    if by_tf:
        st.markdown("**Win rate by timeframe**")
        st.altair_chart(
            ui.bar({k: v.win_rate for k, v in by_tf.items()}, "Timeframe", "Win rate"),
            width="stretch",
        )

    by_rank = performance.rank_bucket(paper_trades, [5, 10])
    if by_rank:
        st.markdown("**Win rate by rank bucket**")
        st.altair_chart(
            ui.bar({k: v.win_rate for k, v in by_rank.items()}, "Rank bucket", "Win rate"),
            width="stretch",
        )

    curve = performance.equity_curve(paper_trades)
    if curve:
        st.markdown("**Equity curve (cumulative R)**")
        # ui.line wants list[tuple[object, float]]; list is invariant, so widen the
        # date-keyed curve to the expected element type for the type checker.
        points: list[tuple[object, float]] = [(d, r) for d, r in curve]
        st.altair_chart(ui.line(points, "Exit date", "Cumulative R"), width="stretch")


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

    # Filter controls (main area). Default = every reason selected + "All" book, so
    # the unfiltered view is unchanged.
    reasons = sorted({e.reason for e in events})
    selected = st.multiselect("Reason", reasons, default=reasons)
    book = st.radio("Book", ["All", "Paper", "Real"], horizontal=True, index=0)

    events = [e for e in events if e.reason in selected]
    if book == "Paper":
        events = [e for e in events if e.is_paper is True]
    elif book == "Real":
        events = [e for e in events if e.is_paper is False]

    if not events:
        ui.empty_state("No exit events match the filters.")
        return

    df = pd.DataFrame(
        [
            {
                "date": e.created_date,
                "trade_id": e.trade_id,
                "is_paper": e.is_paper,
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
    win_rate = performance.summarize(list(session.scalars(select(PaperTrade)))).win_rate

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
    ui.page_header("Universe")
    ui.empty_state("Universe view coming soon.")


def _render_digests(session: Session) -> None:
    ui.page_header("Digest Log")
    ui.empty_state("Digest log coming soon.")


# label -> renderer. Order defines sidebar order; first entry is the default
# landing page. Radio nav (not st.navigation) so AppTest can drive page switches.
PAGES: dict[str, Callable[[Session], None]] = {
    "Overview": _render_overview,
    "Today's Candidates": _render_candidates,
    "Active Trades": _render_active,
    "Trade Entry": _render_entry,
    "Closed Trades": _render_closed,
    "Screener Performance": _render_performance,
    "Exit Log": _render_exits,
    "Universe": _render_universe,
    "Digest Log": _render_digests,
}


def render() -> None:
    ui.inject_css()
    db_url = load_settings().db_url  # read fresh each rerun (env-resolved)
    engine = get_engine(db_url)  # ensure tables exist; empty DB is fine
    st.sidebar.title("📈 Swing Screener")
    choice = st.sidebar.radio("Navigate", list(PAGES), label_visibility="collapsed")
    renderer = PAGES[choice]
    with Session(engine) as session:
        with ui.error_boundary(choice):
            renderer(session)


render()
