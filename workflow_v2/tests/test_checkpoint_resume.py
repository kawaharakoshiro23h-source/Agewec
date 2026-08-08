from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agewec_v2.checkpointing import (
    RunNotFoundError,
    UnsafeLegacyContinuationError,
    checkpoint_contains_run,
    checkpoint_db_for_resume,
    load_persisted_result,
    open_sqlite_checkpointer,
)


class _ApprovalState(TypedDict):
    value: int


def _approval_graph(checkpointer):
    def ask(state: _ApprovalState) -> dict[str, int]:
        answer = interrupt({"label": "確認", "value": state["value"]})
        return {"value": int(answer)}

    builder = StateGraph(_ApprovalState)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=checkpointer)


class CheckpointResumeTest(unittest.TestCase):
    def test_incomplete_legacy_run_cannot_continue_with_stale_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite"
            thread = {"configurable": {"thread_id": "run-legacy-pending"}}
            with open_sqlite_checkpointer(database) as checkpointer:
                _approval_graph(checkpointer).invoke({"value": 1}, thread)

            with open_sqlite_checkpointer(database) as checkpointer:
                with self.assertRaisesRegex(
                    UnsafeLegacyContinuationError,
                    "専用のstate移行",
                ):
                    load_persisted_result(
                        _approval_graph(checkpointer),
                        thread,
                        "run-legacy-pending",
                        allow_continuation=False,
                    )

    def test_resume_database_prefers_new_location_when_run_exists_there(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferred = root / "runtime/checkpoints.sqlite"
            legacy = root / "workflow_v2/work/checkpoints.sqlite"
            thread = {"configurable": {"thread_id": "run-new"}}
            with open_sqlite_checkpointer(preferred) as checkpointer:
                _approval_graph(checkpointer).invoke({"value": 1}, thread)

            self.assertTrue(checkpoint_contains_run(preferred, "run-new"))
            self.assertEqual(
                checkpoint_db_for_resume("run-new", preferred, legacy),
                preferred,
            )

    def test_resume_database_falls_back_to_legacy_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferred = root / "runtime/checkpoints.sqlite"
            legacy = root / "workflow_v2/work/checkpoints.sqlite"
            thread = {"configurable": {"thread_id": "run-legacy"}}
            with open_sqlite_checkpointer(legacy) as checkpointer:
                _approval_graph(checkpointer).invoke({"value": 1}, thread)

            self.assertFalse(checkpoint_contains_run(preferred, "run-legacy"))
            self.assertTrue(checkpoint_contains_run(legacy, "run-legacy"))
            self.assertEqual(
                checkpoint_db_for_resume("run-legacy", preferred, legacy),
                legacy,
            )

    def test_unknown_run_keeps_preferred_database_for_normal_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferred = root / "runtime/checkpoints.sqlite"
            legacy = root / "workflow_v2/work/checkpoints.sqlite"
            self.assertEqual(
                checkpoint_db_for_resume("run-missing", preferred, legacy),
                preferred,
            )

    def test_interrupted_run_resumes_after_reopening_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checkpoints.sqlite"
            thread = {"configurable": {"thread_id": "run-resume-test"}}

            with open_sqlite_checkpointer(database) as checkpointer:
                graph = _approval_graph(checkpointer)
                first = graph.invoke({"value": 1}, thread)
                self.assertIn("__interrupt__", first)

            with open_sqlite_checkpointer(database) as checkpointer:
                graph = _approval_graph(checkpointer)
                restored = load_persisted_result(
                    graph,
                    thread,
                    "run-resume-test",
                )
                self.assertEqual(
                    restored["__interrupt__"][0].value["label"],
                    "確認",
                )
                completed = graph.invoke(Command(resume=7), thread)

            self.assertEqual(completed["value"], 7)

    def test_completed_run_is_loaded_without_reexecution(self) -> None:
        calls = 0

        class State(TypedDict):
            value: int

        def build(checkpointer):
            def increment(state: State) -> dict[str, int]:
                nonlocal calls
                calls += 1
                return {"value": state["value"] + 1}

            builder = StateGraph(State)
            builder.add_node("increment", increment)
            builder.add_edge(START, "increment")
            builder.add_edge("increment", END)
            return builder.compile(checkpointer=checkpointer)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checkpoints.sqlite"
            thread = {"configurable": {"thread_id": "run-complete-test"}}
            with open_sqlite_checkpointer(database) as checkpointer:
                completed = build(checkpointer).invoke({"value": 1}, thread)
                self.assertEqual(completed["value"], 2)

            with open_sqlite_checkpointer(database) as checkpointer:
                restored = load_persisted_result(
                    build(checkpointer),
                    thread,
                    "run-complete-test",
                )

        self.assertEqual(restored["value"], 2)
        self.assertEqual(calls, 1)

    def test_unknown_run_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checkpoints.sqlite"
            thread = {"configurable": {"thread_id": "run-missing"}}
            with open_sqlite_checkpointer(database) as checkpointer:
                graph = _approval_graph(checkpointer)
                with self.assertRaisesRegex(
                    RunNotFoundError,
                    "run-missing",
                ):
                    load_persisted_result(graph, thread, "run-missing")


if __name__ == "__main__":
    unittest.main()
