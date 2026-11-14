"""The strategy builder agent: plain English in, an approved, backtested strategy out.

Structural rule, enforced by test (backend/tests/test_agent.py): nothing in
this package may import code that can place orders (`backend.live`). The model
produces data; the graph's code decides every action, and the only actions
available are validating, saving a strategy version and running a backtest.
"""
