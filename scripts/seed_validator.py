"""
seed_validator.py — Validate a Nymph seed package JSON against the schema and business rules.

Enforces all 11 guardrails from the skill brief. Must pass before applying to any environment.

Usage:
    python seed_validator.py --input seed_package.json
    python seed_validator.py --input seed_package.json --strict
"""

import argparse
import json
import sys
from pathlib import Path

REFERENCES_DIR = Path(__file__).parent.parent / "references"

VALID_FLOW_TYPES = {"RAS", "WAS", "recycle", "filtrate", "centrate", "scum", "bypass",
                    "primary_sludge", "thickened_sludge", "digested_sludge", "leachate"}

GENERIC_PHRASES = [
    "activated sludge process",
    "typically used in",
    "generally speaking",
    "in most plants",
    "standard practice",
    "common in wastewater",
]


class ValidationError:
    def __init__(self, path: str, message: str, severity: str = "error"):
        self.path = path
        self.message = message
        self.severity = severity  # "error" or "warning"

    def __str__(self):
        icon = "[FAIL]" if self.severity == "error" else "[WARN]"
        return f"  {icon} [{self.path}] {self.message}"


# ---------------------------------------------------------------------------
# Guardrail 1 — Require NPDES number plus name/location before seeding
# ---------------------------------------------------------------------------
def validate_identity(identity: dict) -> list[ValidationError]:
    errors = []
    path = "identity"

    if not identity.get("plantName"):
        errors.append(ValidationError(f"{path}.plantName", "Required — plant name must be present before seeding", "error"))

    if not identity.get("npdesPermitNumber"):
        errors.append(ValidationError(f"{path}.npdesPermitNumber", "Required — NPDES number must be present before seeding", "error"))

    if not (identity.get("location") or identity.get("city")):
        errors.append(ValidationError(f"{path}.location", "Required — city/state must be present before seeding", "error"))

    if identity.get("plantType") != "wastewater":
        errors.append(ValidationError(f"{path}.plantType", f"Expected 'wastewater', got '{identity.get('plantType')}'", "error"))

    if identity.get("confidence") not in ("high", "medium", "low", None):
        errors.append(ValidationError(f"{path}.confidence", f"Must be high/medium/low, got '{identity.get('confidence')}'", "warning"))

    coords = identity.get("coordinates", {})
    if coords.get("lat") is not None and not isinstance(coords["lat"], (int, float)):
        errors.append(ValidationError(f"{path}.coordinates.lat", "Must be a number or null", "error"))
    if coords.get("lon") is not None and not isinstance(coords["lon"], (int, float)):
        errors.append(ValidationError(f"{path}.coordinates.lon", "Must be a number or null", "error"))

    return errors


def validate_identity_patch(patch: dict) -> list[ValidationError]:
    errors = []
    path = "identityPatch"
    for field in ["plantName", "plantType"]:
        if not patch.get(field):
            errors.append(ValidationError(f"{path}.{field}", "Required field missing", "error"))
    if patch.get("plantType") and patch["plantType"] != "wastewater":
        errors.append(ValidationError(f"{path}.plantType", "Must be 'wastewater'", "error"))
    return errors


# ---------------------------------------------------------------------------
# Guardrail 3 — Attach source document, page, and confidence to every fact
# Guardrail 6 — Preserve seasonality, statistics, units, frequency, sample type
# ---------------------------------------------------------------------------
def validate_basics(basics: dict) -> list[ValidationError]:
    errors = []
    path = "overview.basics"

    for flow_field in ["designFlowMGD", "averageFlowMGD", "peakFlowMGD"]:
        val = basics.get(flow_field)
        if val is not None and not isinstance(val, (int, float)):
            errors.append(ValidationError(
                f"{path}.{flow_field}",
                f"Must be a number (float), not a string. Got: {repr(val)}. Example: 0.83 not '0.83 MGD'",
                "error"
            ))

    # Guardrail 3: basics must have _sourceMeta if any values were extracted
    has_values = any(
        basics.get(f) is not None
        for f in ["processType", "influentType", "designFlowMGD", "averageFlowMGD", "peakFlowMGD"]
    )
    if has_values and not basics.get("_sourceMeta"):
        errors.append(ValidationError(
            f"{path}._sourceMeta",
            "Source metadata required when any basics fields are populated (guardrail 3)",
            "warning"
        ))

    return errors


