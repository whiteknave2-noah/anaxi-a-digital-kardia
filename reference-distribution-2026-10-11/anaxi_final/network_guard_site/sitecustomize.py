"""Installs anaxi_final/network_guard in subprocesses that opt in via PYTHONPATH."""
import os
import sys

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _here not in sys.path:
    sys.path.insert(0, _here)
try:
    import network_guard
    network_guard.install()
    import runtime_isolation
    runtime_isolation.redirect_signal_observation_log()
except Exception:  # never break interpreter start-up
    pass
