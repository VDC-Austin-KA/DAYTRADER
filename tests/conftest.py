"""Test-wide isolation from the developer's real ``.env``.

Without this the suite inherits whatever is in ``.env`` — including
``DASHBOARD_TRADE_MODE=moomoo`` and ``MOOMOO_TRD_ENV=REAL``, which made
``test_paper_engine`` fire a live order at a real brokerage account. It only
failed because the fixture symbol was fake. Tests must never reach a broker
or a gateway, so we pin the trading/data switches to their inert values
*before* ``app.config`` is imported and its settings singleton is built.
"""
from __future__ import annotations

import os

# Must run at import time: app.config builds a module-level Settings on first
# import, and pytest imports conftest before collecting any test module.
os.environ.update(
    {
        "DASHBOARD_TRADE_MODE": "paper",
        "PREDICTION_TRADE_MODE": "paper",
        "SPREADS_TRADE_MODE": "paper",
        "MOOMOO_TRD_ENV": "SIMULATE",
        # Empty host makes moomoo_data.configured() False, so the data layer
        # stays on its Tradier/stub path and never dials the gateway.
        "MOOMOO_OPEND_HOST": "",
        "DATA_PROVIDER": "tradier",
        "TRADIER_TOKEN": "",
        "ENABLE_SCHEDULER": "false",
        "AUTO_TRAIN_ON_START": "false",
    }
)
