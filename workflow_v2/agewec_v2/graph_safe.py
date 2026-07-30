"""Guarded LangGraph topology with bounded autonomous loops."""
from __future__ import annotations

from typing import Callable

from langgraph.graph import END, START, StateGraph

from . import nodes_runtime as nodes
from .execution_limits import (
    escalation_router,
    execution_limit_escalation,
    guard_router,
    make_execution_guard,
)
from .review import make_review_gate, review_router
from .state import PHASES
from .state_safe import SafeWorkflowState


def _guard_name(phase: str) -> str:
    return f"guard_{phase}"


def _add_guarded_reviewed_phase(
    graph: StateGraph,
    phase: str,
    function: Callable,
    next_phase: str,
    *,
    label: str | None = None,
) -> None:
    guard_name = _guard_name(phase)
    review_name = f"review_{phase}"
    graph.add_node(guard_name, make_execution_guard(phase))
    graph.add_node(phase, function)
    graph.add_node(review_name, make_review_gate(phase, label=label))
    graph.add_conditional_edges(
        guard_name,
        guard_router,
        {
            "allow": phase,
            "escalate": "execution_limit_escalation",
            "abort": END,
        },
    )
    graph.add_edge(phase, review_name)
    graph.add_conditional_edges(
        review_name,
        review_router,
        {
            "approve": _guard_name(next_phase),
            "retry": guard_name,
            "abort": END,
        },
    )


def _visual_qa_router(state: SafeWorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route == "retry":
        return _guard_name("visual_qa")
    if review_route != "approve":
        return review_route
    route = (
        state.get("phase_results", {})
        .get("visual_qa", {})
        .get("data", {})
        .get("route", "post_production")
    )
    return _guard_name(route)


def _review_board_router(state: SafeWorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route == "retry":
        return _guard_name("review_board")
    if review_route != "approve":
        return review_route
    verdict = (
        state.get("phase_results", {})
        .get("review_board", {})
        .get("data", {})
        .get("verdict", "revise")
    )
    return (
        "review_final_submission"
        if verdict == "pass"
        else _guard_name("post_production")
    )


def build_graph(checkpointer=None):
    graph = StateGraph(SafeWorkflowState)
    graph.add_node("execution_limit_escalation", execution_limit_escalation)
    escalation_paths = {"abort": END}
    escalation_paths.update(
        {phase: _guard_name(phase) for phase in PHASES if phase != "final_submission"}
    )
    graph.add_conditional_edges(
        "execution_limit_escalation",
        escalation_router,
        escalation_paths,
    )

    _add_guarded_reviewed_phase(
        graph,
        "executive_producer",
        nodes.executive_producer,
        "creative_director",
    )
    _add_guarded_reviewed_phase(
        graph,
        "creative_director",
        nodes.creative_director,
        "writer_storyboard",
        label="人間確認1: コンセプト承認",
    )
    _add_guarded_reviewed_phase(
        graph,
        "writer_storyboard",
        nodes.writer_storyboard,
        "asset_curator",
    )
    _add_guarded_reviewed_phase(
        graph,
        "asset_curator",
        nodes.asset_curator,
        "director",
    )
    _add_guarded_reviewed_phase(
        graph,
        "director",
        nodes.director,
        "image_video_production",
        label="人間確認2: 絵コンテ・素材・演出承認",
    )
    _add_guarded_reviewed_phase(
        graph,
        "image_video_production",
        nodes.image_video_production,
        "visual_qa",
    )

    graph.add_node(
        _guard_name("visual_qa"),
        make_execution_guard("visual_qa"),
    )
    graph.add_node("visual_qa", nodes.visual_qa)
    graph.add_node("review_visual_qa", make_review_gate("visual_qa"))
    graph.add_conditional_edges(
        _guard_name("visual_qa"),
        guard_router,
        {
            "allow": "visual_qa",
            "escalate": "execution_limit_escalation",
            "abort": END,
        },
    )
    graph.add_edge("visual_qa", "review_visual_qa")
    graph.add_conditional_edges(
        "review_visual_qa",
        _visual_qa_router,
        {
            _guard_name("visual_qa"): _guard_name("visual_qa"),
            _guard_name("image_video_production"): _guard_name(
                "image_video_production"
            ),
            _guard_name("asset_curator"): _guard_name("asset_curator"),
            _guard_name("post_production"): _guard_name("post_production"),
            "abort": END,
        },
    )

    _add_guarded_reviewed_phase(
        graph,
        "post_production",
        nodes.post_production,
        "review_board",
    )

    graph.add_node(
        _guard_name("review_board"),
        make_execution_guard("review_board"),
    )
    graph.add_node("review_board", nodes.review_board)
    graph.add_node("review_review_board", make_review_gate("review_board"))
    graph.add_conditional_edges(
        _guard_name("review_board"),
        guard_router,
        {
            "allow": "review_board",
            "escalate": "execution_limit_escalation",
            "abort": END,
        },
    )
    graph.add_edge("review_board", "review_review_board")
    graph.add_conditional_edges(
        "review_review_board",
        _review_board_router,
        {
            _guard_name("review_board"): _guard_name("review_board"),
            _guard_name("post_production"): _guard_name("post_production"),
            "review_final_submission": "review_final_submission",
            "abort": END,
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
            "approve": _guard_name("provenance"),
            "retry": _guard_name("post_production"),
            "abort": END,
        },
    )

    graph.add_node(
        _guard_name("provenance"),
        make_execution_guard("provenance"),
    )
    graph.add_node("provenance", nodes.provenance)
    graph.add_node("review_provenance", make_review_gate("provenance"))
    graph.add_conditional_edges(
        _guard_name("provenance"),
        guard_router,
        {
            "allow": "provenance",
            "escalate": "execution_limit_escalation",
            "abort": END,
        },
    )
    graph.add_edge("provenance", "review_provenance")
    graph.add_conditional_edges(
        "review_provenance",
        review_router,
        {
            "approve": END,
            "retry": _guard_name("provenance"),
            "abort": END,
        },
    )
    graph.add_edge(START, _guard_name("executive_producer"))
    return graph.compile(checkpointer=checkpointer)
