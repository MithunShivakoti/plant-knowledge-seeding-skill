"""
db_apply.py — Apply a Nymph seed package to the Firestore database.

Writes to four Firestore surfaces:
  1. plants/{npdes}            — identityPatch fields (name, city, state, zip, coordinates, licensed)
  2. plant_profiles/{npdes}    — profile_data (basics, processTrain, permitLimits, nymphLogs)
  3. plant_files/{npdes}/files/{filename} — each document from documents[]
  4. agent_runs (auto-ID)      — auditRun metadata

Usage:
    python db_apply.py AL0056626_seed_package.json
    python db_apply.py AL0056626_seed_package.json --dry-run
    python db_apply.py AL0056626_seed_package.json --force

Options:
    --dry-run   Print every write that would be executed without touching the DB.
    --force     Skip confirmation prompt (for scripted pipelines).
    --merge     Merge into existing documents instead of overwriting (default: overwrite).

Environment variables (override collection paths):
    NYMPH_PLANTS_COLLECTION        default: plants
    NYMPH_PROFILES_COLLECTION      default: plant_profiles
    NYMPH_FILES_COLLECTION         default: plant_files
    NYMPH_RUNS_COLLECTION          default: agent_runs
    GCP_CREDENTIALS_PATH           default: ../gcp_credentials.json relative to this script
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Collection name configuration
# ---------------------------------------------------------------------------
PLANTS_COLL    = os.environ.get("NYMPH_PLANTS_COLLECTION",   "plants")
PROFILES_COLL  = os.environ.get("NYMPH_PROFILES_COLLECTION", "plant_profiles")
FILES_COLL     = os.environ.get("NYMPH_FILES_COLLECTION",    "plant_files")
RUNS_COLL      = os.environ.get("NYMPH_RUNS_COLLECTION",     "agent_runs")

_DEFAULT_CREDS = Path(__file__).parent.parent / "gcp_credentials.json"
GCP_CREDS_PATH = os.environ.get("GCP_CREDENTIALS_PATH", str(_DEFAULT_CREDS))


# ---------------------------------------------------------------------------
# Firestore client (lazy — only imported if not dry-run)
# ---------------------------------------------------------------------------
def _get_db():
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError:
        print("ERROR: google-cloud-firestore not installed.", file=sys.stderr)
        print("  pip install google-cloud-firestore", file=sys.stderr)
        sys.exit(1)

    if not Path(GCP_CREDS_PATH).exists():
        print(f"ERROR: GCP credentials not found at {GCP_CREDS_PATH}", file=sys.stderr)
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_file(
        GCP_CREDS_PATH,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return firestore.Client(credentials=creds, project=creds.project_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plants_doc(pkg: dict) -> dict:
    """Build the plants table write object from identityPatch + coordinates."""
    patch = pkg.get("identityPatch", {})
    identity = pkg.get("identity", {})
    doc = {
        "plantName":  patch.get("plantName") or identity.get("plantName"),
        "city":       patch.get("city")      or identity.get("city"),
        "state":      patch.get("state")     or identity.get("state"),
        "zip":        patch.get("zip")       or identity.get("zip"),
        "plantType":  patch.get("plantType", "wastewater"),
        "updatedAt":  _now(),
        "seedSource": "plant_knowledge_public_seed",
    }
    coords = identity.get("coordinates", {})
    if coords.get("lat") and coords.get("lon"):
        doc["coordinates"] = {"lat": coords["lat"], "lon": coords["lon"]}
    if patch.get("owner"):
        doc["owner"] = patch["owner"]
    if "licensed" in patch:
        doc["licensed"] = patch["licensed"]
    return {k: v for k, v in doc.items() if v is not None}


def _profile_doc(pkg: dict) -> dict:
    """Build the plant_profiles profile_data write object."""
    overview = pkg.get("overview", {})
    return {
        "profile_data": {
            "basics":       overview.get("basics", {}),
            "processTrain": overview.get("processTrain", {}),
            "permitLimits": pkg.get("permitLimits", []),
            "nymphLogs":    pkg.get("nymphLogs", {}),
        },
        "updatedAt":  _now(),
        "seedSource": "plant_knowledge_public_seed",
        "npdes":      pkg.get("identity", {}).get("npdesPermitNumber"),
    }


def _file_docs(pkg: dict, npdes: str) -> list[tuple[str, dict]]:
    """Return [(doc_id, doc_data), ...] for each source document."""
    results = []
    for doc in pkg.get("documents", []):
        filename = doc.get("filename", "unknown")
        doc_id = filename.replace("/", "_").replace("\\", "_")
        data = {
            "filename":    filename,
            "fileType":    doc.get("fileType", "document"),
            "tags":        doc.get("tags", []),
            "sourceUrl":   doc.get("sourceUrl") or doc.get("url", ""),
            "mimeType":    doc.get("mimeType", "application/pdf"),
            "pageCount":   doc.get("pageCount"),
            "npdes":       npdes,
            "addedAt":     _now(),
            "seedSource":  "plant_knowledge_public_seed",
        }
        results.append((doc_id, {k: v for k, v in data.items() if v is not None}))
    return results


def _run_doc(pkg: dict, npdes: str, package_path: str) -> dict:
    """Build the agent_runs row from auditRun."""
    audit = pkg.get("auditRun", {})
    run_audit = pkg.get("runAudit", {})
    return {
        "worker":         audit.get("worker", "plant_knowledge_public_seed"),
        "proposalType":   audit.get("proposalType", "plant_knowledge"),
        "proposalStatus": audit.get("proposalStatus", "pending_review"),
        "npdes":          npdes,
        "packageFile":    Path(package_path).name,
        "metadata": {
            "npdesPermitNumber":          npdes,
            "sourceDocuments":            audit.get("metadata", {}).get("sourceDocuments", []),
            "conflicts":                  run_audit.get("conflicts", []),
            "skippedUnsupportedSurfaces": audit.get("metadata", {}).get("skippedUnsupportedSurfaces", []),
            "seedSummary":                run_audit.get("seedSummary", ""),
            "factsSkipped":               len(run_audit.get("factsSkipped", [])),
        },
        "createdAt": _now(),
    }


# ---------------------------------------------------------------------------
# Print helpers for dry-run output
# ---------------------------------------------------------------------------
def _print_write(op: str, path: str, data: dict) -> None:
    print(f"\n  [{op}] {path}")
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            n = len(v) if isinstance(v, list) else len(v)
            print(f"    {k}: <{type(v).__name__} with {n} item(s)>")
        else:
            print(f"    {k}: {v!r}")


# ---------------------------------------------------------------------------
# Main apply logic
# ---------------------------------------------------------------------------
def apply(package_path: str, dry_run: bool, force: bool, merge: bool) -> None:
    pkg = _load(package_path)
    identity = pkg.get("identity", {})
    npdes = (
        pkg.get("identityPatch", {}).get("npdesPermitNumber")
        or identity.get("npdesPermitNumber")
        or Path(package_path).stem.split("_")[0]
    )
    plant_name = identity.get("plantName", npdes)
    proposal_status = pkg.get("auditRun", {}).get("proposalStatus", "pending_review")

    print(f"\n{'='*64}")
    print(f"  Nymph DB Apply — {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Plant:  {plant_name}")
    print(f"  NPDES:  {npdes}")
    print(f"  Status: {proposal_status}")
    print(f"  Mode:   {'merge' if merge else 'set (overwrite)'}")
    print(f"{'='*64}\n")

    if proposal_status != "applied" and not dry_run:
        print(f"WARNING: proposalStatus is '{proposal_status}', not 'applied'.")
        print("  Run seed_runner with --production to mark as ready, or")
        print("  manually set proposalStatus='applied' in the seed package.\n")
        if not force:
            ans = input("  Proceed anyway? [y/N] ").strip().lower()
            if ans != "y":
                print("Aborted.")
                sys.exit(0)

    # --- Build all write payloads ---
    plants_data  = _plants_doc(pkg)
    profile_data = _profile_doc(pkg)
    file_docs    = _file_docs(pkg, npdes)
    run_data     = _run_doc(pkg, npdes, package_path)

    writes = [
        ("SET", f"{PLANTS_COLL}/{npdes}",           plants_data),
        ("SET", f"{PROFILES_COLL}/{npdes}",          profile_data),
    ]
    for doc_id, fdata in file_docs:
        writes.append(("SET", f"{FILES_COLL}/{npdes}/files/{doc_id}", fdata))
    writes.append(("ADD", f"{RUNS_COLL}/(auto-id)", run_data))

    # --- Print summary ---
    print(f"  Writes planned: {len(writes)}")
    print(f"    {PLANTS_COLL}/{npdes}  (plants table)")
    print(f"    {PROFILES_COLL}/{npdes}  (profile_data)")
    for doc_id, _ in file_docs:
        print(f"    {FILES_COLL}/{npdes}/files/{doc_id}")
    print(f"    {RUNS_COLL}/(auto-id)  (agent run record)")

    if dry_run:
        print("\n--- DRY RUN: writes that would be executed ---")
        for op, path, data in writes:
            _print_write(op, path, data)
        print(f"\n--- DRY RUN complete: {len(writes)} write(s) would be executed ---\n")
        return

    if not force:
        ans = input(f"\n  Apply {len(writes)} write(s) to Firestore? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            sys.exit(0)

    # --- Execute writes ---
    db = _get_db()
    write_fn = db.collection

    errors = []
    for op, path, data in writes:
        try:
            parts = path.split("/")
            if op == "SET":
                ref = db.document(path)
                if merge:
                    ref.set(data, merge=True)
                else:
                    ref.set(data)
                print(f"  ✓  SET  {path}")
            elif op == "ADD":
                # agent_runs — auto-generated ID
                coll_path = "/".join(parts[:-1]) if parts[-1] == "(auto-id)" else path
                ref = db.collection(coll_path).add(data)
                print(f"  ✓  ADD  {coll_path} → {ref[1].id}")
        except Exception as exc:
            print(f"  ✗  FAILED  {path} — {exc}", file=sys.stderr)
            errors.append((path, str(exc)))

    print(f"\n{'='*64}")
    if errors:
        print(f"  COMPLETED WITH {len(errors)} ERROR(S)")
        for path, msg in errors:
            print(f"  ✗  {path}: {msg}")
        sys.exit(1)
    else:
        print(f"  DONE — {len(writes)} write(s) applied to Firestore")
    print(f"{'='*64}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a Nymph seed package to Firestore")
    parser.add_argument("path", help="Path to seed package JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Print writes without executing")
    parser.add_argument("--force",   action="store_true", help="Skip confirmation prompts")
    parser.add_argument("--merge",   action="store_true", help="Merge into existing documents (default: overwrite)")
    args = parser.parse_args()

    if not Path(args.path).exists():
        print(f"ERROR: File not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    apply(args.path, dry_run=args.dry_run, force=args.force, merge=args.merge)


if __name__ == "__main__":
    main()
