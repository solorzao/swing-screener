"""Shared presentation helpers for the dashboard (theme, layout, formatting, charts)."""

from collections.abc import Iterator
from contextlib import contextmanager

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
    """Inject the dashboard's global CSS (chrome hiding + card styling)."""
    st.markdown(_CSS, unsafe_allow_html=True)


def fmt_pct(frac: float) -> str:
    """Format a fraction as a signed percentage, e.g. 0.1234 -> '+12.34%'."""
    return f"{frac * 100:+.2f}%"


def fmt_money(amount: float) -> str:
    """Format a dollar amount, e.g. 1234.5 -> '$1,234.50', -12.0 -> '-$12.00'."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def pl_color(value: float) -> str:
    """Semantic color for a P/L value: green when > 0, red otherwise (break-even = red)."""
    return POS if value > 0 else NEG


def page_header(title: str, caption: str | None = None) -> None:
    """Standard page heading with optional caption."""
    st.header(title)
    if caption:
        st.caption(caption)


def empty_state(message: str) -> None:
    """Friendly empty-state message used when a view has no data."""
    st.info(message, icon="📭")


@contextmanager
def error_boundary(view_name: str) -> Iterator[None]:
    """Render any exception as a calm card with collapsible details, never a stack trace."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - top of a view; intentionally catch all
        st.error(f"Couldn't load **{view_name}**.", icon="⚠️")
        with st.expander("Technical details"):
            st.code(f"{type(exc).__name__}: {exc}")
