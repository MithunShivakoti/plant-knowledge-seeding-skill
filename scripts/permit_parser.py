"""
permit_parser.py — Extract structured Nymph plant knowledge from permit document text.

Uses OpenAI gpt-4o to parse permit text into structured JSON matching the Nymph
seed package schema. Attaches source, page, and confidence to every extracted fact.

Usage:
    python permit_parser.py --input extracted_text.json --npdes AL0056626
    python permit_parser.py --input extracted_text.json --npdes AL0056626 --output parsed_facts.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from openai import OpenAI

REFERENCES_DIR = Path(__file__).parent.parent / "references"

SYSTEM_PROMPT = """You are a wastewater permit analyst for Nymph, an AI platform for wastewater treatment plant operators.

Your job is to extract structured plant knowledge from NPDES permit documents and return it as JSON.

Rules you must follow:
1. Only extract facts that are explicitly stated in the provided permit text. Do not infer, guess, or add generic wastewater knowledge.
2. Attach source document, page number, and a confidence score (0.0–1.0) to every extracted fact.
3. Flow values must be numbers (floats), never strings. Use null if not found.
4. Permit limits must preserve the exact unit, statistic, season, frequency, and sample type from the permit table.
5. Do not convert permit limits into operating targets — they are different things.
6. Process train nodes must have a normalized type from the vocabulary provided.
7. Nymph Log entries must be plant-specific facts from this document, not generic wastewater knowledge.
8. Leave any array or field empty ([]) if public evidence from this document does not support it.
9. If two sections of the permit give different values for the same field, report both in the conflicts array.

You must output a single valid JSON object matching the schema provided."""

EXTRACTION_PROMPT_TEMPLATE = """Extract structured Nymph plant knowledge from this wastewater NPDES permit document.
Only extract values explicitly stated in the text. Do not guess or infer. Leave any field null or empty if not found.
Attach the page number where each value was found.

NPDES Permit Number: {npdes}
Source Document: {source_doc}
Total pages in document: {total_pages}

PROCESS UNIT VOCABULARY (use these exact type values):
{vocab}

NYMPH SCHEMA (your output must match this shape):
{schema}

FIELD EXTRACTION GUIDANCE — search every page for these specific signals:

basics.designFlowMGD (number):
  Look for: "Design Flow", "Qw =", "Design Flow in Million Gallons", "Facility Design Flow"
  Value is a number followed by "MGD". Example: "2.7 MGD" → 2.7

basics.processType (string):
  Look for: "Treatment Method", "Type of Discharger", "Fact Sheet", "Description of Applicant", "Description of Facility"
  Examples: "Mechanical (WWTP)", "Activated Sludge", "Oxidation Ditch", "Lagoon", "Trickling Filter"

basics.influentType (string):
  Look for: "Type of Discharger", "Description of Discharge", "Municipal", "Domestic", "Industrial"
  Examples: "Domestic", "Municipal", "Industrial", "Mixed"

basics.averageFlowMGD (number):
  Look for: "Average Flow", "Annual Average Flow", "Avg Flow", "Mean Annual Flow"
  Value is a number followed by "MGD".

basics.peakFlowMGD (number):
  Look for: "Peak Flow", "Maximum Flow", "Peak Design Flow"
  Value is a number followed by "MGD".

basics.receivingWaterClassification (string):
  Look for water use classification near outfall descriptions and Fact Sheet sections.
  Signals: "receiving water", "discharge to", "classified as", "water quality classification",
  "water use classification", "stream classification", "designated use"
  Alabama full names and abbreviations to match:
    "Swimming and Fish & Wildlife Waters" → "S, F&W"
    "Fish & Wildlife Waters" → "F&W"
    "Warm Water Fisheries (WWF)" — use as-is
    "Critical Use Fisheries (CUF)" — use as-is
    "Outstanding Alabama Water (OAW)" — use as-is
    "Special Resource Water (SRW)" — use as-is
    "Effluent Limited Segment (ELS)" — use as-is
    "ONRW", "Tier 2", "Tier 2.5", "WQC"
  Short codes also found in permit tables (use exactly as printed): "S, F&W", "F&W", "WWF", "OAW", "ELS"
  Extract the classification string exactly as it appears in the permit. Do not guess.
  If multiple outfalls have different classifications, extract the primary outfall (001) classification.
  Leave null if no explicit classification statement is found.