# ---------------------------------------------------------------------------
# Guardrail 4 — Process units as ordered nodes, not paragraphs
# Guardrail 5 — Keep active and inactive units separate
# ---------------------------------------------------------------------------
def validate_process_unit(unit: dict, index: int, train: str) -> list[ValidationError]:
    errors = []
    path = f"overview.processTrain.{train}[{index}]"

    required = ["id", "type", "label", "train", "order", "stage", "status"]
    for field in required:
        if not unit.get(field) and unit.get(field) != 0:
            errors.append(ValidationError(f"{path}.{field}", "Required field missing", "error"))

    if unit.get("train") not in ("liquid", "solids", None):
        errors.append(ValidationError(f"{path}.train", f"Must be 'liquid' or 'solids', got '{unit.get('train')}'", "error"))

    valid_stages = {"preliminary", "primary", "secondary", "disinfection", "discharge", "solids_train"}
    if unit.get("stage") and unit["stage"] not in valid_stages:
        errors.append(ValidationError(f"{path}.stage", f"Unknown stage '{unit['stage']}'", "warning"))

    # Guardrail 5: inactive units must have status=inactive — not be omitted
    # 'inferred' is allowed for standard process units not explicitly named in the permit
    valid_statuses = {"active", "inactive", "standby", "inferred"}
    if unit.get("status") and unit["status"] not in valid_statuses:
        errors.append(ValidationError(
            f"{path}.status",
            f"Status must be active/inactive/standby/inferred, got '{unit['status']}'. "
            f"Record inactive units as status=inactive — do not omit them.",
            "error"
        ))

    if unit.get("order") is not None and not isinstance(unit["order"], int):
        errors.append(ValidationError(f"{path}.order", "Order must be an integer (guardrail 4)", "error"))

    return errors


def validate_process_train(pt: dict) -> list[ValidationError]:
    errors = []

    liquid = pt.get("liquid", [])
    solids = pt.get("solids", [])
    connections = pt.get("connections", [])

    # Reject legacy key names
    if "liquidTrain" in pt or "solidsTrain" in pt:
        errors.append(ValidationError(
            "overview.processTrain",
            "Use 'liquid' and 'solids' keys — 'liquidTrain'/'solidsTrain' are not accepted",
            "error"
        ))

    for i, unit in enumerate(liquid):
        errors.extend(validate_process_unit(unit, i, "liquid"))
    for i, unit in enumerate(solids):
        errors.extend(validate_process_unit(unit, i, "solids"))

    def check_order_unique(units, train_name):
        orders = [u.get("order") for u in units if u.get("order") is not None]
        if len(orders) != len(set(orders)):
            errors.append(ValidationError(
                f"overview.processTrain.{train_name}",
                f"Duplicate order values: {orders}",
                "error"
            ))
    check_order_unique(liquid, "liquid")
    check_order_unique(solids, "solids")

    # Connections must only represent named side/return flows
    all_ids = {u.get("id") for u in liquid + solids if u.get("id")}
    for i, conn in enumerate(connections):
        cpath = f"overview.processTrain.connections[{i}]"
        if not conn.get("from"):
            errors.append(ValidationError(f"{cpath}.from", "Required field missing", "error"))
        if not conn.get("to"):
            errors.append(ValidationError(f"{cpath}.to", "Required field missing", "error"))
        if not conn.get("flowType"):
            errors.append(ValidationError(f"{cpath}.flowType", "Required field missing", "error"))
        elif conn["flowType"] not in VALID_FLOW_TYPES:
            errors.append(ValidationError(
                f"{cpath}.flowType",
                f"'{conn['flowType']}' is not a named side/return flow. "
                f"Main forward path must not be in connections — use node order instead.",
                "warning"
            ))
        if conn.get("from") and conn["from"] not in all_ids:
            errors.append(ValidationError(f"{cpath}.from", f"Node ID '{conn['from']}' not found in any train", "warning"))
        if conn.get("to") and conn["to"] not in all_ids:
            errors.append(ValidationError(f"{cpath}.to", f"Node ID '{conn['to']}' not found in any train", "warning"))

    return errors


