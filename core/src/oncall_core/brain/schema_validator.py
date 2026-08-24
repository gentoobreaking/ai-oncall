"""TriageReport JSON schema 驗證與修復迴圈（algs/schema-validation.md §C.1/C.2）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# §C.1 契約常數
RISK_LEVELS: frozenset[str] = frozenset({"read-only", "mutating"})
MAX_HYPOTHESES = 5
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ExecutorRejected(Exception):
    """executor 對未通過驗證輸入的硬拒絕（§C.2）——即使已獲人類批准。"""


@dataclass(slots=True)
class Hypothesis:
    cause: str
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SuggestedAction:
    action: str
    risk: str  # "read-only" | "mutating"
    runbook_ref: str | None = None


@dataclass(slots=True)
class TriageReport:
    """僅能由 validate_report() 成功後建構——validated 恆為 True，
    executor 以此型別為唯一接受的輸入（硬拒絕其餘）。"""

    incident_id: str
    hypotheses: list[Hypothesis]
    suggested_actions: list[SuggestedAction]
    missing_context: list[str]
    prompt_version: str
    validated: bool = True


def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def extract_json_object(raw: str) -> dict[str, object]:
    """從 LLM 原始輸出抽取 JSON 物件；容許前後雜訊，不容許截斷。

    回傳解析後的 dict；失敗拋 ValueError。
    """
    text = raw.strip()
    # 去除 markdown code fence 包裹（§C.3 語料案例）
    if text.startswith("```"):
        lines = text.splitlines()
        # 去掉首行 ```json 與末行 ```
        start = 1 if len(lines) > 1 else 0
        end = len(lines)
        while end > start and lines[end - 1].strip().startswith("```"):
            end -= 1
        text = "\n".join(lines[start:end]).strip()
    # 若含雜訊前綴，嘗試抓第一個 { 到最後一個 }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
        raise ValueError("output contains no JSON object")
    snippet = text[first_brace : last_brace + 1]
    try:
        parsed = json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON top-level must be an object")
    return parsed


def validate_report(
    data: dict[str, object], expected_incident_id: str | None = None
) -> TriageReport:
    """依 §C.1 驗證並建構 TriageReport；任何不符拋 ValueError（錯誤訊息供 repair prompt）。"""
    errors: list[str] = []

    incident_id = data.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id.strip():
        _err(errors, "incident_id must be a non-empty string")

    hypotheses_raw = data.get("hypotheses")
    hypotheses: list[Hypothesis] = []
    if not isinstance(hypotheses_raw, list) or not (1 <= len(hypotheses_raw) <= MAX_HYPOTHESES):
        _err(errors, f"hypotheses must be an array of 1-{MAX_HYPOTHESES} items")
    else:
        for i, h in enumerate(hypotheses_raw):
            if not isinstance(h, dict):
                _err(errors, f"hypotheses[{i}] must be an object")
                continue
            cause = h.get("cause")
            conf = h.get("confidence")
            evidence = h.get("evidence", [])
            cause_ok = isinstance(cause, str) and bool(cause.strip())
            conf_ok = (
                isinstance(conf, int | float)
                and not isinstance(conf, bool)
                and 0.0 <= float(conf) <= 1.0
            )
            evidence_ok = isinstance(evidence, list) and all(isinstance(e, str) for e in evidence)
            if not cause_ok:
                _err(errors, f"hypotheses[{i}].cause must be a non-empty string")
            if not conf_ok:
                _err(errors, f"hypotheses[{i}].confidence must be a number in [0.0, 1.0]")
            if not evidence_ok:
                _err(errors, f"hypotheses[{i}].evidence must be an array of strings")
            if cause_ok and conf_ok and evidence_ok:
                assert isinstance(cause, str)
                assert isinstance(conf, int | float)
                hypotheses.append(
                    Hypothesis(
                        cause=cause,
                        confidence=float(conf),
                        evidence=[str(e) for e in evidence],  # type: ignore[arg-type]
                    )
                )

    actions_raw = data.get("suggested_actions")
    actions: list[SuggestedAction] = []
    if not isinstance(actions_raw, list):
        _err(errors, "suggested_actions must be an array")
    else:
        for i, a in enumerate(actions_raw):
            if not isinstance(a, dict):
                _err(errors, f"suggested_actions[{i}] must be an object")
                continue
            action = a.get("action")
            risk = a.get("risk")
            runbook_ref = a.get("runbook_ref")
            action_ok = isinstance(action, str) and bool(action.strip())
            risk_ok = risk in RISK_LEVELS
            ref_ok = runbook_ref is None or isinstance(runbook_ref, str)
            if not action_ok:
                _err(errors, f"suggested_actions[{i}].action must be a non-empty string")
            if not risk_ok:
                _err(
                    errors,
                    f"suggested_actions[{i}].risk must be one of"
                    f" {sorted(RISK_LEVELS)}, got {risk!r}",
                )
            if not ref_ok:
                _err(errors, f"suggested_actions[{i}].runbook_ref must be a string or null")
            if action_ok and risk_ok and ref_ok:
                assert isinstance(action, str)
                actions.append(
                    SuggestedAction(action=action, risk=str(risk), runbook_ref=runbook_ref)
                )

    missing_context = data.get("missing_context")
    missing_list: list[str] | None = None
    if not isinstance(missing_context, list):
        _err(errors, "missing_context must be an array of strings")
    elif not all(isinstance(m, str) for m in missing_context):
        _err(errors, "missing_context must contain only strings")
    else:
        missing_list = [str(m) for m in missing_context]

    prompt_version = data.get("prompt_version")
    if not isinstance(prompt_version, str) or not SEMVER_RE.match(prompt_version):
        _err(errors, "prompt_version must be a semver string like '1.0.0'")

    if (
        expected_incident_id is not None
        and isinstance(incident_id, str)
        and incident_id != expected_incident_id
    ):
        _err(
            errors, f"incident_id mismatch: expected {expected_incident_id!r}, got {incident_id!r}"
        )

    if errors:
        raise ValueError("; ".join(errors))

    return TriageReport(
        incident_id=str(incident_id),
        hypotheses=hypotheses,
        suggested_actions=actions,
        missing_context=missing_list or [],
        prompt_version=str(prompt_version),
    )


def ensure_executor_input(report: object) -> TriageReport:
    """executor 的硬拒絕閘門（§C.2）：只接受 validated TriageReport。"""
    if not isinstance(report, TriageReport) or not report.validated:
        raise ExecutorRejected("executor only accepts a validated TriageReport")
    return report