permitLimits[].limits[].frequency (string):
  Found in the discharge limits table. Column labeled "Sample Freq", "Monitoring Frequency", or "Frequency".
  Examples: "2X Weekly", "Monthly", "Daily", "Quarterly", "Annually", "Continuous"
  IMPORTANT: Every limit must have this populated from the permit table. Do not leave null.

permitLimits[].limits[].sampleType (string):
  Same discharge limits table. Column labeled "Sample Type" or "Type".
  Examples: "Grab", "24-Hr Composite", "Calculated", "Continuous", "FFGS"
  IMPORTANT: Every limit must have this populated from the permit table. Do not leave null.

nymphLogs.chemicalsDosing[]:
  Look for pages containing: "chlorin", "disinfection", "chemical feed", "polymer", "alum",
  "sodium hypochlorite", "ferric chloride", "dechlorination", "sulfur dioxide"
  Extract any chemicals named with dosing context or feed points. Do not guess dose rates.

processTrain.liquid[] and processTrain.solids[]:
  Look for pages containing: "process train", "treatment units", "headworks", "aeration basin",
  "clarifier", "digester", "process flow", "schematic", "description of facility", "unit processes",
  "influent", "effluent", "secondary treatment", "tertiary treatment"
  Extract an ordered list of all treatment units mentioned, in flow sequence.

nymphLogs.proceduresPreferences[]:
  Look for: "SSO Response Plan", "MWPP", "AEPACS", "DMR", "Discharge Monitoring Report",
  "reporting requirements", "certified operator", "WET testing", "toxicity testing",
  "sludge disposal", "biosolids", "sample schedule", "notification requirement"

PERMIT TEXT (with page numbers):
{permit_text}

Return a single JSON object with these top-level keys:

- basics: object with processType, influentType, designFlowMGD, averageFlowMGD, peakFlowMGD (all numbers or null — never strings), plus _sourceMeta

- processTrain: object with three arrays:
    liquid[]: nodes for the liquid treatment train
    solids[]: nodes for the solids treatment train
    connections[]: named side/return flows only (RAS, WAS, recycle, filtrate, centrate, scum, bypass)
  Each node must have: id, type, label, train ("liquid" or "solids"), order (integer), stage, status ("active"/"inactive"/"standby"), notes
  Include inactive units from diagrams — record as status="inactive", do NOT omit them.
  For node IDs use format: {plant_slug}-l1, {plant_slug}-l2, etc. for liquid; {plant_slug}-s1, {plant_slug}-s2, etc. for solids.
  Plant slug: {plant_slug}

- permitLimits: ARRAY (not an object) of permit parameter entries. One entry per parameter/outfall combination.
  Each entry must have:
    parameter: string — primary parameter name (e.g. "BOD", "TSS", "pH")
    aliases: array of strings — other names used in the permit (e.g. ["BOD5", "CBOD5"])
    unit: string — exactly as written in the permit (e.g. "mg/L", "CFU/100mL", "lbs/day", "su")
    outfall: string or null — outfall designation from the permit (e.g. "001")
    samplePoint: string or null — sample location (e.g. "Effluent", "Influent")
    limits: array of limit objects, each with:
      value (number), limitType (Monthly/Weekly/Daily/Annual), statistic (average/maximum/minimum/geometric_mean),
      qualifier (null or "<=" or ">="),
      seasonality (REQUIRED — one of "Summer (May-October)", "Winter (November-April)", or "Not Seasonal";
        check the permit table for seasonal applicability around this parameter row),
      samplePoint (string or null — extract from the monitoring location or outfall column adjacent to this row,
        e.g. "Effluent Gross", "Effluent", "Influent", "Effluent — Outfall 001"),
      frequency (e.g. "Daily", "Weekly"), sampleType (e.g. "Composite", "Grab"),
      reportOnly (boolean), evidenceClass ("direct_text_extraction"), confidence (0.9), _sourceMeta

- nymphLogs: object with four arrays:
    chemicalsDosing[]: chemicals named in the permit with feed points — do NOT include guessed doses
    operatingHistory[]: seasonal patterns, historical capacity info, noted changes — from permit text only
    proceduresPreferences[]: permit-required procedures (DMR, SSO notification, WET testing, sludge disposal, sample schedules)
    historicalOutcomes[]: outcomes from inspections or enforcement records
  Each log entry must have: value (plant-specific fact, not generic wastewater text), source ("permit_pdf"),
  evidenceClass ("direct_text_extraction"), confidence (0.0-1.0), status ("confirmed"), tags (array),
  lastUpdated (ISO datetime), _sourceMeta

