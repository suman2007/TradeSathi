"""Run the real dashboard.py headless (Streamlit AppTest) against a pre-computed offline result."""
import os

import pandas as pd
from streamlit.testing.v1 import AppTest

from tradesathi import engine
from tradesathi.storage import holdings as hd
from tradesathi.storage.store import Store

DASH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")


def test_dashboard_renders_with_data(workdir, fake_source):
    hd.save(pd.DataFrame([{"symbol": "TCS", "qty": 10, "buy_price": 900.0}]))
    engine.cycle(fake_source, Store(), 0)
    at = AppTest.from_file(DASH, default_timeout=60).run()
    assert not at.exception, at.exception
    assert any(m.label == "Invested" for m in at.metric)                # overview: portfolio summary
    assert any("TCS" in b.label for b in at.sidebar.button)             # left pane lists the holding by name


def test_dashboard_before_first_cycle(workdir):
    at = AppTest.from_file(DASH, default_timeout=60).run()
    assert not at.exception, at.exception


def test_password_gate(workdir):
    os.makedirs(".streamlit")
    with open(".streamlit/secrets.toml", "w") as f:
        f.write('app_password = "s3cret"\n')
    at = AppTest.from_file(DASH, default_timeout=60).run()
    assert not at.exception, at.exception
    assert len(at.text_input) == 1 and not at.subheader                 # locked: only the password box
    at.text_input[0].input("wrong").run()
    assert at.error
    at.text_input[0].input("s3cret").run()
    assert not at.exception and "authed" in at.session_state
