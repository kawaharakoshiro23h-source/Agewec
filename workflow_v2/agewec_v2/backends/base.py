"""動画生成バックエンドの共通契約。

【本番経路: 現役】LangGraph 側は「この画像とPromptで N 秒」だけを渡し、
フレーム数・クレジット・ジョブIDなどサービス固有の事情は各Adapterが吸収する。

    LangGraph → VideoRequest → VideoBackend → VideoResult
                                  ├─ MockBackend    （テスト用）
                                  ├─ ComfyBackend   （ローカルLTX）
                                  └─ RunwayBackend  （クラウド・複数モデル）

尺や解像度の許容値は **モデル単位** で異なるため、プロバイダ単位ではなく
`capabilities(model)` で表現する（veo3.1_fast と gen4.5 と seedance2 は別物）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class UnsupportedDurationError(ValueError):
    """モデルが受け付けられない尺を要求された（短く切り詰めずに失敗させる）。"""


@dataclass(frozen=True)
class VideoRequest:
    """LangGraph からバックエンドへの共通の注文書。

    フレーム数ではなく **秒** で表現する。8n+1 のようなモデル固有の制約は
    各 Adapter が内部で変換する。
    """

    cut_id: int
    image_path: str
    positive_prompt: str
    negative_prompt: str = ""
    seconds: float = 5.0
    width: int = 576
    height: int = 384
    fps: int = 24
    seed: int | None = None
    attempt: int = 1
    # モデル固有の追加指定（steps など）。共通契約を汚さないための逃げ道。
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoResult:
    """バックエンドからの返答。課金・追跡に必要な情報を必ず含む。

    `requested_seconds` と `billed_seconds` を分けるのは、多くのAPIが
    許容尺へ切り上げて課金するため（6秒要求→8秒課金、など）。
    """

    output_path: str
    provider: str
    model: str
    requested_seconds: float
    billed_seconds: float
    actual_seconds: float | None = None
    job_id: str | None = None
    elapsed_seconds: float = 0.0
    cost_usd: float = 0.0
    has_native_audio: bool = False
    settings: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """provenance へ残す形。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "job_id": self.job_id,
            "requested_seconds": self.requested_seconds,
            "billed_seconds": self.billed_seconds,
            "actual_seconds": self.actual_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "cost_usd": round(self.cost_usd, 4),
            "has_native_audio": self.has_native_audio,
            "settings": self.settings,
        }


@dataclass(frozen=True)
class Capabilities:
    """モデルごとの制約。比較・尺合わせ・音声処理の判断に使う。

    allowed_seconds が空なら任意長（ローカルLTXのように連続値を扱える）。
    """

    model: str
    allowed_seconds: tuple[float, ...] = ()
    max_seconds: float = 0.0
    resolutions: tuple[str, ...] = ()
    supports_seed: bool = True
    supports_negative_prompt: bool = True
    has_native_audio: bool = False
    cost_per_second_usd: float = 0.0
    # Seedance 2 Miniなど、1生成ごとの最低課金があるモデル向け。
    minimum_cost_usd: float = 0.0

    def resolve_seconds(self, requested: float) -> float:
        """要求尺を、このモデルが受け付ける尺へ切り上げる。

        **決して短くしない。** 短い動画を返すとPhase 08で尺不足エラーになり、
        原因が分かりにくい形で失敗するため、生成前にここで弾く。
        長い分はPhase 08でトリムする。

        Raises:
            UnsupportedDurationError: モデルの上限を超える尺を要求された場合。
                呼び出し側はカットを分割するか、別モデルを選ぶ必要がある。
        """
        limit = self.max_seconds or (
            max(self.allowed_seconds) if self.allowed_seconds else 0.0
        )
        if limit and requested > limit + 1e-6:
            raise UnsupportedDurationError(
                f"{self.model}: {requested}秒は上限{limit}秒を超えます。"
                "カットを分割するか、長尺対応モデルを選んでください。"
            )
        if not self.allowed_seconds:
            return requested
        for value in sorted(self.allowed_seconds):
            if value >= requested - 1e-6:
                return value
        raise UnsupportedDurationError(
            f"{self.model}: {requested}秒を満たす許容尺がありません "
            f"（許容: {self.allowed_seconds}）"
        )

    def estimate_cost(self, requested: float) -> float:
        """課金対象秒と最低課金の大きい方で見積もる。"""
        duration_cost = self.resolve_seconds(requested) * self.cost_per_second_usd
        return max(duration_cost, self.minimum_cost_usd)


class VideoBackend(Protocol):
    """すべての動画生成バックエンドが満たす契約。"""

    provider: str

    def capabilities(self, model: str | None = None) -> Capabilities: ...

    def generate(self, request: VideoRequest) -> VideoResult: ...
