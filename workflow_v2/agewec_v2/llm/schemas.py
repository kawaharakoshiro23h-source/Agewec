"""Pydantic output contracts for every LLM-controlled phase."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ProjectBrief(BaseModel):
    objective: str
    target_award: str
    target_duration_seconds: float = Field(gt=0)
    audience: str
    deliverable: str
    constraints: list[str] = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)


class VisualLanguage(BaseModel):
    palette: list[str] = Field(min_length=1)
    continuity_rule: str


class CameraIntent(BaseModel):
    viewer_experience: str
    energy_curve: str
    stability: str
    continuity: str
    hard_constraints: list[str] = Field(min_length=1)


class CreativeConcept(BaseModel):
    title: str
    logline: str
    tone: list[str] = Field(min_length=1)
    visual_language: VisualLanguage
    camera_intent: CameraIntent
    audio_direction: str
    success_criteria: list[str] = Field(min_length=1)


class StoryboardCut(BaseModel):
    id: int = Field(gt=0)
    name: str
    scene: str
    narration: str
    seconds: float = Field(gt=0)
    media_requirement: Literal[
        "video_required",
        "still_allowed",
        "still_preferred",
    ]
    time_of_day: str
    visual_role: str
    location: str
    subject: str


class Storyboard(BaseModel):
    total_seconds: float = Field(gt=0)
    cuts: list[StoryboardCut] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timeline(self) -> "Storyboard":
        ids = [cut.id for cut in self.cuts]
        if len(ids) != len(set(ids)):
            raise ValueError("cut ids must be unique")
        # 尺合計と目標尺の整合は、プロジェクト設定を参照できる
        # writer_storyboardノード側で検証・小差補正する。Schema段階で
        # 拒否すると、1秒程度の単純な足し算誤差もLLM再試行になり、
        # 修復応答が壊れたJSONになるリスクが高いため。
        return self


class AssetChoice(BaseModel):
    asset_id: str
    reason: str


class AssetSelectionItem(BaseModel):
    cut_id: int = Field(gt=0)
    primary: AssetChoice
    alternatives: list[AssetChoice] = Field(default_factory=list)


class AssetSelection(BaseModel):
    selections: list[AssetSelectionItem] = Field(min_length=1)
    missing_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_assignments(self) -> "AssetSelection":
        cut_ids = [item.cut_id for item in self.selections]
        if len(cut_ids) != len(set(cut_ids)):
            raise ValueError("asset selections must have unique cut ids")
        for item in self.selections:
            ids = [
                item.primary.asset_id,
                *(choice.asset_id for choice in item.alternatives),
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(
                    f"cut {item.cut_id} contains duplicate asset ids"
                )
        return self


class AssetRationaleItem(BaseModel):
    cut_id: int = Field(gt=0)
    reason: str = Field(min_length=1)


class AssetRationale(BaseModel):
    rationales: list[AssetRationaleItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cut_ids(self) -> "AssetRationale":
        cut_ids = [item.cut_id for item in self.rationales]
        if len(cut_ids) != len(set(cut_ids)):
            raise ValueError("asset rationales must have unique cut ids")
        return self


class DirectionShot(BaseModel):
    cut_id: int = Field(gt=0)
    asset_id: str
    positive_prompt: str
    negative_prompt: str = ""
    camera_motion: str
    motion_intensity: Literal["subtle", "moderate", "strong"]
    rationale: str
    camera_intent_alignment: str
    deviation_reason: str | None = None


class DirectionPlan(BaseModel):
    shots: list[DirectionShot] = Field(min_length=1)
    continuity_checks: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shots(self) -> "DirectionPlan":
        cut_ids = [shot.cut_id for shot in self.shots]
        if len(cut_ids) != len(set(cut_ids)):
            raise ValueError("direction shots must have unique cut ids")
        return self


class VisualQACutResult(BaseModel):
    cut_id: int = Field(gt=0)
    verdict: Literal["pass", "revise", "replace_asset"]
    issues: list[str] = Field(default_factory=list)


class VisualQAResult(BaseModel):
    verdict: Literal["pass", "revise", "replace_asset"]
    route: Literal[
        "image_video_production",
        "asset_curator",
        "post_production",
    ]
    issues: list[str] = Field(default_factory=list)
    cut_results: list[VisualQACutResult] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class EditOperation(BaseModel):
    order: int = Field(gt=0)
    operation: str
    cut_id: int | None = None
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class EditPlan(BaseModel):
    operations: list[EditOperation] = Field(min_length=1)
    narration_direction: str
    bgm_direction: str
    subtitle_direction: str
    final_duration_seconds: float = Field(gt=0)


class ReviewBoardResult(BaseModel):
    rubric_scores: dict[str, float] = Field(min_length=1)
    average: float = Field(ge=0, le=5)
    verdict: Literal["pass", "revise"]
    recommendations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_scores(self) -> "ReviewBoardResult":
        if any(score < 0 or score > 5 for score in self.rubric_scores.values()):
            raise ValueError("rubric scores must be between 0 and 5")
        if self.rubric_scores:
            actual = sum(self.rubric_scores.values()) / len(self.rubric_scores)
            if abs(actual - self.average) > 0.15:
                raise ValueError(
                    f"average={self.average} does not match scores={actual}"
                )
        return self


ROLE_SCHEMAS: dict[str, type[BaseModel]] = {
    "executive_producer": ProjectBrief,
    "creative_director": CreativeConcept,
    "writer_storyboard": Storyboard,
    "asset_curator": AssetSelection,
    "asset_curator_rationale": AssetRationale,
    "director": DirectionPlan,
    "visual_qa": VisualQAResult,
    "post_production": EditPlan,
    "review_board": ReviewBoardResult,
}
