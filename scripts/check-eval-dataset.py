"""Check that the evaluation dataset still matches the fixed case contract.

    uv run --project src/functions --no-sync python scripts/check-eval-dataset.py

Local only: it reads no Azure resource and calls no agent. Running the cases against
the deployed agent is run-foundry-evaluation.py's job.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_dataset import DEFAULT_DATASET, EvalDatasetError, load_dataset


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    arguments = parser.parse_args(argv)

    try:
        cases = load_dataset(arguments.dataset)
    except (OSError, EvalDatasetError) as error:
        print(f"Evaluation dataset is unusable: {error}", file=sys.stderr)
        return 1

    counts = Counter(case.case_type for case in cases)
    breakdown = ", ".join(f"{case_type}={count}" for case_type, count in sorted(counts.items()))
    print(f"{len(cases)} evaluation cases are valid ({breakdown}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
