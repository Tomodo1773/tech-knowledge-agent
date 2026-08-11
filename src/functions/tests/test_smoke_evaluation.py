from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]
DATASET = REPOSITORY_ROOT / "eval" / "smoke.jsonl"
SOURCE_URL = (
    "https://github.com/acme/blog/blob/"
    "0123456789abcdef0123456789abcdef01234567/articles/sync.md"
)


def _module() -> Any:
    path = REPOSITORY_ROOT / "scripts" / "run-smoke-evaluation.py"
    spec = importlib.util.spec_from_file_location("run_smoke_evaluation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _module()


def _case(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "sync-schedule",
        "query": "When does the article sync run?",
        "expectedSources": ["articles/sync.md"],
        **overrides,
    }


def test_valid_case_is_parsed_into_the_fixed_contract() -> None:
    case = smoke.parse_case(_case(), line_number=1)

    assert case.id == "sync-schedule"
    assert case.expected_sources == ("articles/sync.md",)


@pytest.mark.parametrize(
    "raw",
    [
        _case(id=""),
        _case(query="   "),
        _case(expectedSources=[]),
        _case(expectedSources=["draft/sync.md"]),
        _case(expectedSources=["articles/sync.txt"]),
        _case(note="extra"),
    ],
)
def test_unusable_cases_are_rejected_before_any_agent_call(raw: dict[str, Any]) -> None:
    with pytest.raises(smoke.SmokeDatasetError):
        smoke.parse_case(raw, line_number=7)


def test_duplicate_ids_and_empty_datasets_are_rejected(tmp_path: Path) -> None:
    duplicated = tmp_path / "duplicated.jsonl"
    duplicated.write_text(
        "\n".join(json.dumps(_case()) for _ in range(2)),
        encoding="utf-8",
    )
    with pytest.raises(smoke.SmokeDatasetError, match="repeats case id"):
        smoke.load_dataset(duplicated)

    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n\n", encoding="utf-8")
    with pytest.raises(smoke.SmokeDatasetError, match="empty"):
        smoke.load_dataset(blank)


def test_citations_are_deduplicated_in_first_seen_order() -> None:
    answer = (
        "Answer text.\n\n## Sources\n"
        f"- [A]({SOURCE_URL})\n"
        f"- [A again]({SOURCE_URL})\n"
        "- [B](https://example.test/not-github)\n"
    )

    assert smoke.citations(answer) == (SOURCE_URL,)


def test_expected_sources_are_matched_against_the_cited_article_paths() -> None:
    case = smoke.parse_case(_case(expectedSources=["articles/sync.md"]), line_number=1)

    assert smoke.missing_sources(case, [SOURCE_URL]) == ()
    assert smoke.missing_sources(case, []) == ("articles/sync.md",)
    # A different article whose name merely ends the same way must not count.
    assert smoke.missing_sources(case, [SOURCE_URL.replace("/sync.md", "/resync.md")]) == (
        "articles/sync.md",
    )


@pytest.mark.skipif(not DATASET.exists(), reason="the smoke dataset is not committed yet")
def test_the_committed_dataset_matches_the_contract_and_stays_around_ten_cases() -> None:
    cases = smoke.load_dataset(DATASET)

    assert 8 <= len(cases) <= 12