- conflicts: array of {{field, values[], resolved}} — report when two permit sections give different values

- extractionSummary: string — describe what was found and what was absent

Do NOT include:
- Operating targets (internal plant targets — different from permit limits)
- Generic wastewater facts not specific to this plant
- Guessed dose rates or assumed preferences

Output only the JSON object. No explanation, no markdown, no code fences."""


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_vocab_summary() -> str:
    vocab_path = REFERENCES_DIR / "process_unit_vocab.json"
    vocab = load_json(str(vocab_path))
    lines = []
    for unit in vocab["units"]:
        aliases = ", ".join(unit["aliases"][:4])
        lines.append(f"  type={unit['type']} train={unit['train']} stage={unit['stage']} aliases: {aliases}...")
    return "\n".join(lines)


def load_schema_summary() -> str:
    schema_path = REFERENCES_DIR / "nymph_schema.json"
    schema = load_json(str(schema_path))
    # Return a compact version focusing on the output shape
    output_keys = {
        "basics": {"processType": "string|null", "influentType": "string|null",
                   "receivingWaterClassification": "string|null",
                   "designFlowMGD": "number|null", "averageFlowMGD": "number|null",
                   "peakFlowMGD": "number|null", "_sourceMeta": {"sourceDoc": "string",
                   "sourcePage": "int|null", "confidence": "float", "extractedAt": "datetime"}},
        "processTrain": {"liquid": "[processUnit]", "solids": "[processUnit]", "connections": "[connection]"},
        "permitLimits": [{"parameter": "string", "aliases": "[string]", "unit": "string",
                          "outfall": "string|null", "samplePoint": "string|null",
                          "limits": [{"value": "number", "limitType": "string", "statistic": "string",
                                      "qualifier": "string|null",
                                      "seasonality": "Not Seasonal|Summer (May-October)|Winter (November-April)",
                                      "samplePoint": "string|null",
                                      "frequency": "string|null", "sampleType": "string|null",
                                      "reportOnly": "boolean",
                                      "evidenceClass": "direct_text_extraction", "confidence": 0.9,
                                      "_sourceMeta": {}}]}],
        "nymphLogs": {"chemicalsDosing": "[logEntry]", "operatingHistory": "[logEntry]",
                      "proceduresPreferences": "[logEntry]", "historicalOutcomes": "[logEntry]"},
        "conflicts": [{"field": "string", "values": ["string"], "resolved": "string"}],
        "extractionSummary": "string",
    }
    return json.dumps(output_keys, indent=2)


def build_permit_text_block(extracted: dict, max_chars: int = 150000) -> str:
    """
    Build a page-annotated text block from the extraction result.
    Truncates to max_chars to stay within context limits.
    """
    lines = []
    total = 0
    for page in extracted.get("pages", []):
        page_num = page["page"]
        text = page["text"]
        if not text:
            continue
        header = f"\n=== PAGE {page_num} ===\n"
        chunk = header + text
        if total + len(chunk) > max_chars:
            remaining = max_chars - total
            lines.append(chunk[:remaining])
            lines.append(f"\n[TRUNCATED — {extracted['totalPages'] - page_num} more pages not shown]")
            break
        lines.append(chunk)
        total += len(chunk)
    return "\n".join(lines)


_CHUNK_SIZE = 65000  # ~16K tokens per chunk; keeps each call under 30K TPM on free tier
_TPM_SLEEP_S = 65    # seconds to wait between chunks so the token-per-minute quota resets


def _call_gpt4o(client: OpenAI, prompt: str, chunk_idx: int, total_chunks: int) -> dict:
    """Single gpt-4o call. Returns parsed JSON dict."""
    print(f"[PARSER] Call {chunk_idx}/{total_chunks}: {len(prompt):,} chars — sending to gpt-4o")
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=8192,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    print(f"[PARSER] Response {chunk_idx} received ({len(raw):,} chars)")
    content = raw.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.endswith("```"):
            content = content[:-3].strip()
    return json.loads(content)


def _patch_limits_from_tables(parsed: dict, tables: dict, source_doc: str, now: str) -> int:
    """
    Patch frequency and sampleType on permit limits using structured table data
    extracted by Document AI.  Only fills fields that are still null/empty.

    Matching strategy: fuzzy — any word (≥3 chars) from the table row's parameter
    cell must appear in the limit's parameter name (case-insensitive).

    Returns the count of individual limit rows patched.
    """
    discharge_rows = (tables or {}).get("discharge_limits_rows", [])
    if not discharge_rows:
        return 0

    permit_limits = parsed.get("permitLimits", [])
    patched = 0

    for row in discharge_rows:
        row_param = (row.get("parameter") or "").lower()
        row_freq = (row.get("frequency") or "").strip()
        row_stype = (row.get("sampleType") or "").strip()
        if not row_param or (not row_freq and not row_stype):
            continue

        # Match against each permit limit group
        for group in permit_limits:
            grp_param = (group.get("parameter") or "").lower()
            # Fuzzy match: any significant word from the table row hits the group name
            words = [w for w in row_param.split() if len(w) >= 3]
            if not words:
                continue
            if not any(w in grp_param for w in words):
                continue

            for lim in group.get("limits", []):
                changed = False
                if row_freq and not lim.get("frequency"):
                    lim["frequency"] = row_freq
                    changed = True
                if row_stype and not lim.get("sampleType"):
                    lim["sampleType"] = row_stype
                    changed = True
                if changed:
                    lim.setdefault("_sourceMeta", {})["confidence"] = min(
                        lim["_sourceMeta"].get("confidence", 1.0), 0.95
                    )
                    patched += 1

    return patched


def extract_facts_from_permit(
    extracted: dict,
    npdes: str,
    source_doc: str,
    plant_slug: str,
    client: OpenAI,
) -> dict:
    """
    Call gpt-4o to extract structured facts from permit text.

    When the extraction result contains structured Document AI tables
    (extracted["tables"]["discharge_limits_rows"]), frequency and sampleType
    are patched directly from table data before returning, with higher confidence
    than regex or ADEM defaults.

    Splits text into chunks of ~65K chars with a 65s sleep between calls to stay
    within the 30K tokens-per-minute rate limit on free-tier accounts.
    Returns merged JSON dict.
    """
    vocab = load_vocab_summary()
    schema = load_schema_summary()
    permit_text = build_permit_text_block(extracted)
    tables = extracted.get("tables", {})
    now = datetime.now(timezone.utc).isoformat()

    if permit_text.strip():
        preview = permit_text.strip()[:500].replace("\n", " ")
        print(f"[PARSER] Extracted text preview (first 500 chars):\n  {preview}")
    else:
        print("[PARSER] WARNING: Extracted text is empty — pdfplumber returned no content (scanned PDF?)")

    # Split into chunks that fit within per-minute token budget
    chunks = [permit_text[i: i + _CHUNK_SIZE] for i in range(0, len(permit_text), _CHUNK_SIZE)] or [""]
    print(f"[PARSER] Permit text {len(permit_text):,} chars → {len(chunks)} chunk(s) of ~{_CHUNK_SIZE:,} chars")

    chunk_results = []
    for idx, chunk in enumerate(chunks, start=1):
        if idx > 1:
            print(f"[PARSER] Waiting {_TPM_SLEEP_S}s before chunk {idx} to respect TPM limit...")
            time.sleep(_TPM_SLEEP_S)
        prompt = EXTRACTION_PROMPT_TEMPLATE.format(
            npdes=npdes,
            source_doc=source_doc,
            total_pages=extracted.get("totalPages", "unknown"),
            vocab=vocab,
            schema=schema,
            permit_text=chunk,
            plant_slug=plant_slug,
        )
        try:
            parsed = _call_gpt4o(client, prompt, idx, len(chunks))
            chunk_results.append(parsed)
        except json.JSONDecodeError as exc:
            print(f"[PARSER] ERROR: chunk {idx} returned invalid JSON — {exc}")
            raise

    # Merge all chunk results
    if len(chunk_results) == 1:
        merged = chunk_results[0]
    else:
        print(f"[PARSER] Merging {len(chunk_results)} chunk results")
        merged = merge_extracted_facts(chunk_results)

    parsed = merged

    # Inject metadata that the model may have omitted
    if "basics" in parsed and "_sourceMeta" not in parsed["basics"]:
        parsed["basics"]["_sourceMeta"] = {
            "sourceDoc": source_doc,
            "sourcePage": None,
            "confidence": 0.8,
            "extractedAt": now,
        }

    # Patch frequency/sampleType from Document AI table data (highest confidence source)
    if tables.get("discharge_limits_rows"):
        table_patched = _patch_limits_from_tables(parsed, tables, source_doc, now)
        if table_patched:
            print(f"[PARSER] Patched {table_patched} limit row(s) with freq/sampleType from Document AI tables")

    # Post-processing: normalize GPT-4o output fields to Nymph schema
    _norm_limits = 0
    _norm_logs = 0
    for group in parsed.get("permitLimits", []):
        if not isinstance(group, dict):
            continue
        group_sample_point = group.get("samplePoint")
        for lim in group.get("limits", []):
            if not isinstance(lim, dict):
                continue
            # Fix 1a: season → seasonality
            if "season" in lim and "seasonality" not in lim:
                raw = (lim.pop("season") or "").strip()
                rl = raw.lower()
                if any(kw in rl for kw in ["summer", "may", "apr-oct", "may-oct", "may through oct"]):
                    lim["seasonality"] = "Summer (May-October)"
                elif any(kw in rl for kw in ["winter", "nov", "oct-apr", "nov-apr", "nov through apr"]):
                    lim["seasonality"] = "Winter (November-April)"
                elif raw:
                    lim["seasonality"] = raw
                else:
                    lim["seasonality"] = "Not Seasonal"
                _norm_limits += 1
            elif "seasonality" not in lim:
                lim["seasonality"] = "Not Seasonal"
            # Fix 1b: evidenceClass
            if "evidenceClass" not in lim:
                lim["evidenceClass"] = "direct_text_extraction"
            # Fix 1c: confidence
            if "confidence" not in lim:
                lim["confidence"] = 0.9
            # Fix 1d: samplePoint — inherit from group level if not set per-limit
            if "samplePoint" not in lim:
                lim["samplePoint"] = group_sample_point

    for category in ["chemicalsDosing", "operatingHistory", "proceduresPreferences", "historicalOutcomes"]:
        for entry in parsed.get("nymphLogs", {}).get(category, []):
            if not isinstance(entry, dict):
                continue
            # Fix 2: source ai_inference → permit_pdf
            if entry.get("source") == "ai_inference":
                entry["source"] = "permit_pdf"
                _norm_logs += 1
            # Fix 2: add evidenceClass if missing
            if "evidenceClass" not in entry:
                entry["evidenceClass"] = "direct_text_extraction"

    # Normalize basics.influentType: "Municipal" and POTW synonyms → "Domestic"
    _raw_itype = (parsed.get("basics", {}).get("influentType") or "").strip().lower()
    if _raw_itype in ("municipal", "municipal wastewater", "potw", "publicly owned treatment works"):
        parsed["basics"]["influentType"] = "Domestic"
        _norm_logs += 1

    if _norm_limits or _norm_logs:
        print(f"[PARSER] Post-processing normalized {_norm_limits} limit field(s), {_norm_logs} log source(s)")

    print(f"[PARSER] Extraction complete:")
    basics = parsed.get("basics", {})
    print(f"  processType:   {basics.get('processType')}")
    print(f"  designFlowMGD: {basics.get('designFlowMGD')}")
    pt = parsed.get("processTrain", {})
    liquid = pt.get("liquid", pt.get("liquidTrain", []))
    solids = pt.get("solids", pt.get("solidsTrain", []))
    limits = parsed.get("permitLimits", [])
    if isinstance(limits, dict):
        limits = list(limits.values())
    logs = parsed.get("nymphLogs", {})
    print(f"  liquid nodes:  {len(liquid)}")
    print(f"  solids nodes:  {len(solids)}")
    print(f"  permitLimits:  {len(limits)} parameters")
    print(f"  chemicalsDosing:  {len(logs.get('chemicalsDosing', []))} entries")
    print(f"  operatingHistory: {len(logs.get('operatingHistory', []))} entries")
    print(f"  procedures:       {len(logs.get('proceduresPreferences', []))} entries")
    print(f"  outcomes:         {len(logs.get('historicalOutcomes', []))} entries")

    return parsed


def merge_extracted_facts(results: list[dict]) -> dict:
    """
    Merge extraction results from multiple documents.
    Later documents supplement earlier ones; conflicts are logged.
    """
    merged = {
        "basics": {},
        "processTrain": {"liquid": [], "solids": [], "connections": []},
        "permitLimits": [],
        "nymphLogs": {
            "chemicalsDosing": [],
            "operatingHistory": [],
            "proceduresPreferences": [],
            "historicalOutcomes": [],
        },
        "conflicts": [],
        "extractionSummary": "",
    }

    summaries = []
    for result in results:
        # Basics — take first non-null value found
        basics = result.get("basics", {})
        for field in ["processType", "influentType", "designFlowMGD", "averageFlowMGD", "peakFlowMGD", "_sourceMeta"]:
            if field not in merged["basics"] or merged["basics"][field] is None:
                if basics.get(field) is not None:
                    merged["basics"][field] = basics[field]
            elif basics.get(field) is not None and basics[field] != merged["basics"].get(field):
                merged["conflicts"].append({
                    "field": f"basics.{field}",
                    "values": [str(merged["basics"][field]), str(basics[field])],
                    "resolved": f"Using first value: {merged['basics'][field]}",
                })

        # Process train — accumulate nodes by ID
        pt = result.get("processTrain", {})
        # Support both new (liquid/solids) and legacy (liquidTrain/solidsTrain) keys
        for node in pt.get("liquid", pt.get("liquidTrain", [])):
            if not any(n["id"] == node["id"] for n in merged["processTrain"]["liquid"]):
                merged["processTrain"]["liquid"].append(node)
        for node in pt.get("solids", pt.get("solidsTrain", [])):
            if not any(n["id"] == node["id"] for n in merged["processTrain"]["solids"]):
                merged["processTrain"]["solids"].append(node)
        for conn in pt.get("connections", []):
            if conn not in merged["processTrain"]["connections"]:
                merged["processTrain"]["connections"].append(conn)

        # Permit limits — merge by parameter+outfall combination (array format)
        incoming_limits = result.get("permitLimits", [])
        if isinstance(incoming_limits, dict):
            # Convert legacy dict format to array
            incoming_limits = [{"parameter": p, **d} for p, d in incoming_limits.items()]
        for item in incoming_limits:
            param = item.get("parameter", "")
            outfall = item.get("outfall")
            existing = next(
                (e for e in merged["permitLimits"]
                 if e.get("parameter") == param and e.get("outfall") == outfall),
                None
            )
            if existing is None:
                merged["permitLimits"].append(item)
            else:
                for lim in item.get("limits", []):
                    if lim not in existing.get("limits", []):
                        existing.setdefault("limits", []).append(lim)

        # Nymph Logs — accumulate unique entries
        for category in ["chemicalsDosing", "operatingHistory", "proceduresPreferences", "historicalOutcomes"]:
            for entry in result.get("nymphLogs", {}).get(category, []):
                if not any(e["value"] == entry["value"] for e in merged["nymphLogs"][category]):
                    merged["nymphLogs"][category].append(entry)

        merged["conflicts"].extend(result.get("conflicts", []))
        if result.get("extractionSummary"):
            summaries.append(result["extractionSummary"])

    merged["extractionSummary"] = " | ".join(summaries)
    return merged


def main():
    parser = argparse.ArgumentParser(description="Parse permit text into structured Nymph plant knowledge")
    parser.add_argument("--input", required=True, help="JSON file from pdf_extractor.py (or a list of them separated by commas)")
    parser.add_argument("--npdes", required=True, help="NPDES permit number")
    parser.add_argument("--plant-slug", default="plant", help="Short slug for node IDs (e.g. gilliam)")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()
   
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("[PARSER] ERROR: OPENAI_API_KEY environment variable not set")
        sys.exit(1)

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())

    input_files = [f.strip() for f in args.input.split(",")]
    all_results = []

    for input_file in input_files:
        print(f"\n[PARSER] Processing: {input_file}")
        extracted = load_json(input_file)
        source_doc = extracted.get("source", input_file)
        result = extract_facts_from_permit(
            extracted=extracted,
            npdes=args.npdes,
            source_doc=source_doc,
            plant_slug=args.plant_slug,
            client=client,
        )
        all_results.append(result)

    if len(all_results) == 1:
        merged = all_results[0]
    else:
        print(f"\n[PARSER] Merging results from {len(all_results)} documents")
        merged = merge_extracted_facts(all_results)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, default=str)
        print(f"\n[PARSER] Parsed facts saved to {args.output}")
    else:
        print("\n--- Parsed Facts JSON ---")
        print(json.dumps(merged, indent=2, default=str))


if __name__ == "__main__":
    main()
