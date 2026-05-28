"""
provenance_linter.py — Check that every populated fact in a seed package has source metadata.

Enforces guardrail 3: attach source document, page number, and confidence to every extracted fact.
Run after seed_validator.py and before applying to production.

Usage:
    python provenance_linter.py --input seed_package.json
    python provenance_linter.py --input seed_package.json --strict
"""

import argparse
import json
import sys
from pathlib import Path


class ProvenanceIssue:
    def __init__(self, path: str, message: str, severity: str = "error"):
        self.path = path
        self.message = message
        self.severity = severity

    def __str__(self):
        icon = "[FAIL]" if self.severity == "error" else "[WARN]"
        return f"  {icon} [{self.path}] {self.message}"


def check_source_meta(meta: dict, path: str, require_page: bool = False) -> list[ProvenanceIssue]:
    issues = []
    if not meta:
        issues.append(ProvenanceIssue(path, "Missing _sourceMeta", "error"))
        return issues

    if not meta.get("sourceDoc"):
        issues.append(ProvenanceIssue(f"{path}.sourceDoc", "Required — source document identifier", "error"))

    conf = meta.get("confidence")
    if conf is None:
        issues.append(ProvenanceIssue(f"{path}.confidence", "Required — confidence score 0.0–1.0", "error"))
    elif not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        issues.append(ProvenanceIssue(f"{path}.confidence", f"Must be 0.0–1.0, got {conf}", "error"))
    elif conf < 0.40:
        issues.append(ProvenanceIssue(
            f"{path}.confidence",
            f"Confidence {conf} is below 0.40 — facts below this threshold should be left empty, not included",
            "error"
        ))

    if require_page and meta.get("sourcePage") is None:
        issues.append(ProvenanceIssue(f"{path}.sourcePage", "Page number should be present for permit facts", "warning"))

    return issues


def lint_basics(basics: dict) -> list[ProvenanceIssue]:
    issues = []
    has_values = any(
        basics.get(f) is not None
        for f in ["processType", "influentType", "designFlowMGD", "averageFlowMGD", "peakFlowMGD"]
    )
    if has_values:
        meta = basics.get("_sourceMeta", {})
        issues.extend(check_source_meta(meta, "overview.basics._sourceMeta", require_page=False))
    return issues


def lint_process_units(pt: dict) -> list[ProvenanceIssue]:
    issues = []
    for train_key in ["liquid", "solids"]:
        for i, unit in enumerate(pt.get(train_key, [])):
            path = f"overview.processTrain.{train_key}[{i}]"
            if not unit.get("notes"):
                issues.append(ProvenanceIssue(
                    f"{path}.notes",
                    f"Node '{unit.get('label', '?')}' has no source note. "
                    f"Add: 'Source: <doc>, <section>, p.<page>'",
                    "warning"
                ))
    return issues


def lint_permit_limits(limits: list) -> list[ProvenanceIssue]:
    issues = []
    if not isinstance(limits, list):
        issues.append(ProvenanceIssue("permitLimits", "Must be an array", "error"))
        return issues

    for i, item in enumerate(limits):
        for j, lim in enumerate(item.get("limits", [])):
            path = f"permitLimits[{i}].limits[{j}]"
            meta = lim.get("_sourceMeta", {})
            issues.extend(check_source_meta(meta, f"{path}._sourceMeta", require_page=True))

    return issues


def lint_nymph_logs(logs: dict) -> list[ProvenanceIssue]:
    issues = []
    for category in ["chemicalsDosing", "operatingHistory", "proceduresPreferences", "historicalOutcomes"]:
        for i, entry in enumerate(logs.get(category, [])):
            path = f"nymphLogs.{category}[{i}]"
            meta = entry.get("_sourceMeta", {})
            issues.extend(check_source_meta(meta, f"{path}._sourceMeta", require_page=False))

            # Source URL should be present for public-source entries
            if entry.get("source") == "ai_inference" and not meta.get("sourceUrl"):
                issues.append(ProvenanceIssue(
                    f"{path}._sourceMeta.sourceUrl",
                    "Source URL recommended for ai_inference log entries",
                    "warning"
                ))
    return issues


def lint_seed_package(package: dict) -> tuple[list[ProvenanceIssue], list[ProvenanceIssue]]:
    all_issues = []

    overview = package.get("overview", {})
    if "basics" in overview:
        all_issues.extend(lint_basics(overview["basics"]))
    if "processTrain" in overview:
        all_issues.extend(lint_process_units(overview["processTrain"]))

    if "permitLimits" in package:
        all_issues.extend(lint_permit_limits(package["permitLimits"]))

    if "nymphLogs" in package:
        all_issues.extend(lint_nymph_logs(package["nymphLogs"]))

    errors = [i for i in all_issues if i.severity == "error"]
    warnings = [i for i in all_issues if i.severity == "warning"]
    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Lint provenance metadata in a Nymph seed package")
    parser.add_argument("--input", required=True, help="Path to seed package JSON")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        package = json.load(f)

    identity = package.get("identity", {})
    plant = identity.get("plantName", "Unknown")
    npdes = identity.get("npdesPermitNumber", "Unknown")

    print(f"\n{'='*60}")
    print(f"  Nymph Provenance Lint Report")
    print(f"  Plant: {plant}")
    print(f"  NPDES: {npdes}")
    print(f"{'='*60}")

    errors, warnings = lint_seed_package(package)

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            print(str(e))
    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(str(w))

    print(f"\n{'='*60}")
    fail = bool(errors) or (args.strict and bool(warnings))
    status = "FAILED" if fail else "PASSED"
    print(f"  Result: {status} — {len(errors)} error(s), {len(warnings)} warning(s)")
    print(f"{'='*60}\n")

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
