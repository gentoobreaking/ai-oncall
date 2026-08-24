"""Runbook YAML 解析與驗證（algs/approval-executor.md §B.1 風險分級）。

YAML 格式：

    name: rollback-api
    service: api
    description: 回滾最近一次部署
    steps:
      - name: confirm-current-revision
        action: kubectl get deployment api -o jsonpath='{.spec.template}'
        risk: read-only
      - name: rollout-undo
        action: kubectl rollout undo deployment/api
        risk: mutating
        dry_run_capable: true

驗證錯誤全部蒐集後一次回報（不 fail-fast）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from oncall_core.brain.schema_validator import RISK_LEVELS

_NAME_RE = re.compile(r"^[a-z][a-z0-9\-_]{1,63}$")


class RunbookValidationError(Exception):
    """聚合式驗證錯誤。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s): " + "; ".join(errors))


@dataclass(slots=True)
class RunbookStep:
    name: str
    action: str  # 執行模板；executor 負責渲染與實際執行
    risk: str  # "read-only" | "mutating"
    dry_run_capable: bool = True


@dataclass(slots=True)
class Runbook:
    name: str
    service: str
    description: str
    steps: list[RunbookStep] = field(default_factory=list)

    @property
    def max_risk(self) -> str:
        """整本 runbook 的風險等級＝最高步驟風險（有任一 mutating 即為 mutating）。"""
        return "mutating" if any(s.risk == "mutating" for s in self.steps) else "read-only"

    def steps_by_risk(self, risk: str) -> list[RunbookStep]:
        return [s for s in self.steps if s.risk == risk]


def parse_runbook(data: object, source: str = "") -> Runbook:
    """解析並驗證 runbook dict；所有錯誤聚合拋出。"""
    prefix = f"{source}: " if source else ""
    errors: list[str] = []

    if not isinstance(data, dict):
        raise RunbookValidationError([f"{prefix}top-level must be a mapping"])

    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append(f"{prefix}name must match {_NAME_RE.pattern}")

    service = data.get("service")
    if not isinstance(service, str) or not service.strip():
        errors.append(f"{prefix}service must be a non-empty string")

    description = data.get("description", "")
    if not isinstance(description, str):
        errors.append(f"{prefix}description must be a string")

    steps_raw = data.get("steps")
    steps: list[RunbookStep] = []
    seen_names: set[str] = set()
    if not isinstance(steps_raw, list) or len(steps_raw) == 0:
        errors.append(f"{prefix}steps must be a non-empty array")
    else:
        for i, raw in enumerate(steps_raw):
            step_errors_before = len(errors)
            if not isinstance(raw, dict):
                errors.append(f"{prefix}steps[{i}] must be a mapping")
                continue

            step_name = raw.get("name")
            if not isinstance(step_name, str) or not _NAME_RE.match(step_name):
                errors.append(f"{prefix}steps[{i}].name must match {_NAME_RE.pattern}")
            elif step_name in seen_names:
                errors.append(f"{prefix}steps[{i}].name duplicated: {step_name}")
            else:
                seen_names.add(step_name)

            action = raw.get("action")
            if not isinstance(action, str) or not action.strip():
                errors.append(f"{prefix}steps[{i}].action must be a non-empty string")

            risk = raw.get("risk")
            if risk not in RISK_LEVELS:
                errors.append(
                    f"{prefix}steps[{i}].risk must be one of {sorted(RISK_LEVELS)}, got {risk!r}"
                )

            dry_run = raw.get("dry_run_capable", True)
            if not isinstance(dry_run, bool):
                errors.append(f"{prefix}steps[{i}].dry_run_capable must be a boolean")

            if len(errors) == step_errors_before:
                steps.append(
                    RunbookStep(
                        name=str(step_name),
                        action=str(action),
                        risk=str(risk),
                        dry_run_capable=bool(dry_run),
                    )
                )

    if errors:
        raise RunbookValidationError(errors)

    assert isinstance(name, str) and isinstance(service, str) and isinstance(description, str)
    return Runbook(name=name, service=service, description=description, steps=steps)


def parse_runbook_yaml(text: str, source: str = "") -> Runbook:
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RunbookValidationError([f"{source or 'yaml'}: parse error: {exc}"]) from exc
    return parse_runbook(data, source=source)
