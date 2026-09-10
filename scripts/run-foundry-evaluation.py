"""Run the evaluation dataset against the deployed Hosted Agent as a Foundry evaluation.

    uv run --project src/functions --no-sync python scripts/run-foundry-evaluation.py

The eval and its runs show up on the project's Evaluations page. It measures the final
user-visible answer because a component can pass while the delivered answer fails, and
component-only evaluation would couple the suite to the current internal design. Semantic
answer quality and deterministic source presence are scored separately so their failure
modes do not blur together.

The eval object carries the schema and the testing criteria, and cannot be edited once
runs hang off it. So the criteria are fingerprinted: an eval whose fingerprint matches
the current files is reused, and changing a prompt, a threshold or the judge model
creates a new one. Runs stay comparable within a fingerprint and never straddle a
change of criteria.

The questions are registered as a project dataset whose version is a digest of its own
content, and runs read them from there rather than carrying them inline, so the same
question set is one addressable asset that the portal can start a run against. Built-in
evaluators score the agent's tool use alongside the hand-written graders, and the
hand-written ones are published to the evaluator catalog with --register-evaluators so
the same evaluation can be assembled from the portal. After each run the service is
asked to compare it with the last one under the same criteria.

This is a diagnostic for comparing improvements, not a deploy gate: probabilistic external
scoring does not block delivery, so regressions are not rejected automatically and a person
must compare the printed table or Foundry report. The command exits 0 whenever the run
completed, whatever the scores are.

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
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_dataset import (
    DEFAULT_DATASET,
    DEFAULT_TOOL_DEFINITIONS,
    REPOSITORY_ROOT,
    EvalCase,
    EvalDatasetError,
    load_dataset,
    load_tool_definitions,
)

DEFAULT_CRITERIA = REPOSITORY_ROOT / "eval" / "criteria.yaml"
AGENT_NAME = "knowledge-agent"
# The project dataset the runs read their rows from. Its versions are content digests,
# so the same questions never register twice and a change always lands as a new version.
DATASET_NAME = "knowledge-agent-smoke"
# Tells reused evals of this project apart from anything else in the Foundry project.
EVAL_OWNER = "tech-knowledge-agent"
POLL_SECONDS = 10
POLL_LIMIT = 180
FINISHED = frozenset({"completed", "failed", "canceled"})
INSIGHT_FINISHED = frozenset({"Succeeded", "Failed", "Canceled"})
# Catalog evaluators address their inputs by the bare names their data schema declares,
# not through the run's item and sample templates.
CATALOG_FIELDS = ("query", "expected_behavior", "response")
CATALOG_PLACEHOLDERS = {
    "{{item.query}}": "{{query}}",
    "{{item.expectedBehavior}}": "{{expected_behavior}}",
    "{{sample.output_text}}": "{{response}}",
}


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
        elif entry["type"] == "azure_ai_evaluator":
            criteria.append(
                {
                    "type": "azure_ai_evaluator",
                    "name": entry["name"],
                    "evaluator_name": entry["evaluatorName"],
                    "data_mapping": entry["dataMapping"],
                    # Built-in evaluators reject a run that does not name a judge model,
                    # so they score against the same deployment as the graders above and
                    # a change of model still splits the eval.
                    "initialization_parameters": {"deployment_name": judge_model},
                }
            )
        else:
            raise EvaluationConfigError(f"{path.name} has unsupported type {entry['type']!r}")
    return stage, criteria


def data_source_config() -> dict[str, Any]:
    """Describe the item fields the criteria templates reference.

    expectedSource is the flattened primary source: string_check compares one string,
    so the list the dataset keeps cannot be handed over as-is. toolDefinitions is the
    only route the tool evaluators receive their definitions by; the agent target's own
    tool descriptions never reach them at run time.
    """
    text_fields = ("id", "caseType", "query", "expectedBehavior", "expectedSource")
    properties: dict[str, Any] = {field: {"type": "string"} for field in text_fields}
    properties["toolDefinitions"] = {"type": "array"}
    return {
        "type": "custom",
        "item_schema": {
            "type": "object",
            "properties": properties,
            "required": [*text_fields, "toolDefinitions"],
        },
        # Without this the templates cannot reference {{sample.output_text}}.
        "include_sample_schema": True,
    }


def build_items(
    cases: Sequence[EvalCase],
    tool_definitions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One item per case, as the dataset file holds them.

    Each line is the item itself. Wrapping it in {"item": ...}, the shape the API takes
    for rows passed inline, makes that wrapper the item: the templates then resolve
    {{item.query}} to nothing and the agent is asked an empty question.
    """
    return [
        {
            "id": case.id,
            "caseType": case.case_type,
            "query": case.query,
            "expectedBehavior": case.expected_behavior,
            "expectedSource": case.primary_source,
            "toolDefinitions": [dict(definition) for definition in tool_definitions],
        }
        for case in cases
    ]


