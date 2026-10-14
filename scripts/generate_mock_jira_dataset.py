#!/usr/bin/env python3
"""Generate a deterministic mock Jira dataset for offline demos and tests.

Writes ``examples/raw_jira_dump.json`` with the same schema as the Jira REST
``/search`` endpoint (fields + changelog histories) so that the whole pipeline
can run without credentials or network access.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from supportpulse.config import project_root
from supportpulse.extractor.jira_client import generate_mock_issues


def build_dump(count: int, days: int, seed: int) -> dict:
    """Return the mock dump payload."""

    reference = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)
    issues = generate_mock_issues(count=count, days=days, seed=seed, reference=reference)
    return {
        "project": "SUP",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_time": reference.isoformat(),
        "days": days,
        "total": len(issues),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=140, help="Number of tickets.")
    parser.add_argument("--days", type=int, default=45, help="History window in days.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Destination path (default: examples/raw_jira_dump.json).",
    )
    args = parser.parse_args()

    destination = args.output or (project_root() / "examples" / "raw_jira_dump.json")
    destination.parent.mkdir(parents=True, exist_ok=True)

    payload = build_dump(args.count, args.days, args.seed)
    destination.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"Wrote {payload['total']} mock tickets -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
