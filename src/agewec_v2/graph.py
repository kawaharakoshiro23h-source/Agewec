"""Public workflow graph entry point.

【互換用ファサード】実体は graph_safe.py（本番経路: 正）。

このモジュールは古いimportが実行上限やH2差し戻し経路を迂回しないための薄い
互換層。新しいコードは `graph_safe.build_graph` を直接使うこと。
"""
from __future__ import annotations

from .graph_safe import build_graph

__all__ = ["build_graph"]
