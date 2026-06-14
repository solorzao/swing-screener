"""Local Streamlit dashboard shell over the screener's SQLite store.

Reads its DB URL from ``SWING_DB_URL`` (default ``sqlite:///local.db``) so tests
can point it at a temp database. Streamlit runs this file top-to-bottom on every
rerun, so :func:`render` is called unconditionally at the bottom.
"""

import os

import streamlit as st

from swing_screener.db.session import get_engine

DB_URL = os.environ.get("SWING_DB_URL", "sqlite:///local.db")

TAB_LABELS = [
    "Today's Candidates",
    "Active Trades",
    "Trade Entry",
    "Closed Trades",
    "Screener Performance",
    "Exit Log",
]


def render() -> None:
    st.title("Swing Screener")
    get_engine(DB_URL)  # ensure tables exist; empty DB is fine
    st.sidebar.caption(f"DB: {DB_URL}")

    tabs = st.tabs(TAB_LABELS)
    placeholders = {
        "Today's Candidates": "No candidates yet — run the screener.",
        "Active Trades": "No open trades.",
        "Trade Entry": "Log a trade here.",
        "Closed Trades": "No closed trades.",
        "Screener Performance": "No shadow-book data yet.",
        "Exit Log": "No exit events.",
    }
    for tab, label in zip(tabs, TAB_LABELS, strict=True):
        with tab:
            st.subheader(label)
            st.write(placeholders[label])


render()