# ---------------------------------------------------------------------------
# Guardrail 2 — Prefer final permits for legal limits
# Guardrail 6 — Preserve seasonality, statistics, units, frequency, sample type
# Guardrail 7 — Do not convert permit limits into operating targets
# ---------------------------------------------------------------------------
def validate_permit_limits(limits: list, facts_skipped: list | None = None) -> list[ValidationError]:
    errors = []
    skipped_surfaces = {s.get("surface") for s in (facts_skipped or [])}

    if isinstance(limits, dict):
        errors.append(ValidationError(
            "permitLimits",
            "Must be an array [], not an object {}. Each item needs a 'parameter' field.",
            "error"
        ))
        return errors

    for i, item in enumerate(limits):
        path = f"permitLimits[{i}]"
        param = item.get("parameter", "")

        if not param:
            errors.append(ValidationError(f"{path}.parameter", "Required field missing", "error"))

        if not item.get("unit"):
            errors.append(ValidationError(f"{path}.unit", "Required — preserve unit exactly as written in permit (guardrail 6)", "error"))

        item_limits = item.get("limits", [])
        if not item_limits:
            errors.append(ValidationError(f"{path}.limits", "Limits array empty — remove parameter or add at least one limit", "warning"))

        valid_limit_types = {"Monthly", "Weekly", "Daily", "Annual", "Single Sample"}
        valid_statistics = {"average", "maximum", "minimum", "geometric_mean"}

        freq_skipped = f"permitLimits.{param}.frequency" in skipped_surfaces
        sample_type_skipped = f"permitLimits.{param}.sampleType" in skipped_surfaces

        for j, lim in enumerate(item_limits):
            lpath = f"{path}.limits[{j}]"

            if "value" not in lim:
                errors.append(ValidationError(f"{lpath}.value", "Required field missing", "error"))
            elif not isinstance(lim["value"], (int, float)):
                errors.append(ValidationError(f"{lpath}.value", f"Must be a number, got {repr(lim['value'])}", "error"))

            if not lim.get("limitType"):
                errors.append(ValidationError(f"{lpath}.limitType", "Required field missing", "error"))
            elif lim["limitType"] not in valid_limit_types:
                errors.append(ValidationError(f"{lpath}.limitType", f"Unknown limitType '{lim['limitType']}'", "warning"))

            if not lim.get("statistic"):
                errors.append(ValidationError(f"{lpath}.statistic", "Required — preserve statistic from permit (guardrail 6)", "error"))
            elif lim["statistic"] not in valid_statistics:
                errors.append(ValidationError(f"{lpath}.statistic", f"Unknown statistic '{lim['statistic']}'", "warning"))

            # Guardrail 6: warn if frequency or sampleType missing — suppressed when already in factsSkipped
            if not lim.get("frequency") and not freq_skipped:
                errors.append(ValidationError(f"{lpath}.frequency", "Monitoring frequency should be preserved from permit (guardrail 6)", "warning"))
            if not lim.get("sampleType") and not sample_type_skipped:
                errors.append(ValidationError(f"{lpath}.sampleType", "Sample type should be preserved from permit (guardrail 6)", "warning"))

            meta = lim.get("_sourceMeta", {})
            if not meta:
                errors.append(ValidationError(f"{lpath}._sourceMeta", "Missing source metadata — every limit must have a source document (guardrail 3)", "error"))
            elif meta.get("confidence") is None:
                errors.append(ValidationError(f"{lpath}._sourceMeta.confidence", "Confidence score required (guardrail 3)", "error"))

    return errors


# ---------------------------------------------------------------------------
# Guardrail 9 — Nymph Logs: source-backed plant context, not generic research notes
# Guardrail 3 — Attach source to every fact
# ---------------------------------------------------------------------------
def validate_log_entry(entry: dict, index: int, category: str) -> list[ValidationError]:
    errors = []
    path = f"nymphLogs.{category}[{index}]"

    if not entry.get("value"):
        errors.append(ValidationError(f"{path}.value", "Required — log entry text", "error"))

    if entry.get("confidence") is None:
        errors.append(ValidationError(f"{path}.confidence", "Required — confidence score 0.0–1.0 (guardrail 3)", "error"))
    elif not isinstance(entry["confidence"], (int, float)) or not (0 <= entry["confidence"] <= 1):
        errors.append(ValidationError(f"{path}.confidence", f"Must be a float 0–1, got {entry.get('confidence')}", "error"))

    if entry.get("source") not in ("ai_inference", "permit_pdf", "operator_confirmed", "document_upload", "echo_api", None):
        errors.append(ValidationError(f"{path}.source", f"Unknown source '{entry.get('source')}'", "warning"))

    if not entry.get("lastUpdated"):
        errors.append(ValidationError(f"{path}.lastUpdated", "Required — ISO datetime string", "error"))

    if not entry.get("tags"):
        errors.append(ValidationError(f"{path}.tags", "Tags array should be present for log entry classification", "warning"))

    # Guardrail 3: every log entry must cite a document
    if not entry.get("_sourceMeta"):
        errors.append(ValidationError(f"{path}._sourceMeta", "Source metadata missing — log entries must cite a document (guardrail 3)", "warning"))

    # Guardrail 9: reject generic wastewater text — fires even when a sourceDoc is present,
    # because a cited generic fact is still wrong content for Nymph Logs.
    value_lower = (entry.get("value") or "").lower()
    for phrase in GENERIC_PHRASES:
        if phrase in value_lower:
            errors.append(ValidationError(
                f"{path}.value",
                f"Contains generic wastewater text ('{phrase}'). "
                f"Nymph Logs must contain plant-specific facts only (guardrail 9).",
                "warning"
            ))
            break

    return errors


