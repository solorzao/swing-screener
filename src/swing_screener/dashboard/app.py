"""Local Streamlit dashboard over the screener's SQLite store.

Configuration comes from the centralized :func:`load_settings` (DB URL from
``SWING_DB_URL`` default ``sqlite:///local.db``; quote cache directory from
``SWING_CACHE_DIR`` default ``.cache``, resolved absolute so a container with a
different cwd still works) so tests can point them at temp locations. Streamlit
runs this file top-to-bottom on every rerun, so :func:`render` is called
unconditionally at the bottom and reads settings fresh each run.

Each tab is a small ``_render_*`` function taking an open :class:`Session`.
Quotes are fetched through ``quotes.latest_closes`` (a module attribute) so a
test can monkeypatch that seam and avoid the network.
"""

from datetime import date
from pathlib import Path

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics import performance
from swing_screener.dashboard import quotes
from swing_screener.dashboard.pl import position_pl
from swing_screener.db import repo
from swing_screener.db.models import ExitEvent, PaperTrade, Trade
from swing_screener.db.session import get_engine
from swing_screener.settings import load_settings

TAB_LABELS = [
    "Today's Candidates",
    "Active Trades",
    "Trade Entry",
    "Closed Trades",
    "Screener Performance",
    "Exit Log",
]


def _cache_dir() -> Path:
    return load_settings().cache_dir


def _render_candidates(session: Session) -> None:
    st.subheader("Today's Candidates")
    run_date = repo.latest_run_date(session)
    signals = repo.latest_signals(session, run_date) if run_date else []
    if not signals:
        st.write("No candidates yet — run the screener.")
        return
    st.caption(f"Latest run: {run_date}")

    rows = [
        {
            "ticker": s.ticker,
            "timeframe": s.timeframe,
            "horizon": s.horizon,
            "score": s.score,
            "mtf_aligned": s.mtf_aligned,
            "quality_tier": s.quality_tier,
            "volatility_tier": s.volatility_tier,
            "entry_floor": s.entry_floor,
            "entry_ceiling": s.entry_ceiling,
            "stop": s.stop,
            "target": s.target,
        }
        for s in signals
    ]
    st.dataframe(rows, width="stretch")

    for s in signals:
        if s.chart_path and Path(s.chart_path).exists():
            st.image(s.chart_path, caption=s.ticker)


def _render_active(session: Session) -> None:
    st.subheader("Active Trades")
    trades = repo.get_open_trades(session)
    if not trades:
        st.write("No open trades.")
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


def _render_entry(session: Session) -> None:
    st.subheader("Trade Entry")
    with st.form("trade_entry"):
        ticker = st.text_input("Ticker")
        timeframe = st.text_input("Timeframe", value="1d")
        horizon = st.text_input("Horizon", value="medium")
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
    st.subheader("Closed Trades")
    trades = repo.get_closed_trades(session)
    if not trades:
        st.write("No closed trades.")
        return

    rows = []
    cumulative = 0.0
    for t in trades:
        exit_price = t.exit_price if t.exit_price is not None else t.entry_price
        realized = (exit_price - t.entry_price) * t.size
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
    st.dataframe(rows, width="stretch")
    st.metric("Cumulative realized P/L", f"${cumulative:,.2f}")


def _render_performance(session: Session) -> None:
    st.subheader("Screener Performance")
    paper_trades = list(session.scalars(select(PaperTrade)))
    if not paper_trades:
        st.write("No shadow-book data yet.")
        return

    summary = performance.summarize(paper_trades)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Fill rate", f"{summary.fill_rate * 100:.0f}%")
    c2.metric("Win rate", f"{summary.win_rate * 100:.0f}%")
    c3.metric("Expectancy R", f"{summary.expectancy_r:.2f}")
    pf = summary.profit_factor
    c4.metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}")
    c5.metric("Closed", str(summary.n_closed))

    st.markdown("**Win rate by timeframe**")
    by_tf = performance.breakdown(paper_trades, "timeframe")
    if by_tf:
        st.bar_chart({k: v.win_rate for k, v in by_tf.items()})

    st.markdown("**Win rate by rank bucket**")
    by_rank = performance.rank_bucket(paper_trades, [5, 10])
    if by_rank:
        st.bar_chart({k: v.win_rate for k, v in by_rank.items()})

    st.markdown("**Equity curve (cumulative R)**")
    curve = performance.equity_curve(paper_trades)
    if curve:
        st.line_chart({str(d): r for d, r in curve})


def _render_exits(session: Session) -> None:
    st.subheader("Exit Log")
    events = list(
        session.scalars(
            select(ExitEvent).order_by(ExitEvent.created_date.desc())
        )
    )
    if not events:
        st.write("No exit events.")
        return

    rows = [
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
    st.dataframe(rows, width="stretch")


def render() -> None:
    st.title("Swing Screener")
    db_url = load_settings().db_url  # read fresh each rerun (env-resolved)
    engine = get_engine(db_url)  # ensure tables exist; empty DB is fine
    st.sidebar.caption(f"DB: {db_url}")

    renderers = [
        _render_candidates,
        _render_active,
        _render_entry,
        _render_closed,
        _render_performance,
        _render_exits,
    ]
    with Session(engine) as session:
        tabs = st.tabs(TAB_LABELS)
        for tab, renderer in zip(tabs, renderers, strict=True):
            with tab:
                renderer(session)


render()
