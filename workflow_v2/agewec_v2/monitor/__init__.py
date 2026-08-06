"""実行中のワークフローをブラウザから観測するための読み取り専用モニタ。

パイプライン（`agewec_v2.run` / `graph_safe` / `pipeline_runtime`）には
一切変更を加えない。`work/runs/` に書き出された成果物を外から読むだけなので、
起動していてもいなくても本番の挙動は同じになる。

    ターミナル                        ブラウザ
      承認・差し戻しの入力     ←→      いま何が起きているかを見る

将来ブラウザから承認できるようにする場合は、`server.py` に POST を足し、
CLI 側が決定ファイルも待つようにする。その際も `reader.py` と画面は
そのまま流用できるよう、JSON APIを境界にしてある。
"""

from .reader import list_runs, read_run

__all__ = ["list_runs", "read_run"]
