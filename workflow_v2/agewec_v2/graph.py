"""LangGraph topology for the isolated AGEWEC v2 workflow."""
from __future__ import annotations

from typing import Callable

from langgraph.graph import END, START, StateGraph

from . import nodes_llm as nodes
from .review import make_review_gate, review_router
from .state import WorkflowState


def _add_reviewed_phase(
    graph: StateGraph,
    phase: str,
    function: Callable,
    next_node: str,
    *,
    label: str | None = None,
) -> None:
    review_name = f"review_{phase}"
    graph.add_node(phase, function)
    graph.add_node(review_name, make_review_gate(phase, label=label))
    graph.add_edge(phase, review_name)
    graph.add_conditional_edges(
        review_name,
        review_router,
        {
            "approve": next_node,
            "retry": phase,
            "abort": END,
        },
    )


def _visual_qa_router(state: WorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route != "approve":
        return review_route
    result = state.get("phase_results", {}).get("visual_qa", {})
    return result.get("data", {}).get("route", "post_production")


def _review_board_router(state: WorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route != "approve":
        return review_route
    verdict = (
        state.get("phase_results", {})
        .get("review_board", {})
        .get("data", {})
        .get("verdict", "revise")
    )
    return "pass" if verdict == "pass" else "revise"


def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    _add_reviewed_phase(
        graph,
        "executive_producer",
        nodes.executive_producer,
        "creative_director",
    )
    _add_reviewed_phase(
        graph,
        "creative_director",
        nodes.creative_director,
        "writer_storyboard",
        label="人間確認1: コンセプト承認",
    )
    _add_reviewed_phase(
        graph,
        "writer_storyboard",
        nodes.writer_storyboard,
        "asset_curator",
    )
    _add_reviewed_phase(
        graph,
        "asset_curator",
        nodes.asset_curator,
        "director",
    )
    _add_reviewed_phase(
        graph,
        "director",
        nodes.director,
        "image_video_production",
        label="人間確認2: 絵コンテ・素材・演出承認",
    )
    _add_reviewed_phase(
        graph,
        "image_video_production",
        nodes.image_video_production,
        "visual_qa",
    )

    graph.add_node("visual_qa", nodes.visual_qa)
    graph.add_node("review_visual_qa", make_review_gate("visual_qa"))
    graph.add_edge("visual_qa", "review_visual_qa")
    graph.add_conditional_edges(
        "review_visual_qa",
        _visual_qa_router,
        {
            "retry": "visual_qa",
            "abort": END,
            "image_video_production": "image_video_production",
            "asset_curator": "asset_curator",
            "post_production": "post_production",
        },
    )

    _add_reviewed_phase(
        graph,
        "post_production",
        nodes.post_production,
        "review_board",
    )

    graph.add_node("review_board", nodes.review_board)
    graph.add_node("review_review_board", make_review_gate("review_board"))
    graph.add_edge("review_board", "review_review_board")
    graph.add_conditional_edges(
        "review_review_board",
        _review_board_router,
        {
            "retry": "review_board",
            "abort": END,
            "revise": "post_production",
            "pass": "review_final_submission",
        },
    )

    graph.add_node(
        "review_final_submission",
        make_review_gate(
            "final_submission",
            source_phase="review_board",
            label="人間確認3: 最終提出承認",
        ),
    )
    graph.add_conditional_edges(
        "review_final_submission",
        review_router,
        {
            "approve": "provenance",
            "retry": "post_production",
            "abort": END,
        },
    )

    _add_reviewed_phase(
        graph,
        "provenance",
        nodes.provenance,
        END,
    )
    graph.add_edge(START, "executive_producer")
    return graph.compile(checkpointer=checkpointer)
