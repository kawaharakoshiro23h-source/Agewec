"""Guarded LangGraph topology with bounded per-cut production loops.

【本番経路: 正】これが実際に実行されるグラフ定義。

    run.py → graph_safe.build_graph() → nodes_runtime → pipeline_runtime
                                                      → nodes_llm → nodes

ノードの実体は `nodes_runtime` から取り込む（`from . import nodes_runtime as nodes`）。
各フェーズの後段に Review Gate、前段に Execution Guard を挿入する。
"""
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
from .timing import with_phase_timing


def _guard_name(phase: str) -> str:
    return f"guard_{phase}"


def _add_guard(
    graph: StateGraph,
    phase: str,
    function: Callable,
) -> None:
    graph.add_node(_guard_name(phase), make_execution_guard(phase))
    graph.add_node(phase, with_phase_timing(phase, function))
    graph.add_conditional_edges(
        _guard_name(phase),
        guard_router,
        {
            "allow": phase,
            "escalate": "execution_limit_escalation",
            "abort": END,
        },
    )


def _add_guarded_reviewed_phase(
    graph: StateGraph,
    phase: str,
    function: Callable,
    next_phase: str,
    *,
    label: str | None = None,
) -> None:
    _add_guard(graph, phase, function)
    review_name = f"review_{phase}"
    graph.add_node(
        review_name,
        make_review_gate(phase, label=label),
    )
    graph.add_edge(phase, review_name)
    graph.add_conditional_edges(
        review_name,
        review_router,
        {
            "approve": _guard_name(next_phase),
            "retry": _guard_name(phase),
            "abort": END,
        },
    )


def _support_review_router(state: SafeWorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route == "approve":
        return _guard_name("image_video_production")
    if review_route != "retry":
        return review_route
    target = state.get("review_target_phase", "support_video_creator")
    if target not in {
        "creative_director",
        "writer_storyboard",
        "asset_curator",
        "director",
        "support_video_creator",
    }:
        target = "support_video_creator"
    return _guard_name(target)


def _cut_review_router(state: SafeWorkflowState) -> str:
    route = state.get("review_route", "abort")
    if route == "approve":
        return "commit_cut_qa"
    if route == "retry":
        return _guard_name("cut_visual_qa")
    return "abort"


def _cut_commit_router(state: SafeWorkflowState) -> str:
    route = state.get("cut_qa_route", "human_review")
    return {
        "next_cut": _guard_name("image_video_production"),
        "sequence_qa": _guard_name("visual_qa"),
        "image_video_production": _guard_name("image_video_production"),
        "support_video_creator": _guard_name("support_video_creator"),
        "director": _guard_name("director"),
        "asset_curator": _guard_name("asset_curator"),
    }.get(route, "abort")


def _sequence_qa_router(state: SafeWorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route == "retry":
        return _guard_name("visual_qa")
    if review_route != "approve":
        return "abort"
    data = (
        state.get("phase_results", {})
        .get("visual_qa", {})
        .get("data", {})
    )
    if data.get("verdict") == "pass":
        return _guard_name("post_production")
    route = data.get("recommended_route", "image_video_production")
    return _guard_name(route)


def _review_board_mode_router(state: SafeWorkflowState) -> str:
    result = state.get("phase_results", {}).get("review_board", {})
    if result.get("status") == "skipped":
        return "review_final_submission"
    return "review_review_board"


def _review_board_router(state: SafeWorkflowState) -> str:
    review_route = state.get("review_route", "abort")
    if review_route == "retry":
        return _guard_name("review_board")
    if review_route != "approve":
        return "abort"
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
    graph.add_node(
        "execution_limit_escalation",
        execution_limit_escalation,
    )
    escalation_paths = {"abort": END}
    escalation_paths.update(
        {
            phase: _guard_name(phase)
            for phase in PHASES
            if phase != "final_submission"
        }
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
        "support_video_creator",
        label="Phase 05 Director Review",
    )

    _add_guard(graph, "support_video_creator", nodes.support_video_creator)
    graph.add_node(
        "review_support_video_creator",
        make_review_gate(
            "support_video_creator",
            label="人間確認2: 絵コンテ・素材・演出・生成条件承認",
        ),
    )
    graph.add_edge(
        "support_video_creator",
        "review_support_video_creator",
    )
    graph.add_conditional_edges(
        "review_support_video_creator",
        _support_review_router,
        {
            _guard_name("creative_director"): _guard_name(
                "creative_director"
            ),
            _guard_name("writer_storyboard"): _guard_name(
                "writer_storyboard"
            ),
            _guard_name("asset_curator"): _guard_name("asset_curator"),
            _guard_name("director"): _guard_name("director"),
            _guard_name("support_video_creator"): _guard_name(
                "support_video_creator"
            ),
            _guard_name("image_video_production"): _guard_name(
                "image_video_production"
            ),
            "abort": END,
        },
    )

    _add_guard(
        graph,
        "image_video_production",
        nodes.image_video_production,
    )
    _add_guard(graph, "cut_visual_qa", nodes.cut_visual_qa)
    graph.add_edge(
        "image_video_production",
        _guard_name("cut_visual_qa"),
    )
    graph.add_node(
        "review_cut_visual_qa",
        make_review_gate(
            "cut_visual_qa",
            label="Phase 07A: Cut Visual QA",
        ),
    )
    graph.add_edge("cut_visual_qa", "review_cut_visual_qa")
    graph.add_node(
        "commit_cut_qa",
        with_phase_timing("commit_cut_qa", nodes.commit_cut_qa),
    )
    graph.add_conditional_edges(
        "review_cut_visual_qa",
        _cut_review_router,
        {
            "commit_cut_qa": "commit_cut_qa",
            _guard_name("cut_visual_qa"): _guard_name("cut_visual_qa"),
            "abort": END,
        },
    )
    graph.add_conditional_edges(
        "commit_cut_qa",
        _cut_commit_router,
        {
            _guard_name("image_video_production"): _guard_name(
                "image_video_production"
            ),
            _guard_name("support_video_creator"): _guard_name(
                "support_video_creator"
            ),
            _guard_name("director"): _guard_name("director"),
            _guard_name("asset_curator"): _guard_name("asset_curator"),
            _guard_name("visual_qa"): _guard_name("visual_qa"),
            "abort": END,
        },
    )

    _add_guard(graph, "visual_qa", nodes.visual_qa)
    graph.add_node(
        "review_visual_qa",
        make_review_gate(
            "visual_qa",
            label="Phase 07B: Sequence Readiness QA",
        ),
    )
    graph.add_edge("visual_qa", "review_visual_qa")
    graph.add_conditional_edges(
        "review_visual_qa",
        _sequence_qa_router,
        {
            _guard_name("visual_qa"): _guard_name("visual_qa"),
            _guard_name("image_video_production"): _guard_name(
                "image_video_production"
            ),
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
    _add_guard(graph, "review_board", nodes.review_board)
    graph.add_node(
        "review_review_board",
        make_review_gate("review_board"),
    )
    graph.add_conditional_edges(
        "review_board",
        _review_board_mode_router,
        {
            "review_review_board": "review_review_board",
            "review_final_submission": "review_final_submission",
        },
    )
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

    _add_guard(graph, "provenance", nodes.provenance)
    graph.add_node(
        "review_provenance",
        make_review_gate("provenance"),
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
