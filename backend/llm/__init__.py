"""LLM access for the strategy builder: one interface, swappable providers.

Nothing in this package can reach order placement, and nothing outside
`backend.agent` should need it.
"""
