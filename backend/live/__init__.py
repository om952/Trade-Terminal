"""Live machinery: tick aggregation, journaling, simulated fills, session loop.

Nothing in this package makes trading decisions. The engine does that, and it is
the same engine the backtest ran. This package only changes where bars come from
(a socket instead of a Parquet file) and where orders go (a live-priced simulator
instead of a historical one).
"""
