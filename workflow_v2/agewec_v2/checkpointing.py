"""Disk-backed checkpoint helpers for the AGEWEC CLI."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


class RunNotFoundError(ValueError):
    """Raised when a requested run_id has no persisted checkpoint."""


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
) -> dict[str, Any]:
    """Load a completed/interrupted run, or continue from its last safe boundary."""
    snapshot = graph.get_state(thread)
    if snapshot.created_at is None:
        raise RunNotFoundError(
            f"保存された実行状態が見つかりません: {run_id}"
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
