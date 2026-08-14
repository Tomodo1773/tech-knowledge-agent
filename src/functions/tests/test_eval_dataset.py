from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]
SCRIPTS = REPOSITORY_ROOT / "scripts"
DATASET = REPOSITORY_ROOT / "eval" / "smoke.jsonl"
CRITERIA = REPOSITORY_ROOT / "eval" / "criteria.yaml"

sys.path.insert(0, str(SCRIPTS))

import eval_dataset  # noqa: E402


def _module(file_name: str, module_name: str) -> Any:
    """Import one of the hyphenated entry points, which `import` cannot name."""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _module("run-foundry-evaluation.py", "run_foundry_evaluation")


def _case(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "sync-schedule",
        "caseType": "normal",
        "query": "記事の同期はいつ動く？",
        "expectedBehavior": "日次のTimerで動くと述べる。",
        "expectedSources": ["articles/sync.md"],
        **overrides,
    }


def test_valid_case_is_parsed_into_the_fixed_contract() -> None:
    case = eval_dataset.parse_case(_case(), line_number=1)

    assert case.id == "sync-schedule"
    assert case.case_type == "normal"
    assert case.expected_sources == ("articles/sync.md",)
    assert case.primary_source == "articles/sync.md"


@pytest.mark.parametrize(
    "raw",
    [
        _case(id=""),
        _case(query="   "),
        _case(expectedBehavior=""),
        _case(caseType="typo"),
        _case(expectedSources=[]),
        _case(expectedSources=["draft/sync.md"]),
        _case(expectedSources=["articles/sync.txt"]),
        # A no-evidence case must not name an article, and the other types must.
        _case(caseType="no-evidence"),
        _case(caseType="multi-article", expectedSources=["articles/sync.md"]),
        _case(note="extra"),
    ],
)
def test_unusable_cases_are_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(eval_dataset.EvalDatasetError):
        eval_dataset.parse_case(raw, line_number=7)


def test_a_no_evidence_case_is_accepted_without_a_source() -> None:
    case = eval_dataset.parse_case(
        _case(caseType="no-evidence", expectedSources=[]), line_number=1
    )

    assert case.expected_sources == ()
    assert case.primary_source == ""


def test_duplicate_ids_and_empty_datasets_are_rejected(tmp_path: Path) -> None:
    duplicated = tmp_path / "duplicated.jsonl"
    duplicated.write_text(
        "\n".join(json.dumps(_case(), ensure_ascii=False) for _ in range(2)),
        encoding="utf-8",
    )
    with pytest.raises(eval_dataset.EvalDatasetError, match="repeats case id"):
        eval_dataset.load_dataset(duplicated)

    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n\n", encoding="utf-8")
    with pytest.raises(eval_dataset.EvalDatasetError, match="empty"):
        eval_dataset.load_dataset(blank)


def test_rows_carry_the_flattened_source_the_string_check_compares() -> None:
    cases = (
        eval_dataset.parse_case(_case(), line_number=1),
        eval_dataset.parse_case(_case(id="none", caseType="no-evidence", expectedSources=[]),
                                line_number=2),
    )

    rows = runner.build_rows(cases)

    assert rows[0]["item"]["expectedSource"] == "articles/sync.md"
    assert rows[1]["item"]["expectedSource"] == ""
    assert set(rows[0]["item"]) == set(runner.data_source_config()["item_schema"]["properties"])


def test_the_committed_criteria_build_the_two_stage_one_graders() -> None:
    stage, criteria = runner.load_criteria(CRITERIA, judge_model="judge-deployment")

    assert stage == 1
    assert [criterion["type"] for criterion in criteria] == ["score_model", "string_check"]
    score_model = criteria[0]
    assert score_model["model"] == "judge-deployment"
    assert score_model["pass_threshold"] == 4
    # The committed prompt is what the judge sees, so it must reach the payload.
    assert "点数の意味" in score_model["input"][0]["content"]
    assert "{{sample.output_text}}" in score_model["input"][1]["content"]


def test_a_grader_that_could_not_score_is_told_apart_from_a_real_failure() -> None:
    throttled = {
        "name": "answer_quality",
        "passed": False,
        "score": 0.0,
        "sample": {"error": {"code": "429", "message": "exceeded rate limit"}},
    }
    scored_low = {"name": "answer_quality", "passed": False, "score": 3.0, "sample": {}}
    deterministic = {"name": "cited_expected_source", "passed": True, "sample": None}

    assert runner.grader_error(throttled) == "429 exceeded rate limit"
    assert runner.grader_error(scored_low) == ""
    assert runner.grader_error(deterministic) == ""


def test_the_judges_reasoning_is_dug_out_of_the_grader_sample() -> None:
    steps = [
        {"description": "sfwの利用に触れている。", "conclusion": "満たす。"},
        {"description": "Free版の手軽さが無い。", "conclusion": "欠けている。"},
    ]
    scored_low = {
        "name": "answer_quality",
        "passed": False,
        "score": 3.0,
        # Foundry leaves `reason` null and hides the justification in here.
        "reason": None,
        "sample": {
            "output": [
                {
                    "role": "assistant",
                    "content": json.dumps({"steps": steps, "result": 3.0}, ensure_ascii=False),
                }
            ]
        },
    }

    assert runner.grader_reasoning(scored_low) == [
        "sfwの利用に触れている。 満たす。",
        "Free版の手軽さが無い。 欠けている。",
    ]
    assert runner.grader_reasoning({"sample": None}) == []
    assert runner.grader_reasoning({"sample": {"output": [{"content": "not json"}]}}) == []


def test_the_fingerprint_changes_only_when_the_criteria_change() -> None:
    config = runner.data_source_config()
    _, criteria = runner.load_criteria(CRITERIA, judge_model="judge-deployment")
    _, same = runner.load_criteria(CRITERIA, judge_model="judge-deployment")
    _, other_judge = runner.load_criteria(CRITERIA, judge_model="another-deployment")

    assert runner.fingerprint(config, criteria) == runner.fingerprint(config, same)
    assert runner.fingerprint(config, criteria) != runner.fingerprint(config, other_judge)

    loosened = json.loads(json.dumps(criteria))
    loosened[0]["pass_threshold"] = 3
    assert runner.fingerprint(config, criteria) != runner.fingerprint(config, loosened)


def test_the_committed_dataset_matches_the_contract_and_stays_around_ten_cases() -> None:
    cases = eval_dataset.load_dataset(DATASET)

    assert 8 <= len(cases) <= 16
    assert all(case.expected_behavior for case in cases)
