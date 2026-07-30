"""Public workflow graph entry point.

The guarded topology is the canonical graph. Keeping this module as a thin
compatibility facade prevents older imports from bypassing execution limits and
the phase-aware H2 retry routes.
"""
from __future__ import annotations

from .graph_safe import build_graph

__all__ = ["build_graph"]
