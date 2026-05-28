"""
table_extractor.py — Extract structured table rows from permit PDF text.

Supplements pdf_extractor.py by finding and parsing effluent limits tables,
unit process summary tables, and monitoring frequency tables. Works on the
JSON output from pdf_extractor.py.

Usage:
    python table_extractor.py --input extracted.json --output tables.json
    python table_extractor.py --input extracted.json --npdes AL0056626
"""

import argparse
import json
import re
import sys
from pathlib import Path


# Column header patterns for effluent limits tables
LIMITS_HEADERS = [
    r"parameter",
    r"effluent\s+limit",
    r"monthly\s+(average|limit)",
    r"weekly\s+(average|limit)",
    r"daily\s+(max|maximum|limit)",
    r"unit(s)?",
    r"sample\s+type",
    r"sample\s+freq",
    r"monitoring\s+freq",
]

# Patterns for known parameter names
PARAMETER_PATTERNS = [
    r"\bBOD\b", r"\bCBOD\b", r"\bTSS\b", r"\bTDS\b",
    r"\bpH\b", r"\bDO\b",
    r"ammonia|NH3|NH4",
    r"nitrate|NO3",
    r"total\s+nitrogen|TN\b",
    r"total\s+phosphorus|TP\b",
    r"E\.\s*coli|fecal\s+coliform|enterococcus",
    r"chlorine|Cl2",
    r"flow|discharge",
    r"turbidity",
    r"oil\s+and\s+grease",
    r"total\s+dissolved\s+solids",
]
PARAMETER_RE = re.compile("|".join(PARAMETER_PATTERNS), re.IGNORECASE)

# Number pattern: matches values like 5, 5.0, 30, 200, 1000
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

# Unit patterns
UNIT_RE = re.compile(
    r"\bmg/[Ll]\b|\bCFU/100\s*m[Ll]\b|\blbs?/day\b|\b[Ss][Uu]\b|\bNTU\b|\bMGD\b|\bm[Ll]/[Ll]\b",
    re.IGNORECASE
)


def detect_table_pages(extracted: dict) -> list[dict]:
    """
    Find pages that likely contain effluent limits tables or unit process tables.
    Returns list of {page, text, table_type}.
    """
    table_pages = []
    for page_obj in extracted.get("pages", []):
        text = page_obj.get("text", "")
        text_lower = text.lower()
        score = 0
        table_type = None

        # Score for effluent limits table
        for pattern in LIMITS_HEADERS:
            if re.search(pattern, text_lower):
                score += 1

        if score >= 3:
            table_type = "effluent_limits"
        elif "unit process" in text_lower or "treatment unit" in text_lower:
            table_type = "unit_process"
        elif "monitoring" in text_lower and "frequency" in text_lower:
            table_type = "monitoring_schedule"

        if table_type:
            table_pages.append({
                "page": page_obj["page"],
                "text": text,
                "table_type": table_type,
                "score": score,
            })

    return table_pages


