"""LangGraph のグラフ定義。

planner → asset_planner → image_gen → qa(⇄image_gen) → video_gen
       → audio → assembly → provenance → END

qa は条件エッジで image_gen に戻る（QAリトライループ）。
qa / assembly は interrupt によるチェックポイント。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import AgentState


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)

    g.add_node("planner", nodes.planner)
    g.add_node("asset", nodes.asset)
    g.add_node("image_gen", nodes.image_gen)
    g.add_node("qa", nodes.qa)
    g.add_node("video_gen", nodes.video_gen)
    g.add_node("audio", nodes.audio)
    g.add_node("assembly", nodes.assembly)
    g.add_node("provenance", nodes.provenance)

    g.add_edge(START, "planner")
    g.add_edge("planner", "asset")
    g.add_edge("asset", "image_gen")
    g.add_edge("image_gen", "qa")
    g.add_conditional_edges(
        "qa", nodes.qa_router,
        {"retry": "image_gen", "continue": "video_gen"},
    )
    g.add_edge("video_gen", "audio")
    g.add_edge("audio", "assembly")
    g.add_edge("assembly", "provenance")
    g.add_edge("provenance", END)

    return g.compile(checkpointer=checkpointer)
