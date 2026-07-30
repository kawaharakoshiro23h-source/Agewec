"""グラフ全体で持ち回す状態の定義。

LangGraph のノードは State(dict) を受け取り、更新差分(dict)を返す。
Cut は各カットの構造化データ。実装段階で項目を足していく。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict


@dataclass
class Cut:
    """絵コンテ1カット分。"""

    id: int
    scene_desc: str = ""
    image_prompt: str = ""
    motion_prompt: str = ""
    narration: str = ""
    seconds: float = 5.0
    # 素材の出所: "generate"(AI生成) / "official_photo"(公式写真) / "official_video"(公式動画)
    source: str = "generate"
    asset_title: str = ""      # 採用した公式素材のタイトル
    asset_url: str = ""        # 採用した公式素材のURL
    image_path: str | None = None
    video_path: str | None = None
    qa_ok: bool = False
    qa_reason: str = ""
    retries: int = 0


class AgentState(TypedDict, total=False):
    """パイプライン全体の状態。

    total=False にして、各ノードが必要なキーだけ更新できるようにする。
    """

    target_award: str          # "夜景賞" / "観光賞" / "DX賞" / "環境賞"
    theme: str                 # 一言テーマ
    storyboard: list[Cut]      # 各カット
    audio: dict[str, str]      # {"narration_path": ..., "bgm_path": ...}
    final_video: str           # 完成 mp4 のパス
    log: list[dict[str, Any]]  # 全ステップの証跡
    config: dict[str, Any]     # backend 切替・retry上限など