def extract_limit_rows(text: str, page: int, source_doc: str) -> list[dict]:
    """
    Parse effluent limit rows from a table page.
    Returns list of structured limit row dicts.
    """
    rows = []
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    for line in lines:
        # Skip header lines
        line_lower = line.lower()
        if any(re.search(p, line_lower) for p in ["parameter", "effluent limit", "monitoring"]):
            continue

        # Line must mention a known parameter
        if not PARAMETER_RE.search(line):
            continue

        numbers = NUMBER_RE.findall(line)
        units = UNIT_RE.findall(line)

        if not numbers:
            continue

        # Extract parameter name — first word or phrase matching a known parameter
        param_match = PARAMETER_RE.search(line)
        parameter = param_match.group(0).strip() if param_match else None

        # Detect statistic keywords
        statistic = None
        if re.search(r"monthly\s+avg|monthly\s+average|30.day", line_lower):
            statistic = "average"
        elif re.search(r"daily\s+max|maximum\s+day|max\b", line_lower):
            statistic = "maximum"
        elif re.search(r"geometric|geomean", line_lower):
            statistic = "geometric_mean"
        elif re.search(r"minimum|min\b", line_lower):
            statistic = "minimum"

        # Detect sample type
        sample_type = None
        if re.search(r"composite", line_lower):
            sample_type = "Composite"
        elif re.search(r"grab", line_lower):
            sample_type = "Grab"

        # Detect frequency
        frequency = None
        for freq_token in ["daily", "weekly", "monthly", "quarterly", "continuous"]:
            if freq_token in line_lower:
                frequency = freq_token.capitalize()
                break

        row = {
            "parameter": parameter,
            "rawLine": line,
            "page": page,
            "numbersFound": [float(n) for n in numbers],
            "unitsFound": units,
            "statistic": statistic,
            "sampleType": sample_type,
            "frequency": frequency,
            "_sourceMeta": {
                "sourceDoc": source_doc,
                "sourcePage": page,
                "confidence": 0.7,
                "extractionMethod": "regex_table_parser",
            },
        }
        rows.append(row)

    return rows


def extract_unit_process_rows(text: str, page: int) -> list[dict]:
    """
    Extract unit process mentions from a Form 2A-style table.
    Returns list of {unit, description, page}.
    """
    rows = []
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    unit_keywords = [
        "screening", "grit", "equalization", "primary clarif",
        "aeration", "oxidation ditch", "activated sludge", "sbr",
        "secondary clarif", "trickling filter", "mbr", "lagoon",
        "uv", "chlorination", "disinfection", "dechlorination", "filtration",
        "thickener", "digester", "belt press", "centrifuge", "biosolids",
    ]

    for line in lines:
        line_lower = line.lower()
        for kw in unit_keywords:
            if kw in line_lower:
                rows.append({
                    "unit": kw.title(),
                    "rawLine": line,
                    "page": page,
                })
                break

    return rows


def extract_tables(extracted: dict, source_doc: str = "unknown") -> dict:
    """
    Main extraction entry point.
    Returns structured table data extracted from the permit text.
    """
    table_pages = detect_table_pages(extracted)
    limit_rows = []
    unit_rows = []

    for tp in table_pages:
        if tp["table_type"] == "effluent_limits":
            limit_rows.extend(extract_limit_rows(tp["text"], tp["page"], source_doc))
        elif tp["table_type"] == "unit_process":
            unit_rows.extend(extract_unit_process_rows(tp["text"], tp["page"]))

    return {
        "source": source_doc,
        "tablePagesFound": len(table_pages),
        "limitRowsExtracted": len(limit_rows),
        "unitRowsExtracted": len(unit_rows),
        "limitRows": limit_rows,
        "unitRows": unit_rows,
        "tablePages": [{"page": tp["page"], "tableType": tp["table_type"]} for tp in table_pages],
    }


def main():
    parser = argparse.ArgumentParser(description="Extract table rows from permit PDF extraction JSON")
    parser.add_argument("--input", required=True, help="JSON output from pdf_extractor.py")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--npdes", help="NPDES number for metadata")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        extracted = json.load(f)

    source_doc = extracted.get("source", args.input)
    result = extract_tables(extracted, source_doc=source_doc)

    print(f"[TABLE] Pages with tables: {result['tablePagesFound']}")
    print(f"[TABLE] Limit rows extracted: {result['limitRowsExtracted']}")
    print(f"[TABLE] Unit process rows: {result['unitRowsExtracted']}")

    if result["limitRows"]:
        print("\n[TABLE] Limit rows preview:")
        for row in result["limitRows"][:5]:
            print(f"  {row['parameter']} — numbers: {row['numbersFound']} units: {row['unitsFound']} stat: {row['statistic']}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n[TABLE] Table data saved to {args.output}")
    else:
        print("\n--- Table Extraction Result ---")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
