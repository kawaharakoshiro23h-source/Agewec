"""Pydantic output contracts for every LLM-controlled phase."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ProjectBrief(BaseModel):
    objective: str
    target_award: str
    audience: str
    deliverable: str
    constraints: list[str] = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)


class VisualLanguage(BaseModel):
    palette: list[str] = Field(min_length=1)
    camera: str
    continuity_rule: str


class CreativeConcept(BaseModel):
    title: str
    logline: str
    tone: list[str] = Field(min_length=1)
    visual_language: VisualLanguage
    audio_direction: str
    success_criteria: list[str] = Field(min_length=1)


class StoryboardCut(BaseModel):
    id: int = Field(gt=0)
    name: str
    scene: str
    narration: str
    seconds: float = Field(gt=0)
    media_strategy: Literal["still", "video", "generated_image"]


class Storyboard(BaseModel):
    total_seconds: float = Field(gt=0)
    cuts: list[StoryboardCut] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timeline(self) -> "Storyboard":
        ids = [cut.id for cut in self.cuts]
        if len(ids) != len(set(ids)):
            raise ValueError("cut ids must be unique")
        actual = sum(cut.seconds for cut in self.cuts)
        if abs(actual - self.total_seconds) > 0.25:
            raise ValueError(
                f"total_seconds={self.total_seconds} does not match cuts={actual}"
            )
        return self


class AssetSelectionItem(BaseModel):
    cut_id: int = Field(gt=0)
    asset_id: str
    reason: str
    rights_risk: Literal["low", "medium", "high", "unknown"]


class AssetSelection(BaseModel):
    selections: list[AssetSelectionItem]
    missing_requirements: list[str] = Field(default_factory=list)


class DirectionShot(BaseModel):
    cut_id: int = Field(gt=0)
    asset_id: str
    positive_prompt: str
    negative_prompt: str = ""
    camera_motion: str
    generation_profile: str


class DirectionPlan(BaseModel):
    shots: list[DirectionShot] = Field(min_length=1)
    continuity_checks: list[str] = Field(min_length=1)


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
    "director": DirectionPlan,
    "visual_qa": VisualQAResult,
    "post_production": EditPlan,
    "review_board": ReviewBoardResult,
}
