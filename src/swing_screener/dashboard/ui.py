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
    """Inject the dashboard's global CSS (chrome hiding + card styling)."""
    st.markdown(_CSS, unsafe_allow_html=True)
