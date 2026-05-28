"""
db_readback.py — Seed package readback report.

Reads a Nymph seed package JSON file and prints a structured summary:
  - Facts by surface (count and sample values)
  - Facts by evidenceClass
  - Facts flagged for human review (low confidence, missing required fields)
  - Conflicts
  - factsSkipped
  - Final verdict: READY FOR REVIEW or NEEDS ATTENTION

Usage:
    python db_readback.py <seed_package.json>
    python db_readback.py AL0056626_seed_package.json
"""

import argparse
import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _div(label: str = "", width: int = 60) -> None:
    if label:
        pad = max(0, width - len(label) - 4)
        print(f"\n-- {label} {'-' * pad}")
    else:
        print("-" * width)


def _count_by_evidence(package: dict) -> dict[str, int]:
    counts: dict[str, int] = {}

    def _walk(obj):
        if isinstance(obj, dict):
            ec = obj.get("evidenceClass")
            if ec:
                counts[ec] = counts.get(ec, 0) + 1
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(package)
    return counts


def _review_flags(package: dict) -> list[str]:
    flags = []

    # basics
    basics = package.get("overview", {}).get("basics", {})
    for field in ["processType", "influentType", "designFlowMGD"]:
        if not basics.get(field):
            flags.append(f"basics.{field} is null — verify manually")

    # permit limits: missing required fields
    for group in package.get("permitLimits", []):
        param = group.get("parameter", "?")
        for i, lim in enumerate(group.get("limits", [])):
            if not lim.get("frequency"):
                flags.append(f"permitLimits[{param}].limits[{i}].frequency is null")
            if not lim.get("sampleType"):
                flags.append(f"permitLimits[{param}].limits[{i}].sampleType is null")
            conf = lim.get("confidence")
            if conf is not None and float(conf) < 0.8:
                flags.append(f"permitLimits[{param}].limits[{i}].confidence={conf} — low confidence")

    # process train nodes with inferred status
    for node in package.get("overview", {}).get("processTrain", {}).get("liquid", []):
        if node.get("status") == "inferred":
            flags.append(f"processTrain.liquid '{node.get('label')}' is inferred — confirm with plant")

    # nymph logs: low confidence entries
    for cat, entries in package.get("nymphLogs", {}).items():
        for entry in entries:
            conf = entry.get("confidence")
            if conf is not None and float(conf) < 0.75:
                flags.append(
                    f"nymphLogs.{cat}: low confidence ({conf}) — '{str(entry.get('value',''))[:80]}'"
                )

    # identity: missing coordinates
    coords = package.get("identity", {}).get("coordinates", {})
    if not coords.get("lat") or not coords.get("lon"):
        flags.append("identity.coordinates lat/lon are null")

    return flags


