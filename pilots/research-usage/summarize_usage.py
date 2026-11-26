"""Summarize the versioned synthetic fixture using only the Python standard library."""
import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATASET_VERSION = "workspace-usage-v1"
METRICS = ("vcpu_hours", "memory_gib_hours")


def summarize(stream):
    projects = {}
    for row in csv.DictReader(stream):
        totals = projects.setdefault(row["project_id"], dict.fromkeys(METRICS, Decimal(0)))
        for metric in METRICS:
            totals[metric] += Decimal(row[metric])
    return {
        "dataset_version": DATASET_VERSION,
        "totals": {metric: format(sum(t[metric] for t in projects.values()), ".2f") for metric in METRICS},
        "projects": [
            {"project_id": project, **{metric: format(values[metric], ".2f") for metric in METRICS}}
            for project, values in sorted(projects.items())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Compare the result with the checked-in acceptance baseline")
    args = parser.parse_args()
    with (ROOT / f"{DATASET_VERSION}.csv").open(encoding="utf-8", newline="") as stream:
        summary = summarize(stream)
    if args.verify:
        expected = json.loads((ROOT / "expected-summary.json").read_text(encoding="utf-8"))
        if summary != expected:
            parser.exit(1, "Usage summary differs from the pilot baseline.\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
