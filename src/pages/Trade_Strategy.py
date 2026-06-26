"""Strategy explainer page. Renders docs/strategy.md.

Reached via the sidebar link on the main page. The default multipage nav is
hidden (see app.py / config), so this only appears through that link.
"""

import os

import streamlit as st

# docs/strategy.md lives at the repo root (this file is src/pages/).
DOC = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "strategy.md")

# Default nav is hidden, so provide an explicit way back to the backtest.
st.page_link("app.py", label="< Back to backtest")

with open(DOC, encoding="utf-8") as f:
    st.markdown(f.read())
