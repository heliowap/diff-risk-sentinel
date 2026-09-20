from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

SeverityType = Literal["critical", "major", "minor"]
VerdictType = Literal["found", "near", "missed"]
PrecisionType = Literal["correct", "incorrect", "unverifiable"]


@dataclass
class Finding:
    file: str
    line: int
    function: str
    claim: str
    severity: str = "major"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Finding:
        return cls(
            file=str(data.get("file", "")),
            line=int(data.get("line", 0)),
            function=str(data.get("function", "")),
            claim=str(data.get("claim", "")),
            severity=str(data.get("severity", "major")).lower(),
        )


@dataclass
class ReviewOutput:
    findings: List[Finding] = field(default_factory=list)
    report_markdown: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "report_markdown": self.report_markdown,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ReviewOutput:
        findings = [Finding.from_dict(f) for f in data.get("findings", [])]
        return cls(
            findings=findings,
            report_markdown=data.get("report_markdown", ""),
        )


@dataclass
class CaseConfig:
    case_id: str
    intro_commit: str
    base_commit: str
    category: str  # "small", "medium", "large", "clean"
    touched_production_functions: int
    fix_commit: Optional[str] = None
    fixed_functions: Any = field(default_factory=dict)
    subject: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CaseConfig:
        return cls(**data)


@dataclass
class RunResult:
    case_id: str
    arm: str
    repetition: int
    target_commit: str
    base_commit: str
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    jev_tokens: int = 0
    jev_cost_usd: float = 0.0
    findings: List[Finding] = field(default_factory=list)
    report_markdown: str = ""
    error: Optional[str] = None
    aborted: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        d["findings"] = [f.to_dict() for f in self.findings]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RunResult:
        data_copy = dict(data)
        data_copy.pop("total_tokens", None)
        findings = [Finding.from_dict(f) for f in data_copy.pop("findings", [])]
        return cls(findings=findings, **data_copy)


@dataclass
class FindingJudgment:
    finding_index: int
    verdict: PrecisionType
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FindingJudgment:
        return cls(**data)


@dataclass
class CaseGrading:
    case_id: str
    arm: str
    repetition: int
    known_defect_verdict: VerdictType
    finding_judgments: List[FindingJudgment] = field(default_factory=list)
    human_spot_checked: bool = False
    human_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "arm": self.arm,
            "repetition": self.repetition,
            "known_defect_verdict": self.known_defect_verdict,
            "finding_judgments": [j.to_dict() for j in self.finding_judgments],
            "human_spot_checked": self.human_spot_checked,
            "human_notes": self.human_notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CaseGrading:
        judgments = [FindingJudgment.from_dict(j) for j in data.get("finding_judgments", [])]
        return cls(
            case_id=data["case_id"],
            arm=data["arm"],
            repetition=data["repetition"],
            known_defect_verdict=data["known_defect_verdict"],
            finding_judgments=judgments,
            human_spot_checked=data.get("human_spot_checked", False),
            human_notes=data.get("human_notes", ""),
        )


def parse_review_output(raw_text: str) -> ReviewOutput:
    """
    Parses agent response text into a ReviewOutput structure.
    Handles raw JSON, markdown-fenced ```json ... ```, and fallbacks.
    """
    cleaned = raw_text.strip()
    if not cleaned:
        return ReviewOutput()

    # Try 1: Find fenced json block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                return ReviewOutput.from_dict(parsed)
        except Exception:
            pass

    # Try 2: Look for outermost JSON object with "findings"
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        candidate = cleaned[start_idx : end_idx + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and ("findings" in parsed or "report_markdown" in parsed):
                return ReviewOutput.from_dict(parsed)
        except Exception:
            pass

    # Fallback: treat whole text as report_markdown with 0 structured findings
    return ReviewOutput(findings=[], report_markdown=cleaned)
