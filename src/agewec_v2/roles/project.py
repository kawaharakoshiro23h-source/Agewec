"""Executive Producer and Creative Director roles."""
from __future__ import annotations

from typing import Any

from ..fallbacks import planning as deterministic
from ..state import WorkflowState

from .common import _approved_project_brief, _run_role

def executive_producer(state: WorkflowState) -> dict[str, Any]:
    project = state.get("project", {})

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        requested_duration = float(
            project.get("target_duration_seconds", 30)
        )
        if abs(float(data["target_duration_seconds"]) - requested_duration) > 0.01:
            raise ValueError(
                "Executive Producer must preserve "
                f"target_duration_seconds={requested_duration}"
            )
        requested_award = str(project.get("target_award", ""))
        if requested_award and data["target_award"] != requested_award:
            raise ValueError(
                f"Executive Producer must preserve target_award={requested_award}"
            )
        return {
            **data,
            "target_duration_seconds": requested_duration,
            "source_project": project,
        }

    return _run_role(
        state,
        phase="executive_producer",
        upstream={
            "project": project,
            "system_capabilities": {
                "orchestrator": "LangGraph",
                "media_backend": state.get("config", {})
                .get("production", {})
                .get("backend", "mock"),
                "review_modes": ["always", "on_exception", "never"],
            },
        },
        summary=lambda data: f"{data['deliverable']}の制作方針をLLMが定義",
        fallback=deterministic.executive_producer,
        transform=transform,
    )


def creative_director(state: WorkflowState) -> dict[str, Any]:
    brief = _approved_project_brief(state)
    if not brief:
        return deterministic._complete(
            state,
            "creative_director",
            summary="上流ProjectBriefがないため実行不可",
            data={},
            status="error",
            confidence=0.0,
            blocking_issues=["executive_producerの有効な出力が必要"],
        )

    def transform(data: dict[str, Any]) -> dict[str, Any]:
        # ProjectBriefの成功基準は、LLMによる転記や言い換えに依存させない。
        # 上流基準を先頭に固定し、Creative Director独自の追加基準だけを
        # 後ろへ残すことで、契約を確実に継承しつつ創造的な追加を許可する。
        inherited = [
            str(item).strip()
            for item in brief.get("success_criteria", [])
            if str(item).strip()
        ]
        proposed = [
            str(item).strip()
            for item in data.get("success_criteria", [])
            if str(item).strip()
        ]
        merged = list(dict.fromkeys([*inherited, *proposed]))
        return {
            **data,
            "success_criteria": merged,
            "inherited_success_criteria": inherited,
        }

    return _run_role(
        state,
        phase="creative_director",
        upstream={
            "project_brief": brief,
        },
        summary=lambda data: f"コンセプト「{data['title']}」をLLMが策定",
        fallback=deterministic.creative_director,
        transform=transform,
    )