def main():
    parser = argparse.ArgumentParser(description="Nymph seed package readback report")
    parser.add_argument("path", help="Path to seed package JSON file")
    args = parser.parse_args()

    try:
        pkg = _load(args.path)
    except FileNotFoundError:
        print(f"ERROR: File not found: {args.path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON — {exc}", file=sys.stderr)
        sys.exit(1)

    identity = pkg.get("identity", {})
    id_patch = pkg.get("identityPatch", {})
    basics = pkg.get("overview", {}).get("basics", {})
    pt = pkg.get("overview", {}).get("processTrain", {})
    permit_limits = pkg.get("permitLimits", [])
    nymph_logs = pkg.get("nymphLogs", {})
    documents = pkg.get("documents", [])
    run_audit = pkg.get("runAudit", {})
    audit_run = pkg.get("auditRun", {})

    print("=" * 60)
    print("  NYMPH SEED PACKAGE READBACK REPORT")
    print("=" * 60)
    print(f"  File:   {Path(args.path).name}")
    print(f"  Plant:  {identity.get('plantName')}")
    print(f"  NPDES:  {identity.get('npdesPermitNumber')}")
    print(f"  City:   {identity.get('city')}, {identity.get('state')} {identity.get('zip', '')}")
    print(f"  Status: {audit_run.get('proposalStatus')}")

    # ------------------------------------------------------------------ #
    _div("FACTS BY SURFACE")
    liquid_nodes = pt.get("liquid", [])
    solids_nodes = pt.get("solids", [])
    conn_nodes = pt.get("connections", [])
    print(f"  overview.basics          — processType={basics.get('processType')}, "
          f"designFlow={basics.get('designFlowMGD')} MGD, avgFlow={basics.get('averageFlowMGD')} MGD")
    print(f"  processTrain.liquid      — {len(liquid_nodes)} node(s)")
    for n in liquid_nodes:
        print(f"    [{n.get('order', 0):02d}] {n.get('label')} ({n.get('type')}) "
              f"— {n.get('status')} / {n.get('evidenceClass','?')}")
    print(f"  processTrain.solids      — {len(solids_nodes)} node(s)")
    for n in solids_nodes:
        print(f"    [{n.get('order', 0):02d}] {n.get('label')} ({n.get('type')}) "
              f"— {n.get('status')} / {n.get('evidenceClass','?')}")
    print(f"  processTrain.connections — {len(conn_nodes)} connection(s)")
    print(f"  permitLimits             — {len(permit_limits)} parameter group(s)")
    for grp in permit_limits:
        lims = grp.get("limits", [])
        spoint = grp.get("samplePoint") or (lims[0].get("samplePoint") if lims else None)
        print(f"    {grp.get('parameter')} ({grp.get('unit')}) outfall={grp.get('outfall')} "
              f"samplePoint={spoint} — {len(lims)} limit row(s)")
        for lim in lims[:3]:
            print(f"      [{lim.get('limitType')} {lim.get('statistic')}] "
                  f"{lim.get('qualifier','')} {lim.get('value')} {grp.get('unit','')} "
                  f"freq={lim.get('frequency')} type={lim.get('sampleType')} "
                  f"seasonal={lim.get('seasonality','?')} "
                  f"ec={lim.get('evidenceClass','?')} conf={lim.get('confidence','?')}")
        if len(lims) > 3:
            print(f"      ... and {len(lims) - 3} more rows")

    total_logs = sum(len(v) for v in nymph_logs.values())
    print(f"  nymphLogs                — {total_logs} total entries")
    for cat, entries in nymph_logs.items():
        print(f"    {cat}: {len(entries)}")
        for e in entries[:2]:
            print(f"      [{e.get('source','?')}/{e.get('evidenceClass','?')} "
                  f"conf={e.get('confidence','?')}] {str(e.get('value',''))[:100]}")
    print(f"  documents                — {len(documents)} file(s)")
    for doc in documents:
        print(f"    {doc.get('filename')} pages={doc.get('pageCount')} mime={doc.get('mimeType')}")
    print(f"  identity.receivingWaters — {len(identity.get('receivingWaters', []))} entry/entries")
    for rw in identity.get("receivingWaters", []):
        print(f"    {rw.get('name','(no name)')} classification={rw.get('classification')} "
              f"source={rw.get('source')}")
    print(f"  identity.outfalls        — {len(identity.get('outfalls', []))} entry/entries")
    for of in identity.get("outfalls", []):
        print(f"    id={of.get('id')} receivingWater={of.get('receivingWater')}")

    # ------------------------------------------------------------------ #
    _div("FACTS BY EVIDENCE CLASS")
    ec_counts = _count_by_evidence(pkg)
    for ec, cnt in sorted(ec_counts.items(), key=lambda x: -x[1]):
        print(f"  {ec:<35} {cnt:>4} occurrence(s)")

    # ------------------------------------------------------------------ #
    _div("IDENTITY PATCH (plants table write)")
    for k, v in id_patch.items():
        print(f"  {k}: {v}")

    # ------------------------------------------------------------------ #
    _div("CONFLICTS")
    conflicts = run_audit.get("conflicts", [])
    if conflicts:
        for c in conflicts:
            print(f"  [{c.get('field')}] {c.get('values')} -> {c.get('resolved')}")
    else:
        print("  (none)")

    # ------------------------------------------------------------------ #
    _div("FACTS SKIPPED")
    skipped = run_audit.get("factsSkipped", [])
    if skipped:
        for s in skipped:
            print(f"  {s.get('surface')}")
            print(f"    {s.get('reason','')[:120]}")
    else:
        print("  (none)")

    # ------------------------------------------------------------------ #
    _div("HUMAN REVIEW FLAGS")
    flags = _review_flags(pkg)
    if flags:
        for f in flags:
            print(f"  !  {f}")
    else:
        print("  (none — all required fields populated)")

    # ------------------------------------------------------------------ #
    _div()
    needs_attention = bool(flags) or bool(conflicts)
    verdict = "NEEDS ATTENTION" if needs_attention else "READY FOR REVIEW"
    print(f"\n  VERDICT: {verdict}")
    if needs_attention:
        print(f"  ({len(flags)} flag(s), {len(conflicts)} conflict(s) require human review before applying)")
    print("=" * 60 + "\n")

    sys.exit(1 if needs_attention else 0)


if __name__ == "__main__":
    main()