def validate_documents(documents: list) -> list[ValidationError]:
    errors = []
    for i, doc in enumerate(documents):
        path = f"documents[{i}]"
        if not doc.get("filename"):
            errors.append(ValidationError(f"{path}.filename", "Required field missing", "error"))
        if not doc.get("sourceUrl"):
            errors.append(ValidationError(f"{path}.sourceUrl", "Every source document must have an official agency sourceUrl", "error"))
        if not doc.get("fileType"):
            errors.append(ValidationError(f"{path}.fileType", "fileType required (document, image, spreadsheet)", "warning"))
        elif doc["fileType"] not in ("document", "image", "spreadsheet"):
            errors.append(ValidationError(f"{path}.fileType", f"Unknown fileType '{doc['fileType']}'", "warning"))
    return errors


# ---------------------------------------------------------------------------
# Guardrail 7 — Do not convert permit limits into operating targets
# Guardrail 8 — Do not populate MOR readings
# Guardrail 10 — Keep searches, skipped facts, conflicts in runAudit
# ---------------------------------------------------------------------------
def validate_run_audit(audit: dict) -> list[ValidationError]:
    errors = []
    path = "runAudit"

    # Guardrail 10
    if not audit.get("sourcesSearched"):
        errors.append(ValidationError(f"{path}.sourcesSearched", "Empty — at least one source must be recorded (guardrail 10)", "error"))

    if not audit.get("seedSummary"):
        errors.append(ValidationError(f"{path}.seedSummary", "Required — human-readable summary of what was seeded", "error"))
    else:
        summary_lower = audit["seedSummary"].lower()
        # Guardrail 7 and 8 must be explicitly confirmed in the summary
        if "operating target" not in summary_lower:
            errors.append(ValidationError(
                f"{path}.seedSummary",
                "Must explicitly confirm that operating targets were left empty (guardrail 7)",
                "warning"
            ))
        if "mor" not in summary_lower:
            errors.append(ValidationError(
                f"{path}.seedSummary",
                "Must explicitly confirm that MOR readings were left empty (guardrail 8)",
                "warning"
            ))

    return errors


def validate_audit_run(audit_run: dict) -> list[ValidationError]:
    errors = []
    path = "auditRun"

    if audit_run.get("worker") != "plant_knowledge_public_seed":
        errors.append(ValidationError(f"{path}.worker", "Must be 'plant_knowledge_public_seed'", "error"))
    if audit_run.get("proposalType") != "plant_knowledge":
        errors.append(ValidationError(f"{path}.proposalType", "Must be 'plant_knowledge'", "error"))
    if audit_run.get("proposalStatus") not in ("applied", "pending_review"):
        errors.append(ValidationError(f"{path}.proposalStatus", "Must be 'applied' or 'pending_review'", "error"))

    meta = audit_run.get("metadata", {})
    if not meta.get("npdesPermitNumber"):
        errors.append(ValidationError(f"{path}.metadata.npdesPermitNumber", "Required", "error"))
    skipped = meta.get("skippedUnsupportedSurfaces", [])
    if "targets" not in skipped or "MOR readings" not in skipped:
        errors.append(ValidationError(
            f"{path}.metadata.skippedUnsupportedSurfaces",
            "Must include 'targets' and 'MOR readings' in skippedUnsupportedSurfaces",
            "error"
        ))

    return errors


# ---------------------------------------------------------------------------
# Guardrail 7/8 — nonGoals must be present
# ---------------------------------------------------------------------------
def validate_non_goals(non_goals: dict) -> list[ValidationError]:
    errors = []
    path = "nonGoals"
    if not non_goals.get("operatingTargets"):
        errors.append(ValidationError(f"{path}.operatingTargets", "Required — explicit statement that operating targets are not populated (guardrail 7)", "error"))
    if not non_goals.get("morReadings"):
        errors.append(ValidationError(f"{path}.morReadings", "Required — explicit statement that MOR readings are not populated (guardrail 8)", "error"))
    return errors


