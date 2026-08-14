"""The fixed case contract for the evaluation dataset.

Shared by check-eval-dataset.py, which enforces it locally, and by
run-foundry-evaluation.py, which projects each case into an evaluation run.
Importable on its own so neither entry point owns the contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPOSITORY_ROOT / "eval" / "smoke.jsonl"

NORMAL = "normal"
NO_EVIDENCE = "no-evidence"
MULTI_ARTICLE = "multi-article"
CASE_TYPES = (NORMAL, NO_EVIDENCE, MULTI_ARTICLE)

_FIELDS = frozenset({"id", "caseType", "query", "expectedBehavior", "expectedSources"})
_ARTICLE_PATH = re.compile(r"^articles/[^\s]+\.md$")


class EvalDatasetError(ValueError):
    """Raised when a dataset line does not match the fixed case contract."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    case_type: str
    query: str
    expected_behavior: str
    expected_sources: tuple[str, ...]

    @property
    def primary_source(self) -> str:
        """The one source the deterministic citation check compares against.

        Empty for no-evidence cases, which have nothing to compare. How that
        emptiness should be scored is still open (docs/quality.md#未解決).
        """
        return self.expected_sources[0] if self.expected_sources else ""


def _text(raw: Mapping[str, object], field: str, *, line_number: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EvalDatasetError(f"line {line_number} has no {field}")
    return value


def parse_case(raw: Mapping[str, object], *, line_number: int) -> EvalCase:
    unknown = set(raw) - _FIELDS
    missing = _FIELDS - set(raw)
    if unknown or missing:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unknown:
            details.append(f"unknown={sorted(unknown)}")
        raise EvalDatasetError(f"line {line_number} has wrong fields: {', '.join(details)}")

    case_id = _text(raw, "id", line_number=line_number)
    case_type = _text(raw, "caseType", line_number=line_number)
    query = _text(raw, "query", line_number=line_number)
    behavior = _text(raw, "expectedBehavior", line_number=line_number)
    if case_type not in CASE_TYPES:
        raise EvalDatasetError(f"line {line_number} has an unknown caseType {case_type!r}")

    sources = raw.get("expectedSources")
    if not isinstance(sources, list):
        raise EvalDatasetError(f"line {line_number} has no expectedSources list")
    for source in sources:
        if not isinstance(source, str) or not _ARTICLE_PATH.fullmatch(source):
            raise EvalDatasetError(f"line {line_number} expects a non-article source")
    # A no-evidence case that names an article contradicts its own expected behaviour,
    # and a normal case without one leaves the citation check nothing to compare.
    if case_type == NO_EVIDENCE and sources:
        raise EvalDatasetError(f"line {line_number} is no-evidence but expects a source")
    if case_type != NO_EVIDENCE and not sources:
        raise EvalDatasetError(f"line {line_number} expects no source")
    if case_type == MULTI_ARTICLE and len(sources) < 2:
        raise EvalDatasetError(f"line {line_number} is multi-article but expects one source")

    return EvalCase(
        id=case_id,
        case_type=case_type,
        query=query,
        expected_behavior=behavior,
        expected_sources=tuple(sources),
    )


def load_dataset(path: Path = DEFAULT_DATASET) -> tuple[EvalCase, ...]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvalDatasetError(f"line {line_number} is not valid JSON") from error
        if not isinstance(raw, Mapping):
            raise EvalDatasetError(f"line {line_number} is not an object")
        case = parse_case(raw, line_number=line_number)
        if case.id in seen:
            raise EvalDatasetError(f"line {line_number} repeats case id {case.id}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise EvalDatasetError("the evaluation dataset is empty")
    return tuple(cases)