def dataset_content(items: Sequence[Mapping[str, Any]]) -> str:
    """The JSONL the project dataset holds, in a form whose digest is stable."""
    return "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items
    )


def dataset_version(content: str) -> str:
    """Identify one question set by its content, so re-registering adds no version."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def register_dataset(project: Any, *, content: str, version: str) -> Any:
    """Return the project dataset holding this content, uploading it only when new."""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return project.datasets.get(name=DATASET_NAME, version=version)
    except ResourceNotFoundError:
        pass
    with tempfile.TemporaryDirectory() as directory:
        # The blob keeps this file name, so it stays readable in the portal.
        path = Path(directory) / f"{DATASET_NAME}.jsonl"
        path.write_text(content, encoding="utf-8")
        return project.datasets.upload_file(
            name=DATASET_NAME, version=version, file_path=str(path)
        )


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


def _clients() -> tuple[Any, Any]:
    """The project client, which owns datasets and insights, and the evals client."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    return project, project.get_openai_client()


def catalog_prompt(criterion: Mapping[str, Any]) -> str:
    """Rewrite a run-time grader prompt into what the evaluator catalog expects.

    The output format is deliberately absent from both forms. The catalog enforces its
    own `result` and `reason` contract, and a second one written here would collide.
    """
    text = "\n\n".join(str(message["content"]) for message in criterion["input"])
    for template, field in CATALOG_PLACEHOLDERS.items():
        text = text.replace(template, field)
    return text


def registered_digest(project: Any, name: str) -> str:
    """The fingerprint of the newest registered version of this evaluator, if any."""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        versions = list(project.beta.evaluators.list_versions(name, type="custom", limit=100))
    except ResourceNotFoundError:
        return ""
    if not versions:
        return ""
    latest = max(versions, key=lambda version: version.created_at)
    return (latest.metadata or {}).get("criteriaFingerprint", "")


def register_evaluators(project: Any, criteria: Sequence[Mapping[str, Any]]) -> int:
    """Publish the hand-written graders to the project's evaluator catalog.

    Registration makes them selectable from the portal's own evaluation screen. The runs
    below keep using the inline graders, which are the only form that reports why a case
    scored what it did. string_check has no catalog counterpart and is left out.
    """
    from azure.ai.projects.models import (
        EvaluatorMetric,
        EvaluatorVersion,
        PromptBasedEvaluatorDefinition,
    )

    published = 0
    for criterion in criteria:
        if criterion["type"] != "score_model":
            continue
        name = str(criterion["name"])
        prompt = catalog_prompt(criterion)
        low, high = criterion["range"]
        digest = fingerprint({"catalog": list(CATALOG_FIELDS)}, [criterion])
        if registered_digest(project, name) == digest:
            print(f"{name} is already registered for criteria {digest}")
            continue
        version = project.beta.evaluators.create_version(
            name,
            EvaluatorVersion(
                display_name=name,
                evaluator_type="custom",
                categories=["quality"],
                supported_evaluation_levels=["turn"],
                metadata={"owner": EVAL_OWNER, "criteriaFingerprint": digest},
                definition=PromptBasedEvaluatorDefinition(
                    prompt_text=prompt,
                    data_schema={
                        "type": "object",
                        "properties": {field: {"type": "string"} for field in CATALOG_FIELDS},
                        "required": list(CATALOG_FIELDS),
                    },
                    metrics={
                        name: EvaluatorMetric(
                            # 'numeric' is rejected; the scale is one of three named kinds.
                            type="ordinal",
                            desirable_direction="increase",
                            min_value=low,
                            max_value=high,
                            threshold=criterion["pass_threshold"],
                            is_primary=True,
                        )
                    },
                ),
            ),
        )
        print(f"registered {name} version {version.version} for criteria {digest}")
        published += 1
    return published


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


def unmeasured(result: Mapping[str, Any]) -> str:
    """Why a criterion measured nothing at all, if that is what happened.

    A built-in evaluator that finds nothing to judge reports itself as skipped, with a
    null score, and Foundry counts the row as passed. Rows like that say nothing about
    quality, so they are told apart from real failures the same way errors are.
    """
    if result.get("status") != "skipped" and result.get("score") is not None:
        return ""
    return str(result.get("reason") or result.get("label") or "not scored")