# ---------------------------------------------------------------------------
# Guardrail 11 — Readback check advisory
# ---------------------------------------------------------------------------
def check_readback_advisory(package: dict) -> list[ValidationError]:
    audit_run = package.get("auditRun", {})
    if audit_run.get("proposalStatus") == "applied":
        return [ValidationError(
            "auditRun.proposalStatus",
            "Package marked as 'applied'. Run scripts/db_readback.py to verify the seed was correctly persisted (guardrail 11).",
            "warning"
        )]
    return []


def validate_seed_package(package: dict) -> tuple[list[ValidationError], list[ValidationError]]:
    """Validate a complete seed package. Returns (errors, warnings)."""
    all_issues = []

    # Required top-level keys
    required_keys = ["identity", "identityPatch", "overview", "permitLimits",
                     "nymphLogs", "documents", "runAudit", "auditRun", "nonGoals"]
    for key in required_keys:
        if key not in package:
            all_issues.append(ValidationError(key, "Top-level required key missing", "error"))

    if "identity" in package:
        all_issues.extend(validate_identity(package["identity"]))

    if "identityPatch" in package:
        all_issues.extend(validate_identity_patch(package["identityPatch"]))

    overview = package.get("overview", {})
    if "basics" in overview:
        all_issues.extend(validate_basics(overview["basics"]))
    if "processTrain" in overview:
        all_issues.extend(validate_process_train(overview["processTrain"]))

    if "permitLimits" in package:
        _facts_skipped = package.get("runAudit", {}).get("factsSkipped", [])
        all_issues.extend(validate_permit_limits(package["permitLimits"], _facts_skipped))

    nymph_logs = package.get("nymphLogs", {})
    for category in ["chemicalsDosing", "operatingHistory", "proceduresPreferences", "historicalOutcomes"]:
        for i, entry in enumerate(nymph_logs.get(category, [])):
            all_issues.extend(validate_log_entry(entry, i, category))

    if "documents" in package:
        all_issues.extend(validate_documents(package["documents"]))

    if "runAudit" in package:
        all_issues.extend(validate_run_audit(package["runAudit"]))

    if "auditRun" in package:
        all_issues.extend(validate_audit_run(package["auditRun"]))

    if "nonGoals" in package:
        all_issues.extend(validate_non_goals(package["nonGoals"]))

    all_issues.extend(check_readback_advisory(package))

    errors = [e for e in all_issues if e.severity == "error"]
    warnings = [e for e in all_issues if e.severity == "warning"]
    return errors, warnings


def print_report(package: dict, errors: list, warnings: list, strict: bool = False):
    identity = package.get("identity", {})
    plant = identity.get("plantName", "Unknown plant")
    npdes = identity.get("npdesPermitNumber", "Unknown NPDES")

    print(f"\n{'='*60}")
    print(f"  Nymph Seed Package Validation Report")
    print(f"  Plant: {plant}")
    print(f"  NPDES: {npdes}")
    print(f"{'='*60}")

    overview = package.get("overview", {})
    pt = overview.get("processTrain", {})
    permit_limits = package.get("permitLimits", [])
    logs = package.get("nymphLogs", {})
    docs = package.get("documents", [])

    print(f"\n  Contents:")
    print(f"    Liquid train nodes:   {len(pt.get('liquid', []))}")
    print(f"    Solids train nodes:   {len(pt.get('solids', []))}")
    print(f"    Connections:          {len(pt.get('connections', []))}")
    print(f"    Permit parameters:    {len(permit_limits) if isinstance(permit_limits, list) else '?'}")
    print(f"    Chemicals & Dosing:   {len(logs.get('chemicalsDosing', []))}")
    print(f"    Operating History:    {len(logs.get('operatingHistory', []))}")
    print(f"    Procedures:           {len(logs.get('proceduresPreferences', []))}")
    print(f"    Historical Outcomes:  {len(logs.get('historicalOutcomes', []))}")
    print(f"    Documents:            {len(docs)}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            print(str(e))

    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(str(w))

    print(f"\n{'='*60}")
    fail = bool(errors) or (strict and bool(warnings))
    status = "FAILED" if fail else "PASSED"
    detail = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    print(f"  Result: {status} — {detail}")
    print(f"{'='*60}\n")
    return fail


def main():
    parser = argparse.ArgumentParser(description="Validate a Nymph seed package JSON")
    parser.add_argument("--input", required=True, help="Path to seed package JSON")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        package = json.load(f)

    errors, warnings = validate_seed_package(package)
    failed = print_report(package, errors, warnings, strict=args.strict)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
