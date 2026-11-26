"""Validation and comparison of participant-submitted research pilot records."""
import hashlib
import json
import re
from decimal import Decimal

MAX_BYTES = 32 * 1024
METRICS = ("vcpu_hours", "memory_gib_hours")
PILOT_INPUTS = {"workspace-usage-v1.csv", "expected-summary.json", "summarize_usage.py"}
HASH_RE = re.compile(r"[a-f0-9]{64}")
AMOUNT_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})\.[0-9]{2}")
PROJECT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Execution records cannot contain duplicate JSON fields")
    return fields


def _metrics(values):
    return all(isinstance(values.get(key), str) and AMOUNT_RE.fullmatch(values[key]) for key in METRICS)


def parse(raw, expected_commit):
    if len(raw) > MAX_BYTES:
        raise ValueError("Execution record exceeds 32 KiB")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_fields)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Upload a valid JSON execution record") from error
    fields = {"schema_version", "source_commit", "inputs", "summary_sha256", "summary"}
    if (not isinstance(data, dict) or set(data) != fields
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise ValueError("Expected a version 1 pilot execution record")
    if data["source_commit"] != expected_commit:
        raise ValueError("Execution record commit does not match the blueprint")
    inputs = data["inputs"]
    if (not isinstance(inputs, dict) or set(inputs) != PILOT_INPUTS
            or any(not isinstance(value, str) or not HASH_RE.fullmatch(value) for value in inputs.values())):
        raise ValueError("Expected SHA-256 hashes for the three pilot inputs")

    summary = data["summary"]
    if (not isinstance(summary, dict) or set(summary) != {"dataset_version", "totals", "projects"}
            or summary["dataset_version"] != "workspace-usage-v1"):
        raise ValueError("Expected a workspace usage v1 summary")
    totals, projects = summary["totals"], summary["projects"]
    if not isinstance(totals, dict) or set(totals) != set(METRICS) or not _metrics(totals):
        raise ValueError("Invalid workspace usage totals")
    if not isinstance(projects, list) or not 1 <= len(projects) <= 100:
        raise ValueError("Expected between one and 100 project totals")
    project_ids = set()
    sums = dict.fromkeys(METRICS, Decimal(0))
    for row in projects:
        if (not isinstance(row, dict) or set(row) != {"project_id", *METRICS}
                or not isinstance(row["project_id"], str) or not PROJECT_RE.fullmatch(row["project_id"])
                or row["project_id"] in project_ids or not _metrics(row)):
            raise ValueError("Invalid or duplicate project totals")
        project_ids.add(row["project_id"])
        for metric in METRICS:
            sums[metric] += Decimal(row[metric])
    if any(sums[metric] != Decimal(totals[metric]) for metric in METRICS):
        raise ValueError("Project totals do not add up to the reported totals")
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if data["summary_sha256"] != hashlib.sha256(encoded).hexdigest():
        raise ValueError("Summary hash does not match the uploaded result")
    return data


def comparison(records):
    attached = [r for r in records if r["execution"] is not None]
    participants = len({r["owner"] for r in attached})
    if not attached:
        return {"state": "empty", "records": 0, "participants": 0}
    first = attached[0]["execution"]
    fields = ("source_commit", "inputs", "summary_sha256")
    differences = [field for field in fields if any(r["execution"][field] != first[field] for r in attached)]
    state = "different" if differences else "match" if participants >= 2 else "waiting"
    return {"state": state, "records": len(attached), "participants": participants, "differences": differences}