def grader_reason(result: Mapping[str, Any]) -> str:
    """The account a built-in evaluator gives of its own score. Empty for the graders."""
    return str(result.get("reason") or "")


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
            note = grader_error(result) or unmeasured(result)
            name = str(result.get("name"))
            if note:
                errors.append(f"{case_id} / {name}: {note}")
            elif not result.get("passed"):
                reason = grader_reason(result)
                reasoning = [reason] if reason else grader_reasoning(result)
                if reasoning:
                    explanations.append((str(case_id), name, reasoning))
            print(
                f"{case_id:<25} {name:<25} "
                f"{result.get('passed')!s:<7} {result.get('score')!s:<6} {note[:60]}"
            )

    for case_id, name, reasoning in explanations:
        print(f"\nwhy {case_id} failed {name}:")
        for step in reasoning:
            print(f"  - {step}")

    if errors:
        print(f"\n{len(errors)} criteria measured nothing, and the run-level counts above")
        print("treat those rows as passed. Read them as unmeasured, not as quality signals.")
        for message in errors:
            print(f"  {message}")


def previous_run(client: Any, *, eval_id: str, run_id: str) -> Any | None:
    """The newest completed run under the same criteria, other than the one just made."""
    finished = [
        existing
        for existing in client.evals.runs.list(eval_id=eval_id, limit=100)
        if existing.id != run_id and existing.status == "completed"
    ]
    return max(finished, key=lambda existing: existing.created_at, default=None)


def _print_comparison(project: Any, client: Any, *, eval_id: str, run: Any) -> None:
    """Let the service compare this run with the last one instead of reading two tables."""
    from azure.ai.projects.models import EvaluationComparisonInsightRequest, Insight

    baseline = previous_run(client, eval_id=eval_id, run_id=run.id)
    if baseline is None:
        print("\nNo earlier completed run under these criteria, so there is nothing to compare.")
        return

    insight = project.beta.insights.generate(
        Insight(
            display_name=f"{EVAL_OWNER}-{run.id}",
            request=EvaluationComparisonInsightRequest(
                eval_id=eval_id,
                baseline_run_id=baseline.id,
                treatment_run_ids=[run.id],
            ),
        )
    )
    for _ in range(POLL_LIMIT):
        insight = project.beta.insights.get(insight.insight_id)
        if insight.state in INSIGHT_FINISHED:
            break
        time.sleep(POLL_SECONDS)
    if insight.state != "Succeeded":
        print(f"\nThe comparison with {baseline.id} ended as {insight.state}.")
        return

    print(f"\ncompared with {baseline.id} ({insight.result.method})")
    print("criterion                  baseline  this run    delta  p-value  effect")
    for comparison in insight.result.comparisons:
        for item in comparison.compare_items:
            print(
                f"{comparison.testing_criteria:<25} "
                f"{comparison.baseline_run_summary.average:>8.3f} "
                f"{item.treatment_run_summary.average:>8.3f} "
                f"{item.delta_estimate:>+8.3f} "
                f"{item.p_value:>8.3f}  {item.treatment_effect}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    parser.add_argument("--tool-definitions", type=Path, default=DEFAULT_TOOL_DEFINITIONS)
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
    parser.add_argument(
        "--register-evaluators",
        action="store_true",
        help="Publish the hand-written graders to the evaluator catalog and stop.",
    )
    arguments = parser.parse_args(argv)

    # Questions, expected behaviours and answers are Japanese; the Windows console
    # defaults to cp932 and would raise on the first character it cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if arguments.show:
        _, client = _clients()
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
        tool_definitions = load_tool_definitions(arguments.tool_definitions)
        stage, criteria = load_criteria(arguments.criteria, judge_model=judge_model)
    except (OSError, EvalDatasetError, EvaluationConfigError, KeyError) as error:
        print(f"Evaluation inputs are unusable: {error}", file=sys.stderr)
        return 1

    if arguments.register_evaluators:
        project, _ = _clients()
        register_evaluators(project, criteria)
        return 0

    config = data_source_config()
    digest = fingerprint(config, criteria)
    items = build_items(cases, tool_definitions)
    content = dataset_content(items)
    version = dataset_version(content)
    target: dict[str, Any] = {"type": "azure_ai_agent", "name": AGENT_NAME}
    if arguments.agent_version:
        target["version"] = arguments.agent_version
    data_source: dict[str, Any] = {
        "type": "azure_ai_target_completions",
        # Replaced with the registered dataset below. --dry-run stops before that
        # and prints the items it would register.
        "source": {"type": "file_content", "content": items},
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
                    "datasetVersion": version,
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

    project, client = _clients()

    dataset = register_dataset(project, content=content, version=version)
    print(f"question set {DATASET_NAME} version {version}: {dataset.id}")
    data_source["source"] = {"type": "file_id", "id": dataset.id}

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
            # The question set is not part of the fingerprint, so runs over different
            # versions of it share one eval. This is the only record of which one ran.
            "datasetVersion": version,
        },
    )
    print(f"started run {run.id} over {len(cases)} cases; waiting for it to finish")

    run = _await_run(client, eval_id=evaluation.id, run_id=run.id)
    _print_results(client, eval_id=evaluation.id, run=run)
    _print_comparison(project, client, eval_id=evaluation.id, run=run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
