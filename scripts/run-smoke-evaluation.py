"""Run the fixed smoke dataset against the deployed Hosted Agent.

This is a diagnostic, not a deploy gate: it always exits 0 when every case ran, even
if citations miss. Failures are investigated with the printed answer and the trace.

    uv run --project src/functions --no-sync python scripts/run-smoke-evaluation.py

Requires FOUNDRY_PROJECT_ENDPOINT and a signed-in identity that can interact with the
agent endpoint on the project -- Foundry Agent Consumer is the least-privilege role, and
Foundry Project Manager (what the deployer holds) also covers it. Resolves the Agent
exactly as the Worker does so the two cannot drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPOSITORY_ROOT / "eval" / "smoke.jsonl"
sys.path.insert(0, str(REPOSITORY_ROOT / "src" / "functions"))

_ARTICLE_PATH = re.compile(r"^articles/[^\s]+\.md$")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https://github\.com/[^\s)]+)\)")


class SmokeDatasetError(ValueError):
    """Raised when a dataset line does not match the fixed case contract."""


@dataclass(frozen=True, slots=True)
class SmokeCase:
    id: str
    query: str
    expected_sources: tuple[str, ...]


def parse_case(raw: Mapping[str, object], *, line_number: int) -> SmokeCase:
    expected = set(raw) - {"id", "query", "expectedSources"}
    if expected:
        raise SmokeDatasetError(f"line {line_number} has unknown fields: {sorted(expected)}")
    case_id = raw.get("id")
    query = raw.get("query")
    sources = raw.get("expectedSources")
    if not isinstance(case_id, str) or not case_id.strip():
        raise SmokeDatasetError(f"line {line_number} has no id")
    if not isinstance(query, str) or not query.strip():
        raise SmokeDatasetError(f"line {line_number} has no query")
    if not isinstance(sources, list) or not sources:
        raise SmokeDatasetError(f"line {line_number} has no expectedSources")
    for source in sources:
        if not isinstance(source, str) or not _ARTICLE_PATH.fullmatch(source):
            raise SmokeDatasetError(f"line {line_number} expects a non-article source")
    return SmokeCase(id=case_id, query=query, expected_sources=tuple(sources))


def load_dataset(path: Path) -> tuple[SmokeCase, ...]:
    cases: list[SmokeCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise SmokeDatasetError(f"line {line_number} is not valid JSON") from error
        if not isinstance(raw, Mapping):
            raise SmokeDatasetError(f"line {line_number} is not an object")
        case = parse_case(raw, line_number=line_number)
        if case.id in seen:
            raise SmokeDatasetError(f"line {line_number} repeats case id {case.id}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise SmokeDatasetError("the smoke dataset is empty")
    return tuple(cases)


def citations(answer: str) -> tuple[str, ...]:
    """Collect the commit-fixed GitHub URLs the agent used as citations."""
    seen: list[str] = []
    for url in _MARKDOWN_LINK.findall(answer):
        if url not in seen:
            seen.append(url)
    return tuple(seen)


def missing_sources(case: SmokeCase, cited: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        source
        for source in case.expected_sources
        if not any(url.endswith(f"/{source}") for url in cited)
    )


def _agent_client() -> object:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential
    from knowledge_agent.contracts import KNOWLEDGE_AGENT_NAME
    from knowledge_agent.worker import HostedAgentClient

    project = AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    return HostedAgentClient(
        project.get_openai_client(agent_name=KNOWLEDGE_AGENT_NAME).with_options(
            timeout=120.0,
            max_retries=1,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Check the dataset contract without calling the agent.",
    )
    arguments = parser.parse_args(argv)

    try:
        cases = load_dataset(arguments.dataset)
    except (OSError, SmokeDatasetError) as error:
        print(f"Smoke dataset is unusable: {error}", file=sys.stderr)
        return 1

    if arguments.validate_only:
        print(f"{len(cases)} smoke cases are valid.")
        return 0

    agent = _agent_client()
    complete = 0
    for case in cases:
        # Each case starts its own conversation so results stay independent.
        answer = agent.ask(case.query, previous_response_id=None)  # type: ignore[attr-defined]
        cited = citations(answer.text)
        missing = missing_sources(case, cited)
        complete += not missing

        print(f"\n=== {case.id} ===")
        print(f"query    : {case.query}")
        print(f"response : {answer.response_id}")
        print(f"answer   :\n{answer.text}")
        print(f"citations: {', '.join(cited) if cited else '(none)'}")
        print(f"expected : {'all cited' if not missing else 'missing ' + ', '.join(missing)}")

    print(f"\n{complete}/{len(cases)} cases cited every expected article.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
