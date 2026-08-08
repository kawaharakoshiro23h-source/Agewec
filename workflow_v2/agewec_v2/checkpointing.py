"""Disk-backed checkpoint helpers for the AGEWEC CLI."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


class RunNotFoundError(ValueError):
    """Raised when a requested run_id has no persisted checkpoint."""


class UnsafeLegacyContinuationError(ValueError):
    """Raised when a legacy run would continue with stale absolute paths."""


def checkpoint_contains_run(path: Path, run_id: str) -> bool:
    """Return whether a SQLite checkpoint contains ``run_id`` without writing."""
    if not path.is_file():
        return False
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def checkpoint_db_for_resume(
    run_id: str,
    preferred: Path,
    legacy: Path,
) -> Path:
    """Select the new checkpoint DB, falling back to the legacy DB if needed."""
    if checkpoint_contains_run(preferred, run_id):
        return preferred
    if preferred != legacy and checkpoint_contains_run(legacy, run_id):
        return legacy
    return preferred


@contextmanager
def open_sqlite_checkpointer(path: Path) -> Iterator[SqliteSaver]:
    """Open and initialize a SQLite checkpointer at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as checkpointer:
        checkpointer.setup()
        yield checkpointer


def load_persisted_result(
    graph: Any,
    thread: dict[str, dict[str, str]],
    run_id: str,
    *,
    allow_continuation: bool = True,
) -> dict[str, Any]:
    """Load a completed/interrupted run, or continue from its last safe boundary."""
    snapshot = graph.get_state(thread)
    if snapshot.created_at is None:
        raise RunNotFoundError(
            f"保存された実行状態が見つかりません: {run_id}"
        )

    if not allow_continuation and (snapshot.interrupts or snapshot.next):
        raise UnsafeLegacyContinuationError(
            "旧checkpointの未完了runは、保存stateに移行前の絶対パスが"
            "含まれる可能性があるため継続できません。完了済みrunの閲覧は"
            "可能です。継続する場合は専用のstate移行が必要です: "
            f"{run_id}"
        )

    result = dict(snapshot.values)
    if snapshot.interrupts:
        # 承認待ちの場合はノードを先に再実行せず、保存済みの質問を再表示する。
        result["__interrupt__"] = list(snapshot.interrupts)
        return result

    if snapshot.next:
        # ノード境界で停止した場合は、最後に完了したチェックポイントの次から進む。
        return graph.invoke(None, thread)

    # 既に完了・中止済みなら外部API等を再実行せず、最終状態を表示する。
    return result
