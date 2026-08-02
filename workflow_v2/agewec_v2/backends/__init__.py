"""Media backends used only by workflow_v2.

動画生成は共通契約（base.py）で抽象化し、設定でバックエンドを切り替える。

    VideoRequest → VideoBackend → VideoResult
        ├─ MockBackend    テスト用
        ├─ ComfyBackend   ローカルLTX（無料・遅い）
        └─ （今後）RunwayBackend  クラウド・複数モデル

課金を伴うバックエンドは、送信前に必ず VideoCostGuard を通す。
"""

from .base import (
    Capabilities,
    UnsupportedDurationError,
    VideoBackend,
    VideoRequest,
    VideoResult,
)
from .comfy_runtime import ComfyClient, ComfyGenerationRequest
from .cost import (
    BudgetStatus,
    VideoBudgetExceededError,
    VideoCostGuard,
    estimate_run_cost,
)
from .local_video import (
    ComfyBackend,
    MockBackend,
    resolve_backend,
    to_video_request,
)

__all__ = [
    "BudgetStatus",
    "Capabilities",
    "ComfyBackend",
    "ComfyClient",
    "ComfyGenerationRequest",
    "MockBackend",
    "UnsupportedDurationError",
    "VideoBackend",
    "VideoBudgetExceededError",
    "VideoCostGuard",
    "VideoRequest",
    "VideoResult",
    "estimate_run_cost",
    "resolve_backend",
    "to_video_request",
]
