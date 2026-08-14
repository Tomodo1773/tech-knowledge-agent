"""Run the evaluation dataset against the deployed Hosted Agent as a Foundry evaluation.

    uv run --project src/functions --no-sync python scripts/run-foundry-evaluation.py

The eval and its runs show up on the project's Evaluations page. Design and the reason
each criterion exists are in docs/quality.md#評価設計.

The eval object carries the schema and the testing criteria, and cannot be edited once
runs hang off it. So the criteria are fingerprinted: an eval whose fingerprint matches
the current files is reused, and changing a prompt, a threshold or the judge model
creates a new one. Runs stay comparable within a fingerprint and never straddle a
change of criteria.

This is a diagnostic, not a deploy gate: it exits 0 whenever the run completed, whatever
the scores are. Failures are read from the printed table and the Foundry report.

Requires FOUNDRY_PROJECT_ENDPOINT, AZURE_AI_MODEL_DEPLOYMENT_NAME (the judge model), and
a signed-in identity with Foundry User on the project -- Foundry Project Manager, what
the deployer holds, also covers it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_dataset import (
    DEFAULT_DATASET,
    REPOSITORY_ROOT,
    EvalCase,
    EvalDatasetError,
    load_dataset,
)

DEFAULT_CRITERIA = REPOSITORY_ROOT / "eval" / "criteria.yaml"
AGENT_NAME = "knowledge-agent"
# Tells reused evals of this project apart from anything else in the Foundry project.
EVAL_OWNER = "tech-knowledge-agent"
POLL_SECONDS = 10
POLL_LIMIT = 180
FINISHED = frozenset({"completed", "failed", "canceled"})


class EvaluationConfigError(ValueError):
    """Raised when eval/criteria.yaml does not describe a usable set of criteria."""


def load_criteria(path: Path, *, judge_model: str) -> tuple[int, list[dict[str, Any]]]:
    """Turn the committed criteria file into the testing criteria the API takes."""
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise EvaluationConfigError(f"{path.name} is not a mapping")
    stage = document.get("stage")
    declared = document.get("criteria")
    if not isinstance(stage, int):
        raise EvaluationConfigError(f"{path.name} has no integer stage")
    if not isinstance(declared, list) or not declared:
        raise EvaluationConfigError(f"{path.name} declares no criteria")

    criteria: list[dict[str, Any]] = []
    for entry in declared:
        if not isinstance(entry, Mapping) or "type" not in entry or "name" not in entry:
            raise EvaluationConfigError(f"{path.name} has a criterion without type and name")
        if entry["type"] == "score_model":
            prompt = (path.parent / entry["promptFile"]).read_text(encoding="utf-8")
            criteria.append(
                {
                    "type": "score_model",
                    "name": entry["name"],
                    "model": judge_model,
                    "input": [
                        {"role": "developer", "content": prompt},
                        {"role": "user", "content": entry["input"]},
                    ],
                    "range": list(entry["range"]),
                    "pass_threshold": entry["passThreshold"],
                }
            )
        elif entry["type"] == "string_check":
            criteria.append(
                {
                    "type": "string_check",
                    "name": entry["name"],
                    "operation": entry["operation"],
                    "input": entry["input"],
                    "reference": entry["reference"],
                }
            )
        else:
            raise EvaluationConfigError(f"{path.name} has unsupported type {entry['type']!r}")
    return stage, criteria


def data_source_config() -> dict[str, Any]:
    """Describe the item fields the criteria templates reference.

    expectedSource is the flattened primary source: string_check compares one string,
    so the list the dataset keeps cannot be handed over as-is.
    """
    fields = ("id", "caseType", "query", "expectedBehavior", "expectedSource")
    return {
        "type": "custom",
        "item_schema": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in fields},
            "required": list(fields),
        },
        # Without this the templates cannot reference {{sample.output_text}}.
        "include_sample_schema": True,
    }


def build_rows(cases: Sequence[EvalCase]) -> list[dict[str, Any]]:
    return [
        {
            "item": {
                "id": case.id,
                "caseType": case.case_type,
                "query": case.query,
                "expectedBehavior": case.expected_behavior,
                "expectedSource": case.primary_source,
            }
        }
        for case in cases
    ]


def fingerprint(config: Mapping[str, Any], criteria: Sequence[Mapping[str, Any]]) -> str:
    """Identify one set of criteria, judge model included, in 12 hex characters."""
    canonical = json.dumps([config, list(criteria)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _owned_evals(client: Any) -> list[Any]:
    return [
        existing
        for existing in client.evals.list(limit=100)
        if (existing.metadata or {}).get("owner") == EVAL_OWNER
    ]


def find_eval(client: Any, digest: str) -> Any | None:
    for existing in _owned_evals(client):
        if (existing.metadata or {}).get("criteriaFingerprint") == digest:
            return existing
    return None


def find_run(client: Any, run_id: str) -> tuple[str | None, Any | None]:
    """Locate a past run without knowing which set of criteria it belongs to."""
    for existing in _owned_evals(client):
        for run in client.evals.runs.list(eval_id=existing.id, limit=100):
            if run.id == run_id:
                return existing.id, run
    return None, None


def _project_client() -> Any:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    return project.get_openai_client()


def _await_run(client: Any, *, eval_id: str, run_id: str) -> Any:
    for _ in range(POLL_LIMIT):
        run = client.evals.runs.retrieve(run_id=run_id, eval_id=eval_id)
        if run.status in FINISHED:
            return run
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"eval run {run_id} did not finish in time")


def grader_error(result: Mapping[str, Any]) -> str:
    """The message a judge model left when it could not score the row, if any.

    A grader that errors comes back with score 0 and passed false, and Foundry counts
    the row as passed in the run-level totals. Rows like that say nothing about answer
    quality, so they have to be told apart from real failures.
    """
    error = ((result.get("sample") or {}) or {}).get("error") or {}
    message = error.get("message") or ""
    code = error.get("code") or ""
    return f"{code} {message}".strip()


def grader_reasoning(result: Mapping[str, Any]) -> list[str]:
    """Why a judge model landed on its score, one line per step it took.

    Foundry leaves the `reason` field null and keeps the justification as a JSON string
    inside the grader's own sample output, where the portal does not surface it. It is
    the only account of why a case scored what it did, so it is dug out here.
    """
    for message in ((result.get("sample") or {}) or {}).get("output") or []:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        steps = parsed.get("steps") if isinstance(parsed, Mapping) else None
        if isinstance(steps, list):
            return [
                " ".join(
                    part
                    for part in (step.get("description"), step.get("conclusion"))
                    if isinstance(part, str) and part
                )
                for step in steps
                if isinstance(step, Mapping)
            ]
    return []


def _print_results(client: Any, *, eval_id: str, run: Any) -> None:
    print(f"\nstatus     : {run.status}")
    print(f"report     : {run.report_url}")
    print(f"counts     : {run.result_counts}")
    for criterion in run.per_testing_criteria_results:
        print(f"  {criterion.testing_criteria}: passed={criterion.passed} failed={criterion.failed}")

    print("\ncase                      criterion                 passed  score  note")
    errors: list[str] = []
    explanations: list[tuple[str, str, list[str]]] = []
    for item in client.evals.runs.output_items.list(run_id=run.id, eval_id=eval_id):
        raw = item.model_dump()
        case_id = (raw.get("datasource_item") or {}).get("id", raw.get("id"))
        for result in raw.get("results") or []:
            note = grader_error(result)
            name = str(result.get("name"))
            if note:
                errors.append(f"{case_id} / {name}: {note}")
            elif not result.get("passed"):
                reasoning = grader_reasoning(result)
                if reasoning:
                    explanations.append((str(case_id), name, reasoning))
            print(
                f"{case_id:<25} {name:<25} "
                f"{result.get('passed')!s:<7} {result.get('score')!s:<6} {note}"
            )

    for case_id, name, reasoning in explanations:
        print(f"\nwhy {case_id} failed {name}:")
        for step in reasoning:
            print(f"  - {step}")

    if errors:
        print(f"\n{len(errors)} criteria could not be scored, and the run-level counts above")
        print("treat those rows as passed. Read them as unmeasured, not as quality signals.")
        for message in errors:
            print(f"  {message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    parser.add_argument(
        "--agent-version",
        help="Pin the agent version. Omitted, Foundry evaluates the latest one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent without reaching Azure.",
    )
    parser.add_argument(
        "--show",
        metavar="RUN_ID",
        help="Print the results of a past run instead of starting a new one.",
    )
    arguments = parser.parse_args(argv)

    # Questions, expected behaviours and answers are Japanese; the Windows console
    # defaults to cp932 and would raise on the first character it cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if arguments.show:
        client = _project_client()
        eval_id, run = find_run(client, arguments.show)
        if run is None:
            print(f"No run {arguments.show} under this project's evals.", file=sys.stderr)
            return 1
        _print_results(client, eval_id=eval_id, run=run)
        return 0

    # Required even for --dry-run: the judge model is part of the fingerprint, so a
    # preview built without it would not describe the run that actually happens.
    judge_model = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "")
    if not judge_model:
        print("AZURE_AI_MODEL_DEPLOYMENT_NAME is not set.", file=sys.stderr)
        return 1

    try:
        cases = load_dataset(arguments.dataset)
        stage, criteria = load_criteria(arguments.criteria, judge_model=judge_model)
    except (OSError, EvalDatasetError, EvaluationConfigError, KeyError) as error:
        print(f"Evaluation inputs are unusable: {error}", file=sys.stderr)
        return 1

    config = data_source_config()
    digest = fingerprint(config, criteria)
    target: dict[str, Any] = {"type": "azure_ai_agent", "name": AGENT_NAME}
    if arguments.agent_version:
        target["version"] = arguments.agent_version
    data_source = {
        "type": "azure_ai_target_completions",
        "source": {"type": "file_content", "content": build_rows(cases)},
        "input_messages": {
            "type": "template",
            "template": [
                {
                    "type": "message",
                    "role": "user",
                    "content": {"type": "input_text", "text": "{{item.query}}"},
                }
            ],
        },
        "target": target,
    }

    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "fingerprint": digest,
                    "stage": stage,
                    "data_source_config": config,
                    "testing_criteria": criteria,
                    "data_source": data_source,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    client = _project_client()

    evaluation = find_eval(client, digest)
    if evaluation is None:
        evaluation = client.evals.create(
            name=f"knowledge-agent-quality-{digest}",
            data_source_config=config,
            testing_criteria=criteria,
            metadata={"owner": EVAL_OWNER, "criteriaFingerprint": digest, "stage": str(stage)},
        )
        print(f"created a new eval for criteria {digest}: {evaluation.id}")
    else:
        print(f"reusing the eval for criteria {digest}: {evaluation.id}")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run = client.evals.runs.create(
        eval_id=evaluation.id,
        name=f"stage{stage}-{stamp}",
        data_source=data_source,
        metadata={
            "owner": EVAL_OWNER,
            "cases": str(len(cases)),
            "agentVersion": arguments.agent_version or "latest",
        },
    )
    print(f"started run {run.id} over {len(cases)} cases; waiting for it to finish")

    run = _await_run(client, eval_id=evaluation.id, run_id=run.id)
    _print_results(client, eval_id=evaluation.id, run=run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
