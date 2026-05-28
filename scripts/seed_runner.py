"""
seed_runner.py — Main entry point for the Nymph Plant Knowledge Seeding Skill.

Orchestrates: ECHO lookup → permit PDF download → text extraction →
gpt-4o parsing → package assembly → validation → output.

Usage:
    python seed_runner.py --plant "Gilliam Creek WWTP" --location "Arab, Alabama" --npdes AL0056626
    python seed_runner.py --plant "Eufaula WWTP" --location "Eufaula, Alabama" --npdes AL0061671 --owner "City of Eufaula"

Options:
    --plant       Full plant name (required)
    --location    City and state, e.g. "Arab, Alabama" (required)
    --npdes       NPDES permit number (required)
    --owner       Permittee/utility name (optional)
    --plant-id    Nymph internal plant ID (optional, used in node IDs)
    --production  Set true to mark package as ready for DB apply (default: pending_review)
    --output      Output JSON file path (default: <npdes>_seed_package.json)
    --skip-echo   Skip ECHO API lookup
    --skip-pdf    Skip PDF download and parsing
    --no-validate Skip validation step
    --verbose     Print detailed progress
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import echo_lookup as echo_mod
import pdf_extractor as pdf_mod
import permit_parser as parser_mod
from seed_validator import validate_seed_package, print_report

from openai import OpenAI

REFERENCES_DIR = Path(__file__).parent.parent / "references"
DOWNLOADS_DIR = Path(__file__).parent.parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)
NOW = datetime.now(timezone.utc).isoformat()

_STATE_MONITORING_DEFAULTS_PATH = REFERENCES_DIR / "state_monitoring_defaults.json"
with open(_STATE_MONITORING_DEFAULTS_PATH, "r", encoding="utf-8") as _f:
    _STATE_MONITORING_DEFAULTS: dict = {
        k: v for k, v in json.load(_f).items() if not k.startswith("_")
    }

# ---------------------------------------------------------------------------
# Param-key → keywords that appear in ECHO parameter names (lowercase)
# Used to match OCR-extracted frequency/sampleType patches onto ECHO limits.
# ---------------------------------------------------------------------------
_PARAM_KEY_KEYWORDS: dict[str, list[str]] = {
    "bod":         ["bod", "biochemical", "cbod"],
    "tss":         ["total suspended", "tss"],
    "nh3":         ["ammonia", "nitrogen, ammonia"],
    "do":          ["oxygen, dissolved", "dissolved oxygen"],
    "ph":          ["ph"],
    "ecoli":       ["coli", "coliform"],
    "chlorine":    ["chlorine", "chlorin", "trc"],
    "flow":        ["flow"],
    "tp":          ["phosphorus"],
    "tn":          ["nitrogen, total", "total nitrogen"],
    "turbidity":   ["turbidity"],
    "temperature": ["temperature"],
    "color":       ["color"],
}

_FREQ_TOKENS = [
    "2x weekly", "2X weekly", "twice weekly",
    "weekly", "monthly", "daily", "quarterly",
    "annually", "annual", "continuous", "semi-annually", "semiannually",
]
_SAMPLE_TOKENS = [
    "24-hr composite", "24-hour composite", "24 hr composite",
    "composite", "grab", "calculated", "continuous", "ffgs",
]


def _extract_supplemental_from_ocr(
    extracted: dict,
    plant_slug: str,
    vocab_path: Path,
    now: str,
) -> dict:
    """
    Regex-based extraction pass over full OCR text.
    Fills fields that gpt-4o misses due to garbled tables or context truncation:
    processType, influentType, averageFlowMGD, peakFlowMGD, frequency/sampleType
    patches, chemicalsDosing, processTrain liquid/solids nodes, SSO/MWPP procedures.
    """
    pages = extracted.get("pages", [])
    page_texts = [(p["page"], p.get("text", "")) for p in pages]
    full_text = "\n".join(t for _, t in page_texts)
    full_lower = full_text.lower()
    source_doc = extracted.get("source", "")

    result: dict = {
        "basics": {},
        "permitLimitsPatches": {},
        "nymphLogs": {"chemicalsDosing": [], "proceduresPreferences": []},
        "processTrain": {"liquid": [], "solids": [], "connections": []},
    }

    # ------------------------------------------------------------------ #
    # basics: processType                                                 #
    # ------------------------------------------------------------------ #
    process_patterns = [
        r"Treatment Methods?\s*[:\-]\s*([^\n\.]{3,80})",
        r"\b(Activated Sludge(?:\s+Process)?)\b",
        r"\b(Oxidation Ditch)\b",
        r"\b(Sequencing Batch Reactor|SBR)\b",
        r"\b(Membrane Bioreactor|MBR)\b",
        r"\b(Trickling Filter)\b",
        r"\b(Lagoon[s]?(?:\s+System)?)\b",
        r"\b(Mechanical\s+\(?WWTP\)?)\b",
        r"\b(Extended Aeration)\b",
        r"\b(Contact Stabilization)\b",
    ]
    for pat in process_patterns:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            val = (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)).strip().rstrip(".,;( ")
            if len(val) > 2:
                result["basics"]["processType"] = val
                break

    # basics: influentType
    influent_patterns = [
        r"Type of Discharger\s*[:\-]\s*([^\n\.]{3,60})",
        r"\b(Municipal|Domestic|Industrial)\s+(?:wastewater|sewage)\b",
        r"discharges?\s+(?:of\s+)?([Mm]unicipal|[Dd]omestic|[Ii]ndustrial)\s+wastewater",
    ]
    for pat in influent_patterns:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            val = (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)).strip().rstrip(".,;")
            _vl = val.lower()
            if _vl in ("municipal", "municipal wastewater", "potw", "publicly owned treatment works"):
                result["basics"]["influentType"] = "Domestic"
            elif _vl in ("domestic", "domestic wastewater"):
                result["basics"]["influentType"] = "Domestic"
            elif _vl == "industrial":
                result["basics"]["influentType"] = "Industrial"
            else:
                result["basics"]["influentType"] = val
            break

    # ------------------------------------------------------------------ #
    # Fix 1 — designFlowMGD (regex-only, never depends on gpt-4o)        #
    # ------------------------------------------------------------------ #
    design_flow_patterns = [
        r"Qw\s*[=\s]+([0-9]+\.?[0-9]*)\s*MGD",
        r"Design\s+Flow[^0-9\n]{0,50}([0-9]+\.?[0-9]*)\s*MGD",
        r"Facility\s+Design\s+Flow[^0-9\n]{0,50}([0-9]+\.?[0-9]*)\s*MGD",
    ]
    for pat in design_flow_patterns:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                result["basics"]["designFlowMGD"] = val
                src_page = next(
                    (pg for pg, txt in page_texts if re.search(pat, txt, re.IGNORECASE)),
                    None,
                )
                result["basics"]["_designFlowPage"] = src_page
                break
            except ValueError:
                pass
    # 4th pattern: any number+MGD within 50 chars after 'design'
    if not result["basics"].get("designFlowMGD"):
        for m in re.finditer(r"([0-9]+\.?[0-9]*)\s*MGD", full_text, re.IGNORECASE):
            context = full_text[max(0, m.start() - 50): m.start()].lower()
            if "design" in context:
                try:
                    result["basics"]["designFlowMGD"] = float(m.group(1))
                    break
                except ValueError:
                    pass

    # Fix 5 — averageFlowMGD
    for pat in [
        r"(?:Average|Annual Average|Mean Annual|Avg\.?|ADF)\s+(?:Daily\s+)?Flow[^\d\n]{0,30}?([\d]+\.[\d]+|[\d]+)\s*MGD",
        r"(?:Daily Average|Avg)\s+Flow\s*[=:]\s*([\d]+\.[\d]+|[\d]+)\s*MGD",
        r"\bADF\s*[=:]\s*([\d]+\.[\d]+|[\d]+)\s*MGD",
        r"(?:Fact Sheet|Permit Rationale|NPDES Rationale)[^.]{0,400}(?:average|avg|ADF)[^\d\n]{0,40}([\d]+\.[\d]*)\s*MGD",
        r"(?:annual average|average daily)\s+(?:flow\s+)?(?:rate\s+)?(?:of\s+)?[^\d\n]{0,30}([\d]+\.[\d]*)\s*(?:MGD|mg/d)",
    ]:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            try:
                result["basics"]["averageFlowMGD"] = float(m.group(1))
                break
            except ValueError:
                pass

    # Fix 5 — peakFlowMGD
    for pat in [
        r"(?:Peak|Maximum\s+Daily|Peak\s+Hour(?:ly)?|PHF)\s+(?:Day\s+|Hourly\s+|Daily\s+)?Flow[^\d\n]{0,30}?([\d]+\.[\d]+|[\d]+)\s*MGD",
        r"\bPHF\s*[=:]\s*([\d]+\.[\d]+|[\d]+)\s*MGD",
        r"Peak\s+Design\s+Flow[^\d\n]{0,30}?([\d]+\.[\d]+|[\d]+)\s*MGD",
        r"(?:Fact Sheet|Permit Rationale|NPDES Rationale)[^.]{0,400}(?:peak|PHF|maximum daily)[^\d\n]{0,40}([\d]+\.[\d]*)\s*MGD",
        r"(?:peak hourly flow|maximum daily flow|peak flow)[^\d\n]{0,50}([\d]+\.[\d]*)\s*MGD",
    ]:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            try:
                result["basics"]["peakFlowMGD"] = float(m.group(1))
                break
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Fix 2 — frequency/sampleType patches per parameter keyword          #
    # ------------------------------------------------------------------ #
    for param_key, keywords in _PARAM_KEY_KEYWORDS.items():
        for kw in keywords:
            pos = full_lower.find(kw)
            if pos == -1:
                continue
            window = full_text[max(0, pos - 100): pos + 500].lower()
            freq = next((ft for ft in _FREQ_TOKENS if ft.lower() in window), None)
            stype = next((st for st in _SAMPLE_TOKENS if st.lower() in window), None)
            if freq or stype:
                result["permitLimitsPatches"][param_key] = {
                    "frequency": freq,
                    "sampleType": stype,
                }
                break

    # ------------------------------------------------------------------ #
    # Fix 4 — chemicalsDosing                                             #
    # Build clean descriptions from permit section text, not raw OCR     #
    # window noise.                                                       #
    # ------------------------------------------------------------------ #

    def _find_section_text(header_pattern: str, chars: int = 500) -> tuple[str, int]:
        """
        Return (clean_text, page_number) for the best matching section header.
        Skips table-of-contents pages (dotted leaders or very sparse word count).
        """
        best_text, best_page = "", 0
        for pg, txt in page_texts:
            m = re.search(header_pattern, txt, re.IGNORECASE)
            if not m:
                continue
            # Skip TOC lines: dotted leaders like "....27" directly follow header
            if re.search(r"\.{4,}\s*\d+", txt[m.start(): m.start() + 80]):
                continue
            excerpt = txt[m.start(): min(len(txt), m.start() + chars)]
            clean = re.sub(r"\s+", " ", excerpt).strip()
            # Prefer the match with the most words (avoids single-word occurrences)
            if len(clean.split()) > len(best_text.split()):
                best_text, best_page = clean, pg
        return best_text, best_page

    # --- Chlorination / TRC ---
    trc_section, trc_page = _find_section_text(
        r"(?:Part\s+IV[\.\s]*[A-Z][\.\s]*)?Total\s+Residual\s+Chlor(?:ine)?"
    )
    if not trc_section:
        trc_section, trc_page = _find_section_text(r"[Cc]hlorin\w+\s+(?:contact|residual|disinfect)")

    # Extract the TRC limit value: scan full_text for explicit "TRC limit of X mg/L" phrase
    trc_limit_str = ""
    trc_limit_m = re.search(
        r"TRC\s+limit\s+of\s+(\d+\.?\d*)\s*mg/[Ll]|"
        r"[Tt]otal\s+[Rr]esidual\s+[Cc]hlorine[^\.]{0,60}(\d+\.?\d*)\s*mg/[Ll]",
        full_text,
    )
    if trc_limit_m:
        val = trc_limit_m.group(1) or trc_limit_m.group(2)
        trc_limit_str = f" Dechlorination required if TRC exceeds {val} mg/L limit."
    elif re.search(r"dechlorin", full_text, re.IGNORECASE):
        trc_limit_str = " Dechlorination required to meet effluent TRC limit."

    if trc_section:
        result["nymphLogs"]["chemicalsDosing"].append({
            "value": (
                "Chlorination used for disinfection. "
                "Total Residual Chlorine (TRC) monitored at effluent."
                + trc_limit_str
            ),
            "source": "permit_pdf",
            "evidenceClass": "direct_text_extraction",
            "confidence": 0.85,
            "status": "confirmed",
            "tags": ["public_seed", "chemicals", "disinfection"],
            "lastUpdated": now,
            "_sourceMeta": {
                "sourceDoc": source_doc,
                "sourcePage": trc_page,
                "confidence": 0.85,
                "extractedAt": now,
            },
        })

    # --- Dechlorination ---
    dechlor_section, dechlor_page = _find_section_text(r"[Dd]echlorin\w+")
    if dechlor_section:
        # Extract the dechlorination agent if named
        # Search section first, then full text, for a named dechlorination agent
        agent_pattern = r"(sodium\s+bisulfite|sulfur\s+dioxide|sodium\s+thiosulfate|sodium\s+sulfite|bisulfite|sulfite)"
        agent_m = re.search(agent_pattern, dechlor_section, re.IGNORECASE) or \
                  re.search(agent_pattern, full_text, re.IGNORECASE)
        agent = agent_m.group(0).title() if agent_m else "chemical reducing agent"
        result["nymphLogs"]["chemicalsDosing"].append({
            "value": (
                f"Dechlorination applied post-disinfection using {agent}. "
                "Required to reduce TRC to effluent permit limit."
            ),
            "source": "permit_pdf",
            "evidenceClass": "direct_text_extraction",
            "confidence": 0.85,
            "status": "confirmed",
            "tags": ["public_seed", "chemicals", "dechlorination"],
            "lastUpdated": now,
            "_sourceMeta": {
                "sourceDoc": source_doc,
                "sourcePage": dechlor_page,
                "confidence": 0.85,
                "extractedAt": now,
            },
        })

    # --- Other chemicals (non-chlorine): sodium hypochlorite, ferric chloride, alum, polymer ---
    other_chem_patterns = [
        (r"[Ss]odium\s+[Hh]ypochlorite", "Sodium Hypochlorite", ["chemicals", "disinfection"]),
        (r"[Ff]erric\s+[Cc]hloride",      "Ferric Chloride",      ["chemicals", "coagulation"]),
        (r"[Ss]odium\s+[Bb]isulfite",     "Sodium Bisulfite",     ["chemicals", "dechlorination"]),
        (r"[Ss]ulfur\s+[Dd]ioxide",       "Sulfur Dioxide",       ["chemicals", "dechlorination"]),
        (r"\bAlum\b",                      "Alum",                 ["chemicals", "coagulation"]),
        (r"[Pp]olymer\b",                  "Polymer",              ["chemicals", "biosolids"]),
    ]
    seen_other: set[str] = set()
    for pg, txt in page_texts:
        for pattern, chem_name, tags in other_chem_patterns:
            if chem_name in seen_other:
                continue
            m = re.search(pattern, txt)
            if not m:
                continue
            start = max(0, m.start() - 60)
            end = min(len(txt), m.end() + 120)
            ctx = re.sub(r"\s+", " ", txt[start:end]).strip()
            seen_other.add(chem_name)
            result["nymphLogs"]["chemicalsDosing"].append({
                "value": f"{chem_name} referenced in permit text: {ctx[:200]}",
                "source": "permit_pdf",
                "evidenceClass": "direct_text_extraction",
                "confidence": 0.85,
                "status": "confirmed",
                "tags": ["public_seed"] + tags,
                "lastUpdated": now,
                "_sourceMeta": {
                    "sourceDoc": source_doc,
                    "sourcePage": pg,
                    "confidence": 0.85,
                    "extractedAt": now,
                },
            })

    # ------------------------------------------------------------------ #
    # Fix 5+6 — processTrain liquid and solids nodes                      #
    # ------------------------------------------------------------------ #
    try:
        with open(str(vocab_path), "r") as f:
            vocab_data = json.load(f)
        vocab_units = vocab_data.get("units", [])
    except Exception:
        vocab_units = []

    def _vocab_label(node_type: str) -> str:
        for u in vocab_units:
            if u["type"] == node_type and u.get("aliases"):
                return u["aliases"][0]
        return node_type.replace("_", " ").title()

    def _vocab_stage(node_type: str) -> str:
        for u in vocab_units:
            if u["type"] == node_type:
                return u.get("stage", "secondary")
        return "secondary"

    LIQUID_SCAN = [
        # Full phrases first (highest specificity)
        ("headworks",                 "headworks"),
        ("bar screen",                "screening"),
        ("screening",                 "screening"),
        ("grit removal",              "grit_removal"),
        ("grit chamber",              "grit_removal"),
        ("grit",                      "grit_removal"),
        ("primary clarifier",         "primary_clarifier"),
        ("primary sedimentation",     "primary_clarifier"),
        ("primary clarif",            "primary_clarifier"),
        ("aeration basin",            "aeration_basin"),
        ("aeration tank",             "aeration_basin"),
        ("activated sludge",          "aeration_basin"),
        ("oxidation ditch",           "oxidation_ditch"),
        ("oxidation pond",            "oxidation_ditch"),
        ("sequencing batch reactor",  "sbr"),
        ("sequencing batch",          "sbr"),
        (" sbr ",                     "sbr"),
        ("membrane bioreactor",       "mbr"),
        (" mbr ",                     "mbr"),
        ("secondary clarifier",       "secondary_clarifier"),
        ("final clarifier",           "secondary_clarifier"),
        ("secondary clarif",          "secondary_clarifier"),
        ("final clarif",              "secondary_clarifier"),
        # Shorter fallbacks — only fire if none of the above matched
        ("aeration",                  "aeration_basin"),
        ("clarif",                    "secondary_clarifier"),
        ("uv disinfect",              "uv_disinfection"),
        ("uv system",                 "uv_disinfection"),
        ("ultraviolet",               "uv_disinfection"),
        ("chlorination",              "chlorination"),
        ("disinfection",              "chlorination"),
        ("dechlorination",            "dechlorination"),
    ]
    SOLIDS_SCAN = [
        ("gravity thickener",         "gravity_thickener"),
        ("thickener",                 "gravity_thickener"),
        ("anaerobic digester",        "anaerobic_digester"),
        ("aerobic digester",          "aerobic_digester"),
        ("digester",                  "anaerobic_digester"),
        ("dissolved air flotation",   "daf"),
        (" daf ",                     "daf"),
        ("belt press",                "belt_press"),
        ("centrifuge",                "centrifuge"),
        ("biosolids",                 "biosolids_storage"),
        ("sludge drying",             "sludge_drying"),
        ("sludge storage",            "biosolids_storage"),
        ("sludge",                    "biosolids_storage"),
    ]

    found_liquid: dict[str, dict] = {}
    found_solids: dict[str, dict] = {}

    for pg, txt in page_texts:
        tl = txt.lower()
        for kw, node_type in LIQUID_SCAN:
            if node_type in found_liquid:
                continue
            pos = tl.find(kw)
            if pos != -1:
                found_liquid[node_type] = {"page": pg, "pos": pos}
        for kw, node_type in SOLIDS_SCAN:
            if node_type in found_solids:
                continue
            pos = tl.find(kw)
            if pos != -1:
                found_solids[node_type] = {"page": pg, "pos": pos}

    # Second pass: inactive units — search for decommission/out-of-service qualifiers
    # and check ±150 chars for known unit keywords.  Only add units not already active.
    _INACTIVE_RE = re.compile(
        r"decommission\w*|out\s+of\s+service|abandon\w*|"
        r"no\s+longer\s+in\s+(?:use|service|operation)|"
        r"taken\s+out\s+of\s+service|not\s+(?:currently\s+)?in\s+(?:use|service|operation)|"
        r"removed\s+from\s+service|not\s+operational|shut\s+down|"
        r"bypassed?\s+(?:unit|basin|clarifier|tank)|offline",
        re.IGNORECASE,
    )
    inactive_liquid: dict[str, dict] = {}
    inactive_solids: dict[str, dict] = {}
    for pg, txt in page_texts:
        for m in _INACTIVE_RE.finditer(txt):
            start = max(0, m.start() - 150)
            end = min(len(txt), m.end() + 150)
            snippet = txt[start:end].lower()
            for kw, node_type in LIQUID_SCAN:
                if node_type in found_liquid or node_type in inactive_liquid:
                    continue
                if kw in snippet:
                    inactive_liquid[node_type] = {"page": pg, "pos": m.start()}
            for kw, node_type in SOLIDS_SCAN:
                if node_type in found_solids or node_type in inactive_solids:
                    continue
                if kw in snippet:
                    inactive_solids[node_type] = {"page": pg, "pos": m.start()}

    # Sort by canonical treatment-sequence order, not by page of mention.
    # Permit documents cite parameters in tables long before describing the process train.
    LIQUID_CANONICAL = [
        "headworks", "screening", "grit_removal", "primary_clarifier",
        "aeration_basin", "oxidation_ditch", "sbr", "mbr",
        "secondary_clarifier", "uv_disinfection", "chlorination", "dechlorination",
    ]
    SOLIDS_CANONICAL = [
        "gravity_thickener", "anaerobic_digester", "aerobic_digester",
        "daf", "belt_press", "centrifuge", "sludge_drying", "biosolids_storage",
    ]

    def _canonical_order(node_type: str, canonical: list[str]) -> int:
        try:
            return canonical.index(node_type)
        except ValueError:
            return len(canonical)

    liquid_sorted = sorted(found_liquid.items(), key=lambda x: _canonical_order(x[0], LIQUID_CANONICAL))
    solids_sorted = sorted(found_solids.items(), key=lambda x: _canonical_order(x[0], SOLIDS_CANONICAL))

    for i, (node_type, info) in enumerate(liquid_sorted, start=1):
        result["processTrain"]["liquid"].append({
            "id": f"{plant_slug}-l{i}",
            "type": node_type,
            "label": _vocab_label(node_type),
            "train": "liquid",
            "order": i,
            "stage": _vocab_stage(node_type),
            "status": "active",
            "confidence": 0.90,
            "evidenceClass": "direct_text_extraction",
            "notes": f"Detected in permit text on page {info['page']}",
        })
    for i, (node_type, info) in enumerate(solids_sorted, start=1):
        result["processTrain"]["solids"].append({
            "id": f"{plant_slug}-s{i}",
            "type": node_type,
            "label": _vocab_label(node_type),
            "train": "solids",
            "order": i,
            "stage": _vocab_stage(node_type),
            "status": "active",
            "confidence": 0.90,
            "evidenceClass": "direct_text_extraction",
            "notes": f"Detected in permit text on page {info['page']}",
        })

    # Inactive units — append after active nodes, with status=inactive, confidence=0.80
    inactive_liquid_sorted = sorted(inactive_liquid.items(), key=lambda x: _canonical_order(x[0], LIQUID_CANONICAL))
    inactive_solids_sorted = sorted(inactive_solids.items(), key=lambda x: _canonical_order(x[0], SOLIDS_CANONICAL))
    liquid_offset = len(result["processTrain"]["liquid"]) + 1
    solids_offset = len(result["processTrain"]["solids"]) + 1
    for i, (node_type, info) in enumerate(inactive_liquid_sorted, start=liquid_offset):
        result["processTrain"]["liquid"].append({
            "id": f"{plant_slug}-l{i}",
            "type": node_type,
            "label": _vocab_label(node_type),
            "train": "liquid",
            "order": i,
            "stage": _vocab_stage(node_type),
            "status": "inactive",
            "confidence": 0.80,
            "evidenceClass": "direct_text_extraction",
            "notes": f"Referenced as decommissioned/out-of-service in permit text on page {info['page']}",
        })
    for i, (node_type, info) in enumerate(inactive_solids_sorted, start=solids_offset):
        result["processTrain"]["solids"].append({
            "id": f"{plant_slug}-s{i}",
            "type": node_type,
            "label": _vocab_label(node_type),
            "train": "solids",
            "order": i,
            "stage": _vocab_stage(node_type),
            "status": "inactive",
            "confidence": 0.80,
            "evidenceClass": "direct_text_extraction",
            "notes": f"Referenced as decommissioned/out-of-service in permit text on page {info['page']}",
        })

    # ------------------------------------------------------------------ #
    # Fix 7 — SSO Response Plan                                           #
    # ------------------------------------------------------------------ #
    if re.search(r"Sanitary Sewer Overflow Response Plan|SSO Response Plan", full_text, re.IGNORECASE):
        sso_page = next(
            (pg for pg, txt in page_texts
             if re.search(r"SSO Response Plan|Sanitary Sewer Overflow Response Plan", txt, re.IGNORECASE)),
            None,
        )
        result["nymphLogs"]["proceduresPreferences"].append({
            "value": (
                "Sanitary Sewer Overflow (SSO) Response Plan required. "
                "Must be developed and implemented within 120 days of permit effective date per Part IV.E."
            ),
            "source": "permit_pdf",
            "evidenceClass": "direct_text_extraction",
            "confidence": 0.95,
            "status": "confirmed",
            "tags": ["public_seed", "SSO", "response_plan", "procedures"],
            "lastUpdated": now,
            "_sourceMeta": {"sourceDoc": source_doc, "sourcePage": sso_page, "confidence": 0.95, "extractedAt": now},
        })

    # ------------------------------------------------------------------ #
    # Fix 8 — MWPP Annual Report                                          #
    # ------------------------------------------------------------------ #
    if re.search(r"Municipal Water Pollution Prevention|MWPP Annual Report", full_text, re.IGNORECASE):
        mwpp_page = next(
            (pg for pg, txt in page_texts
             if re.search(r"MWPP|Municipal Water Pollution Prevention", txt, re.IGNORECASE)),
            None,
        )
        result["nymphLogs"]["proceduresPreferences"].append({
            "value": "Municipal Water Pollution Prevention (MWPP) Annual Report required by May 31st each year.",
            "source": "permit_pdf",
            "evidenceClass": "direct_text_extraction",
            "confidence": 0.95,
            "status": "confirmed",
            "tags": ["public_seed", "MWPP", "annual_report", "procedures"],
            "lastUpdated": now,
            "_sourceMeta": {"sourceDoc": source_doc, "sourcePage": mwpp_page, "confidence": 0.95, "extractedAt": now},
        })

    # ------------------------------------------------------------------ #
    # Fix 3 — Seasonality detection for E. coli limits                   #
    # ------------------------------------------------------------------ #
    if re.search(r"E\.?\s*coli", full_text, re.IGNORECASE):
        ecoli_seasonal: dict[str, str] = {}
        if re.search(
            r"(?:Summer|May\b.{0,60}Oct(?:ober)?|E\.?\s*coli[^\.]{0,100}May[^\.]{0,100}Oct(?:ober)?)",
            full_text, re.IGNORECASE,
        ):
            ecoli_seasonal["summer"] = "Summer (May-October)"
        if re.search(
            r"(?:Winter|Nov(?:ember)?\b.{0,60}Apr(?:il)?|E\.?\s*coli[^\.]{0,100}Nov(?:ember)?[^\.]{0,100}Apr(?:il)?)",
            full_text, re.IGNORECASE,
        ):
            ecoli_seasonal["winter"] = "Winter (November-April)"
        if ecoli_seasonal:
            result["seasonalityPatches"] = {"ecoli": ecoli_seasonal}

    return result


# ---------------------------------------------------------------------------
# State-specific standard monitoring defaults
# Loaded from references/state_monitoring_defaults.json at startup.
# Applied after OCR-based patches for any param limit still missing freq/sampleType.
# BOD and Ammonia are intentionally excluded from all state tables —
# they must come from permit PDF table extraction.
# ---------------------------------------------------------------------------


def _apply_state_monitoring_defaults(
    parsed_facts: dict, state_code: str, facts_skipped: list
) -> int:
    """
    Patch frequency/sampleType on any ECHO permit limit that is still null,
    using state-specific standard monitoring schedule values from
    references/state_monitoring_defaults.json.

    For states with no entry in the file, adds a factsSkipped entry and returns 0.
    Returns count of limits patched.
    """
    state_defaults = _STATE_MONITORING_DEFAULTS.get(state_code)
    if state_defaults is None:
        facts_skipped.append({
            "surface": "permitLimits.frequency_sampleType",
            "reason": (
                f"No state monitoring defaults available for state '{state_code}'. "
                "frequency and sampleType left null on ECHO limits — "
                "add defaults to references/state_monitoring_defaults.json to enable."
            ),
        })
        return 0
    if not state_defaults:
        return 0

    patched = 0
    for group in parsed_facts.get("permitLimits", []):
        param_lower = (group.get("parameter") or "").lower()
        for keyword, defaults in state_defaults.items():
            if keyword not in param_lower:
                continue
            freq = defaults.get("frequency")
            stype = defaults.get("sampleType")
            for lim in group.get("limits", []):
                changed = False
                if not lim.get("frequency") and freq:
                    lim["frequency"] = freq
                    changed = True
                if not lim.get("sampleType") and stype:
                    lim["sampleType"] = stype
                    changed = True
                if changed:
                    lim["evidenceClass"] = "adem_defaults"
                    patched += 1
    return patched


_WUC_PATTERNS: list[tuple] = [
    # (compiled_pattern, fixed_replacement_or_None)  — None means use group(1)
    (re.compile(r"S\s*,\s*F\s*[&and]+\s*W\b", re.IGNORECASE), "S, F&W"),
    (re.compile(r"Swimming[^.]{0,50}Fish[^.]{0,50}Wildlife", re.IGNORECASE), "S, F&W"),
    (re.compile(r"water\s+use\s+classifi\w*\s*[:\-=]\s*([A-Z][A-Z,\s&]{1,40})", re.IGNORECASE), None),
    (re.compile(r"classifi\w+\s+(?:as|for)\s+([A-Z][A-Z,\s&]{1,40})", re.IGNORECASE), None),
    (re.compile(r"Class\s+([A-Z][A-Z,\s&]{0,20})\b(?:\s*\(|\s*waters|\s*$)", re.IGNORECASE), None),
    (re.compile(r"F\s*[&and]+\s*W\b", re.IGNORECASE), "F&W"),
]


def _extract_water_classification_from_ocr(
    all_extracted: list,
    receiving_water_names: list[str],
    parsed_facts: dict,
    facts_skipped: list,
) -> None:
    """Regex fallback: scan permit OCR text for water use classification when GPT-4o fails."""
    if parsed_facts.get("basics", {}).get("receivingWaterClassification"):
        return

    full_text = " ".join(
        p.get("text", "")
        for ext in all_extracted
        for p in ext.get("pages", [])
    )
    if not full_text.strip():
        facts_skipped.append({
            "surface": "identity.receivingWaters.classification",
            "reason": "Water use classification not found — no OCR text available.",
        })
        return

    def _scan(text: str) -> str | None:
        for pat, fixed in _WUC_PATTERNS:
            m = pat.search(text)
            if m:
                val = fixed if fixed else m.group(1).strip().rstrip(".,; \t")
                if len(val) >= 2:
                    return val
        return None

    # Search within ±500 chars of each receiving water name first
    classification = None
    for name in receiving_water_names:
        if not name:
            continue
        idx = full_text.lower().find(name.lower())
        if idx != -1:
            snippet = full_text[max(0, idx - 500): idx + len(name) + 500]
            classification = _scan(snippet)
            if classification:
                break

    # Full-text fallback for the strongest patterns
    if not classification:
        classification = _scan(full_text)

    if classification:
        parsed_facts["basics"]["receivingWaterClassification"] = classification
        print(f"[RUNNER] Water use classification extracted via regex: '{classification}'")
    else:
        facts_skipped.append({
            "surface": "identity.receivingWaters.classification",
            "reason": "Water use classification not found in permit OCR text — check permit Fact Sheet manually.",
        })


_FREQ_CANONICAL: dict[str, str] = {
    "weekly": "Weekly",
    "monthly": "Monthly",
    "daily": "Daily",
    "annually": "Annually",
    "annual": "Annually",
    "quarterly": "Quarterly",
    "continuous": "Continuous",
    "2x weekly": "2X Weekly",
    "twice weekly": "2X Weekly",
    "2 x weekly": "2X Weekly",
    "biweekly": "2X Weekly",
    "calculated": "Calculated",
}


def _normalize_frequencies(parsed_facts: dict) -> None:
    """Normalize all frequency values on permit limits to consistent Title Case."""
    for group in parsed_facts.get("permitLimits", []):
        for lim in group.get("limits", []):
            freq = lim.get("frequency")
            if freq:
                normalized = _FREQ_CANONICAL.get(freq.lower().strip())
                if normalized:
                    lim["frequency"] = normalized


_MECHANICAL_PROCESS_TYPES = {
    "mechanical (wwtp)", "activated sludge", "mechanical", "extended aeration",
}

_INFERRED_NODE_DEFAULTS: dict[str, dict] = {
    "grit_removal":      {"stage": "preliminary", "label": "Grit Removal"},
    "primary_clarifier": {"stage": "primary",     "label": "Primary Clarifier"},
    "aeration_basin":    {"stage": "secondary",   "label": "Aeration Basin"},
}

_LIQUID_CANONICAL_ORDER = [
    "headworks", "screening", "grit_removal", "primary_clarifier",
    "aeration_basin", "oxidation_ditch", "sbr", "mbr",
    "secondary_clarifier", "uv_disinfection", "chlorination", "dechlorination",
]

_INFER_NOTE = "Standard Mechanical WWTP unit process — not explicitly named in permit text"


def _infer_standard_unit_processes(parsed_facts: dict, plant_slug: str, now: str) -> int:
    """
    For Mechanical/Activated Sludge plants, insert standard unit processes that
    are absent from the liquid train but expected given the process type.

    Only infers when there is at least one secondary-stage node already detected
    (guards against false inference on incomplete data). Inferred nodes get
    status='inferred' and confidence=0.55 (Low — inferred from process type only).

    Returns the count of nodes added.
    """
    process_type = (parsed_facts["basics"].get("processType") or "").lower()
    if not any(pt in process_type for pt in _MECHANICAL_PROCESS_TYPES):
        return 0

    liquid = parsed_facts["processTrain"].get("liquid") or []
    present_types = {n["type"] for n in liquid}

    # Only infer if we already have enough context (secondary clarifier or chlorination)
    anchor_types = {"secondary_clarifier", "chlorination", "dechlorination", "uv_disinfection"}
    if not present_types & anchor_types:
        return 0

    # Determine which standard nodes to infer
    # grit_removal: always infer if screening or headworks present and grit missing
    # primary_clarifier: only infer if plant has secondary clarifier but no aeration_basin
    #   (primary is optional on small plants; skip if plant looks like extended aeration)
    # aeration_basin: always infer for activated sludge type plants
    to_infer: list[str] = []

    if "grit_removal" not in present_types and (
        "screening" in present_types or "headworks" in present_types
    ):
        to_infer.append("grit_removal")

    if "aeration_basin" not in present_types and "oxidation_ditch" not in present_types and "sbr" not in present_types:
        to_infer.append("aeration_basin")

    # primary_clarifier only if we have both headworks/screening and secondary clarifier,
    # and the plant is not an extended-aeration / oxidation-ditch type
    if (
        "primary_clarifier" not in present_types
        and "extended aeration" not in process_type
        and "oxidation_ditch" not in present_types
        and ("secondary_clarifier" in present_types or "chlorination" in present_types)
        and ("screening" in present_types or "headworks" in present_types)
    ):
        to_infer.append("primary_clarifier")

    if not to_infer:
        return 0

    # Rebuild liquid list with inferred nodes, then re-sort and re-number
    def _canon_pos(t: str) -> int:
        try:
            return _LIQUID_CANONICAL_ORDER.index(t)
        except ValueError:
            return len(_LIQUID_CANONICAL_ORDER)

    for node_type in to_infer:
        defaults = _INFERRED_NODE_DEFAULTS[node_type]
        liquid.append({
            "id": f"{plant_slug}-inferred-{node_type.replace('_', '-')}",
            "type": node_type,
            "label": defaults["label"],
            "train": "liquid",
            "order": 0,
            "stage": defaults["stage"],
            "status": "inferred",
            "confidence": 0.55,
            "evidenceClass": "heuristic_inference",
            "notes": _INFER_NOTE,
        })

    liquid.sort(key=lambda n: _canon_pos(n["type"]))
    for i, node in enumerate(liquid, start=1):
        node["id"] = f"{plant_slug}-l{i}"
        node["order"] = i

    parsed_facts["processTrain"]["liquid"] = liquid
    return len(to_infer)


# GPT-4o returns these strings when it cannot extract a value — treat them as absent.
_GPT_WEAK_VALUES = frozenset({
    "not found", "none", "n/a", "", "municipal/industrial wastewater", "not specified",
    "unknown", "not stated", "not available",
})


def _apply_supplemental(parsed_facts: dict, supp: dict, now: str) -> None:
    """
    Merge supplemental OCR extraction results into parsed_facts in-place.

    For processType and influentType: supplemental regex always wins over GPT-4o
    when GPT-4o returned a weak/generic value ('Not found', 'None', etc.) and
    the supplemental result is a real non-empty non-generic string.
    For numeric flow fields: only fills when current is absent.
    """
    # basics
    for field in ["processType", "influentType", "designFlowMGD", "averageFlowMGD", "peakFlowMGD"]:
        supp_val = supp["basics"].get(field)
        if supp_val is None:
            continue
        # Normalise influentType synonyms before comparison
        if field == "influentType":
            _sl = (supp_val or "").strip().lower()
            if _sl in ("municipal", "municipal wastewater", "potw", "publicly owned treatment works"):
                supp_val = "Domestic"
        current = parsed_facts["basics"].get(field)
        if field in ("processType", "influentType"):
            current_weak = not current or str(current).strip().lower() in _GPT_WEAK_VALUES
            supp_valid = supp_val and str(supp_val).strip().lower() not in _GPT_WEAK_VALUES
            if current_weak and supp_valid:
                parsed_facts["basics"][field] = supp_val
            elif field == "influentType" and current in ("Mixed", "Municipal") and supp_valid:
                parsed_facts["basics"][field] = supp_val
        else:
            if not current:
                parsed_facts["basics"][field] = supp_val

    # processTrain — prefer whichever source found more nodes; regex pass is more reliable
    # than gpt-4o on garbled OCR text, so if supplemental found more nodes use them
    supp_liquid = supp["processTrain"].get("liquid") or []
    supp_solids = supp["processTrain"].get("solids") or []
    cur_liquid = parsed_facts["processTrain"].get("liquid") or []
    cur_solids = parsed_facts["processTrain"].get("solids") or []
    if len(supp_liquid) > len(cur_liquid):
        parsed_facts["processTrain"]["liquid"] = supp_liquid
    if len(supp_solids) > len(cur_solids):
        parsed_facts["processTrain"]["solids"] = supp_solids

    # permitLimits patches: apply frequency/sampleType to ECHO limits by keyword match
    patches = supp.get("permitLimitsPatches", {})
    for echo_group in parsed_facts.get("permitLimits", []):
        param_lower = (echo_group.get("parameter") or "").lower()
        for param_key, patch in patches.items():
            kws = _PARAM_KEY_KEYWORDS.get(param_key, [param_key])
            if not any(kw in param_lower for kw in kws):
                continue
            for lim in echo_group.get("limits", []):
                if not lim.get("frequency") and patch.get("frequency"):
                    lim["frequency"] = patch["frequency"]
                if not lim.get("sampleType") and patch.get("sampleType"):
                    lim["sampleType"] = patch["sampleType"]

    # nymphLogs — accumulate unique entries
    for category in ["chemicalsDosing", "proceduresPreferences"]:
        existing_vals = {e["value"] for e in parsed_facts["nymphLogs"].get(category, [])}
        for entry in supp["nymphLogs"].get(category, []):
            if entry.get("value") not in existing_vals:
                parsed_facts["nymphLogs"].setdefault(category, []).append(entry)
                existing_vals.add(entry["value"])

    # seasonality patches — mark E. coli permit limits if seasonal text found in permit
    seasonality_patches = supp.get("seasonalityPatches", {})
    if seasonality_patches.get("ecoli"):
        ecoli_kws = _PARAM_KEY_KEYWORDS.get("ecoli", ["coli"])
        season_str = " / ".join(seasonality_patches["ecoli"].values())
        for group in parsed_facts.get("permitLimits", []):
            param_lower = (group.get("parameter") or "").lower()
            if any(kw in param_lower for kw in ecoli_kws):
                for lim in group.get("limits", []):
                    if lim.get("seasonality") == "Not Seasonal":
                        lim["seasonality"] = f"Seasonal — {season_str}"


# ---------------------------------------------------------------------------
# NPDES permit number prefix → state info
# Every NPDES number starts with a 2-letter state code (same as USPS abbreviation).
# direct_pdf_patterns: URL templates with {npdes} placeholder to attempt before
#   falling back to portal navigation. Empty list means portal-only.
# portal: primary permit search URL to record in sources_searched so operators
#   know where to look manually when automated download fails.
# direct_download: "uncertain" = worth trying but often blocked;
#                  "portal_only" = skip attempts, go straight to portal note.
# ---------------------------------------------------------------------------
NPDES_STATE_MAP = {
    "AL": {
        "name": "Alabama", "agency": "ADEM",
        "portal": "https://epa.adem.alabama.gov/epa/permitSearch.do",
        "direct_pdf_patterns": [
            "https://www.adem.state.al.us/alldocs/docs/wPermits/{npdes}.pdf",
            "https://epa.adem.alabama.gov/epa/permitdoc/{npdes}/final-permit.pdf",
        ],
        "direct_download": "uncertain",
    },
    "AK": {
        "name": "Alaska", "agency": "ADEC",
        "portal": "https://dec.alaska.gov/water/wastewater/permits/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "AZ": {
        "name": "Arizona", "agency": "ADEQ",
        "portal": "https://gisweb.azdeq.gov/azdocdbpub/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "AR": {
        "name": "Arkansas", "agency": "ADEQ (AR)",
        "portal": "https://www.adeq.state.ar.us/water/permits/arapdes/default.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "CA": {
        "name": "California", "agency": "SWRCB",
        "portal": "https://www.waterboards.ca.gov/water_issues/programs/npdes/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "CO": {
        "name": "Colorado", "agency": "CDPHE",
        "portal": "https://cdphe.colorado.gov/water-quality/permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "CT": {
        "name": "Connecticut", "agency": "DEEP",
        "portal": "https://portal.ct.gov/DEEP/Water/Permits-Registrations-Certifications/NPDES-Permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "DE": {
        "name": "Delaware", "agency": "DNREC",
        "portal": "https://dnrec.delaware.gov/water/permits/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "FL": {
        "name": "Florida", "agency": "FDEP",
        "portal": "https://oculus.dep.state.fl.us/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "GA": {
        "name": "Georgia", "agency": "EPD",
        "portal": "https://gaepd.force.com/EPDonline/s/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "HI": {
        "name": "Hawaii", "agency": "DOH",
        "portal": "https://health.hawaii.gov/cwb/site-navigation/national-pollutant-discharge-elimination-system/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "ID": {
        "name": "Idaho", "agency": "DEQ (ID)",
        "portal": "https://www.deq.idaho.gov/water-quality/wastewater/wastewater-land-application-permits/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "IL": {
        "name": "Illinois", "agency": "IEPA",
        "portal": "https://www2.illinois.gov/epa/topics/water-quality/permits/Pages/default.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "IN": {
        "name": "Indiana", "agency": "IDEM",
        "portal": "https://vfc.idem.in.gov/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "IA": {
        "name": "Iowa", "agency": "DNR (IA)",
        "portal": "https://programs.iowadnr.gov/npdespermits/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "KS": {
        "name": "Kansas", "agency": "KDHE",
        "portal": "https://www.kdhe.ks.gov/1340/NPDES-Permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "KY": {
        "name": "Kentucky", "agency": "DEP (KY)",
        "portal": "https://eec.ky.gov/Environmental-Protection/Water/Permits/Pages/KPDES-Permit-Search.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "LA": {
        "name": "Louisiana", "agency": "LDEQ",
        "portal": "https://edms.deq.louisiana.gov/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "ME": {
        "name": "Maine", "agency": "DEP (ME)",
        "portal": "https://www.maine.gov/dep/water/licensing/wdp/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MD": {
        "name": "Maryland", "agency": "MDE",
        "portal": "https://mde.maryland.gov/programs/water/Pages/npdesPermitsSearch.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MA": {
        "name": "Massachusetts", "agency": "MassDEP",
        "portal": "https://edep.dep.mass.gov/Pages/Welcome.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MI": {
        "name": "Michigan", "agency": "EGLE",
        "portal": "https://www.michigan.gov/egle/regulatory-assistance/permits/surface-water-permits/wpdes-permit-search",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MN": {
        "name": "Minnesota", "agency": "MPCA",
        "portal": "https://www.pca.state.mn.us/business-with-us/permit-search",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MS": {
        "name": "Mississippi", "agency": "MDEQ",
        "portal": "https://www.mdeq.ms.gov/water/surface-water/wastewater/npdes/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MO": {
        "name": "Missouri", "agency": "MoDNR",
        "portal": "https://dnr.mo.gov/water/business-industry-other-entities/permits-certification-engineering-fees-forms/state-operating-permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "MT": {
        "name": "Montana", "agency": "DEQ (MT)",
        "portal": "https://deq.mt.gov/water/permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NE": {
        "name": "Nebraska", "agency": "NDEE",
        "portal": "https://dee.ne.gov/NDEQProg.nsf/onweb/NPDES",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NV": {
        "name": "Nevada", "agency": "NDEP",
        "portal": "https://ndep.nv.gov/water/permits/water-pollution-permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NH": {
        "name": "New Hampshire", "agency": "DES (NH)",
        "portal": "https://www.des.nh.gov/water/wastewater/permits.htm",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NJ": {
        "name": "New Jersey", "agency": "NJDEP",
        "portal": "https://www13.state.nj.us/DataMiner/Search/SearchByCategory?isExternal=Y&getCategory=NJPDES%20Permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NM": {
        "name": "New Mexico", "agency": "NMED",
        "portal": "https://www.env.nm.gov/water-quality/surface-water-quality-bureau/npdes/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NY": {
        "name": "New York", "agency": "NYSDEC",
        "portal": "https://extapps.dec.ny.gov/cfmx/extapps/envapps/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "NC": {
        "name": "North Carolina", "agency": "NCDEQ",
        "portal": "https://edocs.deq.nc.gov/WaterResources/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "ND": {
        "name": "North Dakota", "agency": "NDDEQ",
        "portal": "https://www.deq.nd.gov/wq/permits/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "OH": {
        "name": "Ohio", "agency": "Ohio EPA",
        "portal": "https://epa.ohio.gov/divisions-and-offices/division-of-surface-water/permit-programs/npdes",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "OK": {
        "name": "Oklahoma", "agency": "ODEQ",
        "portal": "https://www.deq.ok.gov/divisions/wqd/permits-and-permits-engineering/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "OR": {
        "name": "Oregon", "agency": "DEQ (OR)",
        "portal": "https://permits.oregon.gov/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "PA": {
        "name": "Pennsylvania", "agency": "PaDEP",
        "portal": "https://www.dep.pa.gov/DataandTools/Pages/eFACTS.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "RI": {
        "name": "Rhode Island", "agency": "DEM (RI)",
        "portal": "https://dem.ri.gov/programs/water/permits/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "SC": {
        "name": "South Carolina", "agency": "SCDHEC",
        "portal": "https://www.scdhec.gov/environment/water-pollution/scpdes-permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "SD": {
        "name": "South Dakota", "agency": "DANR",
        "portal": "https://danr.sd.gov/water/watersheds/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "TN": {
        "name": "Tennessee", "agency": "TDEC",
        "portal": "https://tdec.tn.gov/epd/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "TX": {
        "name": "Texas", "agency": "TCEQ",
        "portal": "https://www15.tceq.texas.gov/crpub/index.cfm?fuseaction=iwr.main",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "UT": {
        "name": "Utah", "agency": "DWQ",
        "portal": "https://deq.utah.gov/water-quality/utah-pollutant-discharge-elimination-system-updes-permits",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "VT": {
        "name": "Vermont", "agency": "DEC (VT)",
        "portal": "https://dec.vermont.gov/watershed/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "VA": {
        "name": "Virginia", "agency": "DEQ (VA)",
        "portal": "https://www.deq.virginia.gov/programs/water/permits/vpdes",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "WA": {
        "name": "Washington", "agency": "Ecology",
        "portal": "https://apps.ecology.wa.gov/paris/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "WV": {
        "name": "West Virginia", "agency": "WVDEP",
        "portal": "https://dep.wv.gov/WWE/Programs/NPDES/Pages/default.aspx",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "WI": {
        "name": "Wisconsin", "agency": "WDNR",
        "portal": "https://dnr.wisconsin.gov/topic/Permits/wpdes.html",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
    "WY": {
        "name": "Wyoming", "agency": "WDEQ",
        "portal": "https://deq.wyoming.gov/water-quality/npdes-updes/",
        "direct_pdf_patterns": [],
        "direct_download": "portal_only",
    },
}


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return slug.strip("-")[:20]


_STATE_NAME_MAP = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def npdes_state_code(npdes: str) -> str:
    """Extract the 2-letter state code from an NPDES permit number prefix."""
    m = re.match(r"^([A-Z]{2})", npdes.upper())
    return m.group(1) if m else ""


def state_abbrev(location: str, npdes: str = "") -> str:
    """
    Return a 2-letter state abbreviation.
    Prefers the NPDES prefix as the most reliable source; falls back to
    parsing the location string.
    """
    if npdes:
        code = npdes_state_code(npdes)
        if code and code in NPDES_STATE_MAP:
            return code
    parts = [p.strip() for p in location.split(",")]
    if parts and len(parts[-1]) == 2 and parts[-1].upper().isalpha():
        return parts[-1].upper()
    loc_lower = location.lower()
    for name, abbr in _STATE_NAME_MAP.items():
        if name in loc_lower:
            return abbr
    return ""


def find_permit_pdf_urls(echo_result: dict, npdes: str, location: str) -> list[dict]:
    """
    Build a prioritized list of permit PDF URLs to try, keyed to the correct
    state agency by NPDES prefix.

    Priority order:
      1 — ECHO document links (direct links found in the DFR response)
      2 — State-specific constructed URL patterns (where known)
      3 — State portal URL recorded as a metadata note (no download attempted)

    Returns list of {url, title, type, priority, source, attempt_download}.
    Items with attempt_download=False are recorded in sources_searched but
    not passed to pdf_mod.download_pdf.
    """
    urls = []

    # Determine state from NPDES prefix (most reliable) then location fallback
    state_code = npdes_state_code(npdes) or state_abbrev(location)
    state_info = NPDES_STATE_MAP.get(state_code, {})
    state_name = state_info.get("name", state_code or "Unknown")
    agency = state_info.get("agency", "State Agency")

    # --- Priority 1: ECHO document links ---
    for doc in echo_result.get("documentLinks", []):
        url = doc.get("url", "")
        if url and url.lower().endswith(".pdf"):
            urls.append({
                "url": url,
                "title": doc.get("title", "ECHO Document"),
                "type": doc.get("type", "permit_pdf"),
                "priority": 1,
                "source": "echo_document_link",
                "attempt_download": True,
            })

    # --- Priority 2: State-specific constructed URL patterns ---
    for pattern in state_info.get("direct_pdf_patterns", []):
        constructed_url = pattern.format(npdes=npdes)
        urls.append({
            "url": constructed_url,
            "title": f"{agency} Final Permit {npdes}",
            "type": "final_permit",
            "priority": 2,
            "source": f"{state_code.lower()}_constructed",
            "attempt_download": state_info.get("direct_download") != "portal_only",
        })

    # --- Priority 3: State portal reference (metadata only, no download) ---
    portal_url = state_info.get("portal", "")
    if portal_url:
        urls.append({
            "url": portal_url,
            "title": f"{state_name} {agency} — permit search portal for {npdes}",
            "type": "state_portal_reference",
            "priority": 3,
            "source": f"{state_code.lower()}_portal_reference",
            "attempt_download": False,
        })

    return sorted(urls, key=lambda x: x["priority"])


def _sanitize_path(p: str) -> str:
    """Strip absolute OS path prefixes, returning only the filename."""
    if isinstance(p, str) and ("\\" in p or (p.startswith("/") and len(p) > 1 and "/" in p[1:])):
        return Path(p).name
    return p


def _sanitize_sourcemeta_paths(obj) -> None:
    """Recursively sanitize sourceDoc paths in every _sourceMeta dict in the package."""
    if isinstance(obj, dict):
        if "_sourceMeta" in obj and isinstance(obj["_sourceMeta"], dict):
            sm = obj["_sourceMeta"]
            if "sourceDoc" in sm:
                sm["sourceDoc"] = _sanitize_path(sm["sourceDoc"])
        for v in obj.values():
            _sanitize_sourcemeta_paths(v)
    elif isinstance(obj, list):
        for item in obj:
            _sanitize_sourcemeta_paths(item)


def _dedup_chemicals_dosing(entries: list) -> list:
    """Keep only the most detailed entry per chemical type bucket (determined by tags).
    Also fills empty tags arrays with a baseline set.
    """
    seen_buckets: dict[tuple, dict] = {}
    for entry in entries:
        if not entry.get("tags"):
            entry["tags"] = ["public_seed", "chemicals"]
        type_tags = tuple(sorted(t for t in entry["tags"] if t not in ("public_seed", "chemicals")))
        key = type_tags or ("other",)
        if key not in seen_buckets or len(entry.get("value", "")) > len(seen_buckets[key].get("value", "")):
            seen_buckets[key] = entry
    return list(seen_buckets.values())


def assemble_seed_package(
    plant_name: str,
    location: str,
    npdes: str,
    owner: str,
    plant_id: str,
    echo_result: dict,
    parsed_facts: dict,
    documents: list[dict],
    sources_searched: list[dict],
    facts_skipped: list[dict],
    production: bool = False,
) -> dict:
    """Assemble the final Nymph seed package from all collected data."""

    # --- Identity ---
    echo_id = echo_result.get("identity", {})
    city = echo_id.get("city") or (location.split(",")[0].strip() if "," in location else None)
    state = echo_id.get("state") or state_abbrev(location, npdes)
    resolved_name = echo_id.get("plantName") or plant_name
    # Owner: CLI arg takes precedence, fall back to ECHO-derived permittee name
    resolved_owner = owner or echo_id.get("owner") or ""

    identity = {
        "plantName": resolved_name,
        "location": location,
        "city": city,
        "state": state,
        "zip": echo_id.get("zip"),
        "street": echo_id.get("street"),
        "county": echo_id.get("county"),
        "coordinates": echo_id.get("coordinates", {"lat": None, "lon": None}),
        "plantType": "wastewater",
        "npdesPermitNumber": npdes,
        "facilityStatus": echo_id.get("facilityStatus"),
        "areas": echo_id.get("areas"),
        "universe": echo_id.get("universe"),
        "naics": echo_id.get("naics", []),
        "sic": echo_id.get("sic", []),
        "permitExpirationDate": echo_result.get("permit", {}).get("expirationDate"),
        "confidence": "high" if echo_id.get("plantName") else "medium",
    }
    if resolved_owner:
        identity["owner"] = resolved_owner
    if plant_id:
        identity["plantId"] = plant_id

    # Fix 4 — receivingWaters: ECHO watershed names + permit-extracted classification
    receiving_water_classification = parsed_facts.get("basics", {}).get("receivingWaterClassification")
    watersheds = echo_result.get("watershed", [])
    receiving_waters = []
    for ws in watersheds:
        name = (
            ws.get("name") or ws.get("waterBodyName") or ws.get("watershedName")
            or ws.get("auName") or ws.get("wbdHu12Name") or ws.get("WBD_HUC12_Name") or ""
        ).strip()
        if not name:
            continue
        entry: dict = {"name": name, "source": "echo_api", "evidenceClass": "echo_api"}
        if receiving_water_classification:
            entry["classification"] = receiving_water_classification
            entry["classificationSource"] = "permit_pdf"
        receiving_waters.append(entry)
    if not receiving_waters and receiving_water_classification:
        receiving_waters.append({
            "classification": receiving_water_classification,
            "source": "permit_pdf",
            "evidenceClass": "direct_text_extraction",
        })
    if receiving_waters:
        identity["receivingWaters"] = receiving_waters

    # Fix 5 — outfalls: unique outfall IDs from permit limits, sorted numerically.
    # Map each outfall to a receiving water by index: outfall[0] → receiving_waters[0],
    # outfall[1] → receiving_waters[1], etc. Falls back to last receiving water for extras.
    limits_raw_for_outfalls = parsed_facts.get("permitLimits", [])
    if isinstance(limits_raw_for_outfalls, dict):
        limits_raw_for_outfalls = list(limits_raw_for_outfalls.values())
    seen_ofs: set = set()
    for _grp in limits_raw_for_outfalls:
        oid = _grp.get("outfall")
        if oid and str(oid).lower() != "null":
            seen_ofs.add(oid)

    def _outfall_sort_key(o: str) -> int:
        digits = re.sub(r"\D", "", o)
        return int(digits) if digits else 999

    sorted_ofs = sorted(seen_ofs, key=_outfall_sort_key)
    outfalls = []
    for i, oid in enumerate(sorted_ofs):
        rw = (receiving_waters[i] if i < len(receiving_waters)
              else receiving_waters[-1] if receiving_waters else None)
        of_entry: dict = {"id": oid, "source": "echo_api", "evidenceClass": "echo_api"}
        if rw and rw.get("name"):
            of_entry["receivingWater"] = rw["name"]
        outfalls.append(of_entry)
    if outfalls:
        identity["outfalls"] = outfalls

    # identityPatch — explicit plants-table write object (never hidden in profile JSON)
    identity_patch = {
        "plantName": resolved_name,
        "city": city,
        "state": state,
        "plantType": "wastewater",
        "zip": echo_id.get("zip"),
        "coordinates": echo_id.get("coordinates", {"lat": None, "lon": None}),
    }
    if resolved_owner:
        identity_patch["owner"] = resolved_owner

    # Fix 6 — licensed flag from ECHO facilityStatus
    facility_status = (echo_id.get("facilityStatus") or "").strip().lower()
    if facility_status:
        identity_patch["licensed"] = facility_status in ("effective", "admin continued")

    # --- Conflicts ---
    conflicts = list(parsed_facts.get("conflicts", []))
    echo_name = echo_result["identity"].get("plantName", "")
    if echo_name and echo_name.lower() != plant_name.lower():
        conflicts.append({
            "field": "identity.plantName",
            "values": [plant_name, echo_name],
            "resolved": f"Using ECHO-verified name: {echo_name}",
        })

    # --- Overview: basics ---
    basics = parsed_facts.get("basics", {})
    for flow_field in ["designFlowMGD", "averageFlowMGD", "peakFlowMGD"]:
        val = basics.get(flow_field)
        if isinstance(val, str):
            try:
                basics[flow_field] = float(re.sub(r"[^\d.]", "", val))
            except ValueError:
                basics[flow_field] = None

    # --- Overview: process train — normalise to liquid/solids keys ---
    pt_raw = parsed_facts.get("processTrain", {})
    # Ensure all nodes have confidence and evidenceClass (GPT-4o nodes may lack them)
    for _train in ("liquid", "solids"):
        for _node in pt_raw.get(_train, []):
            if "confidence" not in _node:
                _node["confidence"] = 0.55 if _node.get("status") == "inferred" else 0.90
            if not _node.get("evidenceClass"):
                _node["evidenceClass"] = "heuristic_inference" if _node.get("status") == "inferred" else "direct_text_extraction"
    process_train = {
        "liquid": pt_raw.get("liquid", pt_raw.get("liquidTrain", [])),
        "solids": pt_raw.get("solids", pt_raw.get("solidsTrain", [])),
        "connections": pt_raw.get("connections", []),
    }

    # --- Permit limits — normalise to array format ---
    # Primary source: ECHO effluent chart (pre-populated in parsed_facts["permitLimits"]).
    # Secondary source: gpt-4o extraction from permit PDF (merged via permit_parser).
    limits_raw = parsed_facts.get("permitLimits", [])
    if isinstance(limits_raw, dict):
        permit_limits = [{"parameter": p, **d} for p, d in limits_raw.items()]
    else:
        permit_limits = limits_raw

    # --- Nymph Logs ---
    nymph_logs = parsed_facts.get("nymphLogs", {
        "chemicalsDosing": [],
        "operatingHistory": [],
        "proceduresPreferences": [],
        "historicalOutcomes": [],
    })
    # Merge all ECHO-derived nymphLogs categories (pre-formed, source-backed entries)
    _DESIGN_FLOW_PATTERN = re.compile(r"design\s+flow|designflowmgd|\b\d+\.?\d*\s*mgd\b", re.IGNORECASE)
    echo_nymph_logs = echo_result.get("nymphLogs", {})
    for category in ("historicalOutcomes", "operatingHistory"):
        echo_entries = echo_nymph_logs.get(category, [])
        if not echo_entries:
            continue
        if category == "operatingHistory":
            echo_entries = [
                e for e in echo_entries
                if not _DESIGN_FLOW_PATTERN.search(e.get("value", ""))
            ]
        nymph_logs.setdefault(category, []).extend(echo_entries)

    # Deduplicate chemicalsDosing — keep most detailed entry per chemical type bucket
    if nymph_logs.get("chemicalsDosing"):
        nymph_logs["chemicalsDosing"] = _dedup_chemicals_dosing(nymph_logs["chemicalsDosing"])

    # --- Audit ---
    all_sources = sources_searched + echo_result.get("sourcesSearched", [])
    echo_warning_conflicts = [
        {"field": "echo_lookup", "values": [w], "resolved": "Review manually"}
        for w in echo_result.get("warnings", [])
    ]
    raw_conflicts = conflicts + echo_warning_conflicts

    # Fix 7 — strip local machine paths from conflict values and resolved field
    def _sanitize_conflict(c: dict) -> dict:
        if "values" not in c and "resolved" not in c:
            return c
        c2 = dict(c)
        if "values" in c2:
            c2["values"] = [
                Path(v).name if (isinstance(v, str) and ("\\" in v or (v.startswith("/") and len(v) > 1)))
                else v
                for v in c2["values"]
            ]
        if "resolved" in c2 and isinstance(c2["resolved"], str):
            c2["resolved"] = re.sub(
                r'[A-Za-z]:\\[^\s,;]+|(?:/[^/\s,;]+){2,}',
                lambda m: Path(m.group()).name,
                c2["resolved"],
            )
        return c2

    all_conflicts = [_sanitize_conflict(c) for c in raw_conflicts]

    liquid_count = len(process_train["liquid"])
    solids_count = len(process_train["solids"])
    limits_count = len(permit_limits)
    logs_count = sum(len(v) for v in nymph_logs.values())

    seed_summary_parts = [
        f"Seeded {liquid_count} liquid train node(s) and {solids_count} solids train node(s).",
        f"Extracted {limits_count} permit limit parameter(s).",
        f"Generated {logs_count} Nymph Log entry/entries from public sources.",
        f"Found {len(documents)} source document(s).",
    ]
    if not basics.get("designFlowMGD"):
        seed_summary_parts.append("Design flow could not be confirmed from public sources.")
    if not basics.get("processType"):
        seed_summary_parts.append("Process type not confirmed — please verify manually.")
    seed_summary_parts.append(
        "Operating targets were left empty — these are internal to the plant, not derived from permit limits. "
        "MOR (Monthly Operating Report) readings were left empty — no public plant-specific discharge monitoring data was ingested."
    )
    if facts_skipped:
        seed_summary_parts.append(
            "Skipped " + str(len(facts_skipped)) + " surface(s): " +
            "; ".join(f["surface"] for f in facts_skipped) + "."
        )

    _skipped_raw = facts_skipped + [
        {
            "surface": "processTrain.connections",
            "reason": (
                "RAS/WAS and recycle paths require process flow diagrams. "
                "NPDES permit documents do not contain machine-readable process diagrams. "
                "Connections array left empty — requires diagram parsing capability to populate."
            ),
        },
        {"surface": "operatingTargets", "reason": "Internal plant targets are not derived from public permit limits"},
        {"surface": "morReadings", "reason": "Monthly Operating Report data is not public plant-specific data for this facility"},
    ]
    _seen_skip_surfaces: set = set()
    skipped_with_non_goals = []
    for _s in _skipped_raw:
        _surf = _s.get("surface")
        if _surf not in _seen_skip_surfaces:
            _seen_skip_surfaces.add(_surf)
            skipped_with_non_goals.append(_s)

    run_audit = {
        "sourcesSearched": all_sources,
        "factsSkipped": skipped_with_non_goals,
        "conflicts": all_conflicts,
        "seedSummary": " ".join(seed_summary_parts),
        "watersheds": echo_result.get("watershed", []),
        "dmrLoadings": echo_result.get("dmrLoadings", []),
    }

    audit_run = {
        "worker": "plant_knowledge_public_seed",
        "proposalType": "plant_knowledge",
        "proposalStatus": "applied" if production else "pending_review",
        "metadata": {
            "npdesPermitNumber": npdes,
            "sourceDocuments": [d.get("sourceUrl") for d in documents if d.get("sourceUrl")],
            "conflictCount": len(all_conflicts),
            "conflictsRef": "see runAudit.conflicts for full detail",
            "skippedUnsupportedSurfaces": ["targets", "MOR readings"],
            "watershedCount": len(echo_result.get("watershed", [])),
            "dmrLoadingTypes": len(echo_result.get("dmrLoadings", [])),
        },
    }

    non_goals = {
        "operatingTargets": "leave_empty_unless_publicly_supported",
        "morReadings": "leave_empty_unless_public_machine_readable_data_exists",
    }

    package = {
        "identity": identity,
        "identityPatch": identity_patch,
        "overview": {
            "basics": basics,
            "processTrain": process_train,
        },
        "permitLimits": permit_limits,
        "nymphLogs": nymph_logs,
        "documents": documents,
        "runAudit": run_audit,
        "auditRun": audit_run,
        "nonGoals": non_goals,
    }
    # Fix 3 — strip all Windows absolute paths from every _sourceMeta.sourceDoc in the package
    _sanitize_sourcemeta_paths(package)
    return package


def print_review_note(package: dict):
    identity = package["identity"]
    overview = package["overview"]
    audit = package["runAudit"]
    audit_run = package["auditRun"]
    basics = overview["basics"]
    pt = overview["processTrain"]
    limits = package["permitLimits"]
    logs = package["nymphLogs"]

    print("\n" + "=" * 60)
    print("  NYMPH PLANT KNOWLEDGE SEED — HUMAN REVIEW NOTE")
    print("=" * 60)
    print(f"\n  Plant:  {identity['plantName']}")
    print(f"  NPDES:  {identity['npdesPermitNumber']}")
    print(f"  City:   {identity.get('city')}, {identity.get('state')} {identity.get('zip', '')}")
    print(f"  Coords: {identity['coordinates']}")
    print(f"  Confidence: {identity['confidence']}")
    print(f"  Status: {audit_run['proposalStatus']}")

    print(f"\n  Overview Basics:")
    print(f"    Process type:     {basics.get('processType')}")
    print(f"    Influent type:    {basics.get('influentType')}")
    print(f"    Design flow:      {basics.get('designFlowMGD')} MGD")
    print(f"    Average flow:     {basics.get('averageFlowMGD')} MGD")
    print(f"    Peak flow:        {basics.get('peakFlowMGD')} MGD")

    print(f"\n  Process Train:")
    for unit in sorted(pt.get("liquid", []), key=lambda u: u.get("order", 999)):
        print(f"    L{unit.get('order'):02d} [{unit.get('stage')}] {unit.get('label')} ({unit.get('type')}) — {unit.get('status')}")
    for unit in sorted(pt.get("solids", []), key=lambda u: u.get("order", 999)):
        print(f"    S{unit.get('order'):02d} [{unit.get('stage')}] {unit.get('label')} ({unit.get('type')}) — {unit.get('status')}")
    if pt.get("connections"):
        conn_strs = ["{}->{} ({})".format(c["from"], c["to"], c["flowType"]) for c in pt["connections"]]
        print(f"    Connections: {conn_strs}")

    print(f"\n  Permit Limits ({len(limits)} parameters):")
    for item in limits[:8]:
        param = item.get("parameter", "?")
        unit = item.get("unit", "?")
        limit_strs = []
        for lim in item.get("limits", [])[:2]:
            limit_strs.append(
                "{} {} {} ({} {})".format(
                    lim.get("qualifier", "<="), lim["value"], unit,
                    lim["limitType"], lim["statistic"]
                )
            )
        print(f"    {param}: {' | '.join(limit_strs)}")
    if len(limits) > 8:
        print(f"    ... and {len(limits) - 8} more")

    print(f"\n  Nymph Logs:")
    for cat in ["chemicalsDosing", "operatingHistory", "proceduresPreferences", "historicalOutcomes"]:
        entries = logs.get(cat, [])
        print(f"    {cat}: {len(entries)} entries")
        for entry in entries[:2]:
            print(f"      - [{entry.get('confidence', '?')}] {entry['value'][:100]}")

    print(f"\n  Documents: {len(package['documents'])}")
    for doc in package["documents"][:5]:
        print(f"    - {doc['filename']}: {doc.get('sourceUrl', 'no URL')[:80]}")

    print(f"\n  Identity Patch (plants table):")
    for k, v in package["identityPatch"].items():
        print(f"    {k}: {v}")

    print(f"\n  Audit:")
    print(f"    Sources searched: {len(audit['sourcesSearched'])}")
    print(f"    Conflicts:        {len(audit['conflicts'])}")
    print(f"    Facts skipped:    {len(audit['factsSkipped'])}")
    print(f"\n  Seed summary:")
    print(f"    {audit['seedSummary']}")

    if audit["conflicts"]:
        print(f"\n  Conflicts to review:")
        for c in audit["conflicts"]:
            print("    {}: {} -> {}".format(c["field"], c.get("values"), c.get("resolved")))

    print("\n" + "=" * 60)
    print("  DO NOT apply to production without reviewing the above.")
    print("  Verify: process train order, permit limit values, no")
    print("  operating targets populated, Nymph Logs are plant-specific.")
    print("=" * 60 + "\n")


def run_pipeline(
    plant_name: str,
    location: str,
    npdes: str,
    owner: str = "",
    plant_id: str = "",
    production: bool = False,
    output_path: str | None = None,
    skip_echo: bool = False,
    skip_pdf: bool = False,
    no_validate: bool = False,
) -> dict:
    """
    Core seeding pipeline — callable both from the CLI and from the FastAPI wrapper.
    Returns the assembled seed package dict.
    """
    npdes = npdes.strip().upper()
    plant_slug = slugify(plant_name)
    output_path = output_path or f"{npdes}_seed_package.json"

    print(f"\n{'='*60}")
    print(f"  Nymph Plant Knowledge Seeding Skill")
    print(f"  Plant: {plant_name}")
    print(f"  Location: {location}")
    print(f"  NPDES: {npdes}")
    print(f"{'='*60}\n")

    sources_searched = []
    documents = []
    facts_skipped = []
    parsed_facts = {
        "basics": {},
        "processTrain": {"liquid": [], "solids": [], "connections": []},
        "permitLimits": [],
        "nymphLogs": {"chemicalsDosing": [], "operatingHistory": [], "proceduresPreferences": [], "historicalOutcomes": []},
        "conflicts": [],
        "extractionSummary": "",
    }

    # --- Step 1: ECHO lookup ---
    echo_result = {
        "identity": {
            "plantName": None, "street": None, "city": None, "state": None,
            "zip": None, "county": None, "coordinates": {"lat": None, "lon": None},
            "facilityStatus": None, "areas": None, "universe": None,
            "owner": None, "naics": [], "sic": [],
        },
        "permit": {"status": None, "expirationDate": None},
        "nymphLogs": {"historicalOutcomes": []},
        "watershed": [],
        "dmrLoadings": [],
        "documentLinks": [],
        "sourcesSearched": [],
        "compliance": {"inspections": [], "violations": []},
        "warnings": [],
    }

    if not skip_echo:
        print("[RUNNER] Step 1: ECHO API lookup")
        try:
            echo_result = echo_mod.lookup_facility_by_npdes(npdes, plant_name=plant_name)
            outcomes = echo_result.get("nymphLogs", {}).get("historicalOutcomes", [])
            print(f"[RUNNER] ECHO DFR returned {len(outcomes)} historicalOutcomes log entries")
        except Exception as exc:
            print(f"[RUNNER] WARNING: ECHO DFR lookup failed — {exc}")
            echo_result["warnings"].append(str(exc))

        # Permit limits from ECHO effluent chart API (runs alongside DFR)
        print("[RUNNER] Step 1b: ECHO effluent chart (permit limits)")
        echo_permit_limits = []
        try:
            echo_permit_limits, _excluded_params = echo_mod.get_permit_limits(npdes)
            if echo_permit_limits:
                parsed_facts["permitLimits"] = echo_permit_limits
                print(f"[RUNNER] Loaded {len(echo_permit_limits)} permit limit group(s) from ECHO")
            for _ep in _excluded_params:
                facts_skipped.append({
                    "surface": f"permitLimits.{_ep}",
                    "reason": "Parameter excluded — no numeric limit values found in ECHO effluent chart; monitoring-only or pollutant scan parameter.",
                })
        except Exception as exc:
            print(f"[RUNNER] WARNING: ECHO permit limits fetch failed — {exc}")

        # proceduresPreferences inferred from permit limits
        print("[RUNNER] Step 1c: Inferring proceduresPreferences from permit limits")
        try:
            echo_procedures = echo_mod.get_procedures_from_limits(echo_permit_limits, npdes)
            if echo_procedures:
                parsed_facts["nymphLogs"]["proceduresPreferences"].extend(echo_procedures)
        except Exception as exc:
            print(f"[RUNNER] WARNING: proceduresPreferences inference failed — {exc}")
    else:
        print("[RUNNER] Step 1: Skipped (--skip-echo)")

    # --- Step 2: Locate and download permit documents ---
    extracted_files = []

    if not skip_pdf:
        print("\n[RUNNER] Step 2: Locating and downloading permit documents")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("[RUNNER] WARNING: OPENAI_API_KEY not set — PDF parsing will be skipped")
            skip_pdf = True

        if not skip_pdf:
            state_code = npdes_state_code(npdes)

            # Alabama: use Playwright automation on the ADEM eFile portal.
            # Direct HTTP downloads to adem.state.al.us and epa.adem.alabama.gov always
            # time out; the portal requires browser-based AJAX form submission.
            if state_code == "AL":
                print("[RUNNER] Alabama permit — using ADEM eFile Playwright automation")
                try:
                    import adem_playwright as adem_mod
                    pdf_path, adem_error = adem_mod.download_permit_pdf_sync(npdes, output_dir=str(DOWNLOADS_DIR))
                    if pdf_path:
                        print(f"[RUNNER] ADEM eFile download successful: {pdf_path}")
                        sources_searched.append({
                            "url": "https://app.adem.alabama.gov/eFile/",
                            "type": "adem_efile_playwright",
                            "fetchedAt": NOW,
                            "resultSummary": f"Downloaded via Playwright: {pdf_path}",
                        })
                        try:
                            with open(pdf_path, "rb") as f:
                                pdf_bytes = f.read()
                            print(f"[RUNNER] Passing to pdf_extractor: {pdf_path} ({len(pdf_bytes):,} bytes)")
                            extraction = pdf_mod.extract_text(pdf_bytes, source_path=str(pdf_path))
                            extraction["source"] = pdf_path
                            tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
                            tmp.close()
                            with open(tmp.name, "w", encoding="utf-8") as f:
                                json.dump(extraction, f, indent=2)
                            extracted_files.append(tmp.name)
                            _text_preview = "".join(
                                p.get("text", "") for p in extraction.get("pages", [])
                            )[:5000]
                            documents.append({
                                "filename": Path(pdf_path).name,
                                "fileType": "document",
                                "mimeType": "application/pdf",
                                "pageCount": extraction.get("totalPages"),
                                "ingestionTimestamp": NOW,
                                "tags": ["permit", "npdes", "final_permit", "public_seed", "adem"],
                                "sourceUrl": "https://app.adem.alabama.gov/eFile/",
                                "description": f"ADEM Final Permit {npdes}",
                                "extractedText": _text_preview,
                            })
                        except Exception as exc:
                            print(f"[RUNNER] WARNING: PDF text extraction failed — {exc}")
                    else:
                        print(f"[RUNNER] ADEM eFile: {adem_error}")
                        sources_searched.append({
                            "url": "https://app.adem.alabama.gov/eFile/",
                            "type": "adem_efile_playwright",
                            "fetchedAt": NOW,
                            "resultSummary": f"Failed: {adem_error}",
                        })
                        # Record portal reference for manual navigation
                        sources_searched.append({
                            "url": NPDES_STATE_MAP["AL"]["portal"],
                            "type": "al_portal_reference",
                            "fetchedAt": NOW,
                            "resultSummary": "Portal reference — manual navigation required",
                        })
                except Exception as exc:
                    print(f"[RUNNER] WARNING: adem_playwright import/run failed — {exc}")
                    sources_searched.append({
                        "url": "https://app.adem.alabama.gov/eFile/",
                        "type": "adem_efile_playwright",
                        "fetchedAt": NOW,
                        "resultSummary": f"Error: {exc}",
                    })

            else:
                # Non-Alabama: try constructed URL patterns then fall back to portal reference
                pdf_urls = find_permit_pdf_urls(echo_result, npdes, location)
                print(f"[RUNNER] {len(pdf_urls)} candidate URL(s) to try")

                for candidate in pdf_urls[:6]:
                    url = candidate["url"]
                    attempt = candidate.get("attempt_download", True)

                    if not attempt:
                        print(f"[RUNNER] Portal reference (no download): {url}")
                        sources_searched.append({
                            "url": url,
                            "type": candidate.get("source", "state_portal_reference"),
                            "fetchedAt": NOW,
                            "resultSummary": "Portal reference — manual navigation required to retrieve permit PDF",
                        })
                        continue

                    print(f"[RUNNER] Probing: {url}")
                    sources_searched.append({
                        "url": url,
                        "type": candidate.get("source", "state_portal"),
                        "fetchedAt": NOW,
                        "resultSummary": "probing",
                    })

                    try:
                        pdf_bytes = pdf_mod.download_pdf(url)
                    except Exception as exc:
                        print(f"[RUNNER] Could not download {url} — {exc}")
                        sources_searched[-1]["resultSummary"] = f"Failed: {exc}"
                        continue

                    sources_searched[-1]["resultSummary"] = f"Downloaded {len(pdf_bytes):,} bytes"

                    suffix = url.split("/")[-1] or f"{npdes}_document.pdf"
                    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
                    tmp.close()
                    extraction = pdf_mod.extract_text(pdf_bytes, source_path=url)
                    extraction["source"] = url
                    with open(tmp.name, "w", encoding="utf-8") as f:
                        json.dump(extraction, f, indent=2)
                    extracted_files.append(tmp.name)

                    _text_preview = "".join(
                        p.get("text", "") for p in extraction.get("pages", [])
                    )[:5000]
                    documents.append({
                        "filename": suffix,
                        "fileType": "document",
                        "mimeType": "application/pdf",
                        "pageCount": extraction.get("totalPages"),
                        "ingestionTimestamp": NOW,
                        "tags": ["permit", "npdes", candidate.get("type", "document"), "public_seed"],
                        "sourceUrl": url,
                        "description": candidate.get("title"),
                        "extractedText": _text_preview,
                    })

                    if len(extracted_files) >= 2:
                        break
    else:
        print("[RUNNER] Step 2: Skipped (--skip-pdf)")
        facts_skipped.append({"surface": "permitDocuments", "reason": "--skip-pdf flag set"})
        facts_skipped.append({"surface": "processTrain", "reason": "No permit documents downloaded"})
        facts_skipped.append({"surface": "permitLimits", "reason": "No permit documents downloaded"})

    # --- Step 3: Parse permit text with gpt-4o ---
    if extracted_files:
        print(f"\n[RUNNER] Step 3: Parsing {len(extracted_files)} document(s) with gpt-4o")
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())

        all_doc_results = []
        all_extracted = []
        for extracted_file in extracted_files:
            with open(extracted_file, "r", encoding="utf-8") as f:
                extracted = json.load(f)
            all_extracted.append(extracted)
            try:
                result = parser_mod.extract_facts_from_permit(
                    extracted=extracted,
                    npdes=npdes,
                    source_doc=extracted.get("source", extracted_file),
                    plant_slug=plant_slug,
                    client=client,
                )
                all_doc_results.append(result)
            except Exception as exc:
                print(f"[RUNNER] WARNING: Parsing failed for {extracted_file} — {exc}")

        if all_doc_results:
            # Merge all parser results into one
            if len(all_doc_results) == 1:
                parser_result = all_doc_results[0]
            else:
                parser_result = parser_mod.merge_extracted_facts(all_doc_results)

            # Overlay parser result onto parsed_facts WITHOUT losing ECHO permit limits.
            # basics: take parser values where non-null (design flow, process type, etc.)
            parser_basics = parser_result.get("basics", {})
            for field in ["processType", "influentType", "designFlowMGD", "averageFlowMGD", "peakFlowMGD", "_sourceMeta"]:
                if parser_basics.get(field) is not None:
                    parsed_facts["basics"][field] = parser_basics[field]

            # processTrain: take parser result (ECHO has none)
            if parser_result.get("processTrain"):
                parsed_facts["processTrain"] = parser_result["processTrain"]

            # permitLimits: use ECHO limits as base; for each parser limit try to patch
            # frequency/sampleType onto the matching ECHO entry (fuzzy name match), or
            # append if no match found.
            echo_limits = parsed_facts.get("permitLimits", [])
            for plim in parser_result.get("permitLimits", []):
                param = (plim.get("parameter") or "").lower()
                outfall = plim.get("outfall")
                # Build expanded keyword set: raw words + known aliases from _PARAM_KEY_KEYWORDS
                param_words = set(w for w in param.split() if len(w) >= 2)
                for _key, _aliases in _PARAM_KEY_KEYWORDS.items():
                    if any(a in param for a in _aliases) or _key in param:
                        param_words.update(_aliases)
                        break
                existing = next(
                    (e for e in echo_limits
                     if e.get("outfall") == outfall
                     and any(w in (e.get("parameter") or "").lower() for w in param_words)),
                    None,
                )
                if existing is not None:
                    # Patch frequency/sampleType onto matching ECHO limits
                    for plim_lim in plim.get("limits", []):
                        for echo_lim in existing.get("limits", []):
                            if (echo_lim.get("limitType") == plim_lim.get("limitType")
                                    and echo_lim.get("statistic") == plim_lim.get("statistic")):
                                if plim_lim.get("frequency") and not echo_lim.get("frequency"):
                                    echo_lim["frequency"] = plim_lim["frequency"]
                                if plim_lim.get("sampleType") and not echo_lim.get("sampleType"):
                                    echo_lim["sampleType"] = plim_lim["sampleType"]
                else:
                    # Only append GPT-4o limit when no ECHO entry with the same parameter exists at all.
                    # This prevents null-outfall GPT-4o extractions (e.g. CBOD5, NH3-N) from
                    # duplicating ECHO entries that already carry proper outfall IDs.
                    if not any(
                        any(w in (e.get("parameter") or "").lower() for w in param_words)
                        for e in echo_limits
                    ):
                        echo_limits.append(plim)
            parsed_facts["permitLimits"] = echo_limits

            # nymphLogs: accumulate unique entries from parser (avoid duplicates already from ECHO)
            for category in ["chemicalsDosing", "operatingHistory", "proceduresPreferences", "historicalOutcomes"]:
                existing_vals = {e["value"] for e in parsed_facts["nymphLogs"].get(category, [])}
                for entry in parser_result.get("nymphLogs", {}).get(category, []):
                    if entry.get("value") not in existing_vals:
                        parsed_facts["nymphLogs"].setdefault(category, []).append(entry)
                        existing_vals.add(entry["value"])

            # conflicts
            parsed_facts.setdefault("conflicts", []).extend(parser_result.get("conflicts", []))

        # Step 3b: Supplemental regex-based OCR extraction (Fixes 2–8)
        # Runs on every extracted document regardless of whether gpt-4o succeeded.
        print("[RUNNER] Step 3b: Supplemental OCR extraction (regex pass)")
        for extracted in all_extracted:
            try:
                supp = _extract_supplemental_from_ocr(
                    extracted,
                    plant_slug,
                    REFERENCES_DIR / "process_unit_vocab.json",
                    NOW,
                )
                _apply_supplemental(parsed_facts, supp, NOW)
                basics = supp.get("basics", {})
                liquid_n = len(supp["processTrain"]["liquid"])
                solids_n = len(supp["processTrain"]["solids"])
                chems_n  = len(supp["nymphLogs"]["chemicalsDosing"])
                procs_n  = len(supp["nymphLogs"]["proceduresPreferences"])
                print(
                    f"[RUNNER] Supplemental: processType={basics.get('processType')}, "
                    f"influentType={basics.get('influentType')}, "
                    f"designFlow={basics.get('designFlowMGD')}, "
                    f"avgFlow={basics.get('averageFlowMGD')}, "
                    f"peakFlow={basics.get('peakFlowMGD')}, "
                    f"liquid={liquid_n}, solids={solids_n}, "
                    f"chems={chems_n}, procs={procs_n}"
                )
                # Fix 7 — add to factsSkipped if avg/peak flow not found anywhere
                if not parsed_facts["basics"].get("averageFlowMGD"):
                    facts_skipped.append({
                        "surface": "overview.basics.averageFlowMGD",
                        "reason": "averageFlowMGD — not stated in final permit or fact sheet; may be in permit application.",
                    })
                if not parsed_facts["basics"].get("peakFlowMGD"):
                    facts_skipped.append({
                        "surface": "overview.basics.peakFlowMGD",
                        "reason": "peakFlowMGD — not stated in final permit or fact sheet; may be in permit application.",
                    })
            except Exception as exc:
                print(f"[RUNNER] WARNING: Supplemental OCR extraction failed — {exc}")

        # Regex fallback for receivingWaterClassification when GPT-4o fails to extract it
        _rw_names = []
        for _ws in echo_result.get("watershed", []):
            _n = (
                _ws.get("name") or _ws.get("waterBodyName") or _ws.get("watershedName")
                or _ws.get("auName") or _ws.get("wbdHu12Name") or _ws.get("WBD_HUC12_Name") or ""
            ).strip()
            if _n:
                _rw_names.append(_n)
        _extract_water_classification_from_ocr(all_extracted, _rw_names, parsed_facts, facts_skipped)

        # Fix 2 — POTW influentType backstop: NAICS 221320 / SIC 4952 + permit text confirms municipal/domestic
        if parsed_facts["basics"].get("influentType") in ("Mixed", None, ""):
            _echo_id_ref = echo_result.get("identity", {})
            _naics_codes = [str(n.get("code", n) if isinstance(n, dict) else n) for n in _echo_id_ref.get("naics", [])]
            _sic_codes   = [str(s.get("code", s) if isinstance(s, dict) else s) for s in _echo_id_ref.get("sic", [])]
            _is_potw = "221320" in _naics_codes or "4952" in _sic_codes
            if _is_potw:
                _full_permit = "".join(
                    p.get("text", "") for ext in all_extracted for p in ext.get("pages", [])
                ).lower()
                if "municipal" in _full_permit or "domestic" in _full_permit:
                    parsed_facts["basics"]["influentType"] = "Domestic"
                    print("[RUNNER] influentType set to 'Domestic' — NAICS/SIC confirms POTW and permit text contains 'municipal'/'domestic'")

        # Infer standard unit processes for mechanical/AS plants
        inferred = _infer_standard_unit_processes(parsed_facts, plant_slug, NOW)
        if inferred:
            print(f"[RUNNER] Inferred {inferred} standard unit process node(s) for process type '{parsed_facts['basics'].get('processType')}'")

        # Apply state-specific standard monitoring defaults to ECHO limits still missing freq/sampleType
        patched = _apply_state_monitoring_defaults(parsed_facts, state_code, facts_skipped)
        if patched:
            print(f"[RUNNER] Applied {state_code} standard monitoring defaults to {patched} limit(s)")

        # Normalize all frequency values to consistent Title Case
        _normalize_frequencies(parsed_facts)

        # Peracetic Acid — not in state defaults; flag if freq/sampleType still missing
        for _grp in parsed_facts.get("permitLimits", []):
            _pl = (_grp.get("parameter") or "").lower()
            if "peracetic" in _pl or "paa" in _pl:
                if any(not _lim.get("sampleType") or not _lim.get("frequency") for _lim in _grp.get("limits", [])):
                    facts_skipped.append({
                        "surface": f"permitLimits.{_grp.get('parameter', 'Peracetic Acid')}.sampleType",
                        "reason": "Peracetic Acid monitoring schedule not in state defaults; extract from permit PDF manually.",
                    })

        # Fix 4 — flag BOD/Ammonia limits where sampleType is still missing (must come from permit PDF)
        _bod_nh3_keywords = [
            (["bod", "carbonaceous", "biochemical"], "BOD Carbonaceous"),
            (["ammonia", "nitrogen, ammonia"],        "Nitrogen, Ammonia Total"),
        ]
        for _kws, _label in _bod_nh3_keywords:
            for _grp in parsed_facts.get("permitLimits", []):
                _pl = (_grp.get("parameter") or "").lower()
                if not any(_kw in _pl for _kw in _kws):
                    continue
                if any(not _lim.get("sampleType") for _lim in _grp.get("limits", [])):
                    facts_skipped.append({
                        "surface": f"permitLimits.{_grp.get('parameter', _label)}.sampleType",
                        "reason": "sampleType not found in Document AI table extraction for this parameter. "
                                  "Expected '24-Hr Composite' for weekly average per ADEM permit standard.",
                    })
                    break
    else:
        print("\n[RUNNER] Step 3: No documents to parse")
        if not skip_pdf:
            facts_skipped.append({"surface": "processTrain", "reason": "No permit PDFs could be downloaded from public sources"})
            facts_skipped.append({"surface": "permitLimits", "reason": "No permit PDFs could be downloaded from public sources"})
            facts_skipped.append({"surface": "nymphLogs", "reason": "No permit PDFs could be downloaded from public sources"})

    # --- Step 4: Assemble seed package ---
    print("\n[RUNNER] Step 4: Assembling seed package")
    package = assemble_seed_package(
        plant_name=plant_name,
        location=location,
        npdes=npdes,
        owner=owner,
        plant_id=plant_id,
        echo_result=echo_result,
        parsed_facts=parsed_facts,
        documents=documents,
        sources_searched=sources_searched,
        facts_skipped=facts_skipped,
        production=production,
    )

    # --- Step 5: Validate ---
    if not no_validate:
        print("\n[RUNNER] Step 5: Validation")
        errors, warnings = validate_seed_package(package)
        print_report(package, errors, warnings)
        if errors:
            print("[RUNNER] Validation errors found — seed package saved but flagged. Review before applying.")
    else:
        print("\n[RUNNER] Step 5: Validation skipped (--no-validate)")

    # --- Step 6: Write output ---
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2, default=str)
    print(f"\n[RUNNER] Seed package saved to: {output_path}")

    # --- Step 7: Human review note ---
    print_review_note(package)

    return package


def main():
    """CLI entry point — parses arguments and delegates to run_pipeline()."""
    parser = argparse.ArgumentParser(description="Nymph Plant Knowledge Seeding Skill")
    parser.add_argument("--plant",      required=True, help="Full plant name")
    parser.add_argument("--location",   required=True, help="City, State (e.g. 'Arab, Alabama')")
    parser.add_argument("--npdes",      required=True, help="NPDES permit number (e.g. AL0056626)")
    parser.add_argument("--owner",      default="",    help="Permittee/utility name")
    parser.add_argument("--plant-id",   default="",    help="Nymph internal plant ID")
    parser.add_argument("--production", action="store_true", help="Mark package as applied")
    parser.add_argument("--output",     help="Output JSON file path")
    parser.add_argument("--skip-echo",  action="store_true", help="Skip ECHO API lookup")
    parser.add_argument("--skip-pdf",   action="store_true", help="Skip PDF download and parsing")
    parser.add_argument("--no-validate",action="store_true", help="Skip validation step")
    parser.add_argument("--verbose",    action="store_true", help="Verbose output")
    args = parser.parse_args()

    return run_pipeline(
        plant_name=args.plant,
        location=args.location,
        npdes=args.npdes,
        owner=args.owner,
        plant_id=args.plant_id,
        production=args.production,
        output_path=args.output,
        skip_echo=args.skip_echo,
        skip_pdf=args.skip_pdf,
        no_validate=args.no_validate,
    )


if __name__ == "__main__":
    main()
