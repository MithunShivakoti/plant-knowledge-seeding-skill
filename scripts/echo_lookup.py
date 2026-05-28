"""
echo_lookup.py — Query EPA ECHO DFR API for facility data by NPDES permit number.

Extracts and maps the following from the DFR response:
  - Identity: facility name, address, county, coordinates, permit status, areas,
              universe, owner/permittee, NAICS, SIC
  - Inspections  → nymphLogs.historicalOutcomes (one entry per inspection)
  - CWARNC violations → nymphLogs.historicalOutcomes (one entry per violating quarter)
  - Watershed data → result.watershed (receiving water bodies, HUC codes, impaired params, ESA flag)
  - DMR pollutant loadings → result.dmrLoadings
  - Permit limits → get_permit_limits() via eff_rest_services.get_effluent_chart

Usage:
    python echo_lookup.py --npdes AL0056626
    python echo_lookup.py --npdes AL0056626 --plant "Gilliam Creek WWTP" --output echo.json
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from pathlib import Path

ECHO_DFR_BASE = "https://echodata.epa.gov/echo"
REQUEST_TIMEOUT = 45
NOW = datetime.now(timezone.utc).isoformat()

# Parameter aliases — loaded from references/parameter_aliases.json
_ALIASES_PATH = Path(__file__).parent.parent / "references" / "parameter_aliases.json"
try:
    with open(_ALIASES_PATH, "r", encoding="utf-8") as _f:
        _PARAM_ALIASES: list[dict] = json.load(_f)
except Exception:
    _PARAM_ALIASES = []


def _lookup_aliases(parameter: str) -> list[str]:
    """Return known aliases for a parameter by substring-matching against the aliases table."""
    param_lower = (parameter or "").lower()
    for entry in _PARAM_ALIASES:
        if entry.get("pattern", "").lower() in param_lower:
            return list(entry.get("aliases", []))
    return []


def _get(url: str, params: dict, timeout: int = REQUEST_TIMEOUT) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NymphSeedAgent/1.0)",
        "Accept": "application/json",
    }
    resp = requests.get(url, params=params, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _safe_float(val) -> Optional[float]:
    """Convert a value to float or return None."""
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _pick_icis_permit(permits: list[dict], npdes: str) -> Optional[dict]:
    """
    Return the ICIS-NPDES permit record whose SourceID matches the NPDES number exactly.
    This is the authoritative permit record for effluent data, coordinates, NAICS, SIC.
    """
    npdes_upper = npdes.upper()
    for p in permits:
        if p.get("EPASystem") == "ICIS-NPDES" and p.get("SourceID", "").upper() == npdes_upper:
            return p
    # Fallback: first ICIS record
    for p in permits:
        if p.get("EPASystem") == "ICIS-NPDES":
            return p
    return permits[0] if permits else None


def _pick_frs_record(permits: list[dict]) -> Optional[dict]:
    """Return the FRS record (most precise GPS coordinates, facility street address)."""
    for p in permits:
        if p.get("EPASystem") == "FRS":
            return p
    return None


def _pick_owner_record(permits: list[dict], npdes: str) -> Optional[dict]:
    """
    Return the associated permit record (permittee/owner entity).
    These have Universe containing 'Associated Permit Record' and a SourceID
    that is a variant of the NPDES number (e.g. ALL061671 for AL0061671).
    """
    for p in permits:
        universe = p.get("Universe", "")
        if "Associated Permit Record" in universe:
            return p
    return None


def _build_log_entry(value: str, source_doc: str, source_url: str,
                     tags: list[str], confidence: float = 0.95) -> dict:
    return {
        "value": value,
        "source": "echo_api",
        "evidenceClass": "echo_api",
        "confidence": confidence,
        "status": "confirmed",
        "tags": tags,
        "lastUpdated": NOW,
        "_sourceMeta": {
            "sourceDoc": source_doc,
            "sourcePage": None,
            "sourceUrl": source_url,
        },
    }


def _parse_inspections(results_block: dict, npdes: str, echo_url: str) -> list[dict]:
    """
    Map ComplianceHistory.Inspection records to nymphLogs.historicalOutcomes entries.
    Each inspection becomes one log entry.
    """
    entries = []
    inspections = results_block.get("ComplianceHistory", {}).get("Inspection", [])
    if not isinstance(inspections, list):
        return entries

    for insp in inspections:
        date = insp.get("Date", "unknown date")
        insp_type = insp.get("InspectionType", "inspection")
        lead = insp.get("LeadAgency", "")
        activity = insp.get("ActivityType", "")
        snc = insp.get("SncFlag", "N")
        finding = insp.get("Finding")

        lead_str = f"{lead} agency" if lead else "agency"
        snc_str = " — Significant Non-Compliance flag raised" if snc == "Y" else ""
        finding_str = f" Finding: {finding}." if finding else ""

        value = (
            f"{lead_str.title()} {activity.lower() if activity else 'inspection'} conducted on {date}"
            f" — type: {insp_type}{snc_str}.{finding_str}"
        )
        entries.append(_build_log_entry(
            value=value,
            source_doc=f"EPA ECHO ComplianceHistory — NPDES {npdes}",
            source_url=echo_url,
            tags=["public_seed", "inspection", "compliance", "echo"],
        ))
    return entries


def _parse_cwarnc_violations(results_block: dict, npdes: str, echo_url: str) -> list[dict]:
    """
    Map CWARNCCompliance quarter violation status to nymphLogs.historicalOutcomes entries.
    Only non-null, non-'No Viol' statuses become entries.
    """
    entries = []
    cwarnc = results_block.get("CWARNCCompliance", {})
    header = cwarnc.get("Header", {})
    sources = cwarnc.get("Sources", [])
    if not isinstance(sources, list):
        return entries

    # Find the status record for our specific NPDES number
    target_status = None
    for src in sources:
        for status_rec in src.get("Status", []):
            if status_rec.get("SourceID", "").upper() == npdes.upper():
                target_status = status_rec
                break
        if target_status:
            break

    if not target_status:
        return entries

    # Iterate through the 12 quarters
    for qtr_num in range(1, 13):
        qtr_key = f"Qtr{qtr_num}Status"
        status = target_status.get(qtr_key)
        if not status or status in ("No Viol", "In Compliance"):
            continue

        start_key = f"Qtr{qtr_num}Start"
        end_key = f"Qtr{qtr_num}End"
        period_start = header.get(start_key, "unknown")
        period_end = header.get(end_key, "unknown")
        period = f"{period_start} to {period_end}"

        value = (
            f"NPDES permit compliance violation ({status}) recorded for {npdes}"
            f" in period {period} per ECHO CWA RNC compliance record."
        )
        entries.append(_build_log_entry(
            value=value,
            source_doc=f"EPA ECHO CWARNCCompliance — NPDES {npdes}",
            source_url=echo_url,
            tags=["public_seed", "violation", "compliance", "echo"],
        ))
    return entries


def _parse_watersheds(results_block: dict) -> list[dict]:
    """
    Extract watershed data from Watersheds.WBD12s.
    Returns list of structured watershed dicts.
    """
    watersheds = []
    wbd12s = results_block.get("Watersheds", {}).get("WBD12s", [])
    if not isinstance(wbd12s, list):
        return watersheds

    for w in wbd12s:
        huc12 = w.get("WBD12")
        name = w.get("WBD12Name")
        water_bodies_raw = w.get("ICISWaterBodyNames", "")
        water_bodies = [wb.strip() for wb in water_bodies_raw.split(",") if wb.strip()] if water_bodies_raw else []
        impaired_raw = w.get("PossibleImpairingParameters", "")
        impaired_params = [p.strip() for p in impaired_raw.split("|") if p.strip()] if impaired_raw else []
        esa_flag = w.get("EsaAquaticSpeciesFlg", "No")
        beach_last_year = w.get("BeachCloseLastYearFlg", "No")

        watersheds.append({
            "huc12": huc12,
            "name": name,
            "receivingWaterBodies": water_bodies,
            "impairedParameters": impaired_params,
            "esaAquaticSpeciesFlag": esa_flag == "Yes",
            "beachClosureLastYear": beach_last_year == "Yes",
        })
    return watersheds


def _parse_dmr_loadings(results_block: dict) -> list[dict]:
    """
    Extract DMR pollutant load data from DmrPollLoads.
    Returns list of structured loading records with year headers resolved.
    """
    dmr_block = results_block.get("DmrPollLoads", {})
    header = dmr_block.get("Header", [{}])
    year_labels = header[0] if isinstance(header, list) and header else {}
    loads = dmr_block.get("Loads", [])
    if not isinstance(loads, list):
        return []

    result = []
    for load in loads:
        entry = {
            "loadingsType": load.get("LoadingsType"),
            "byYear": {},
        }
        for yr_num in range(1, 6):
            year_key = f"Year{yr_num}"
            year_label = year_labels.get(year_key, f"Year{yr_num}")
            raw_val = load.get(f"{year_key}Value")
            if raw_val is not None:
                entry["byYear"][year_label] = raw_val
        result.append(entry)
    return result


def _parse_naics_sic(results_block: dict, npdes: str) -> dict:
    """
    Extract NAICS and SIC codes for the target NPDES permit.
    Returns {naics: [{code, desc}], sic: [{code, desc}]}.
    """
    naics_out = []
    sic_out = []

    naics_sources = results_block.get("NAICS", {}).get("Sources", [])
    for src in naics_sources:
        for code_rec in src.get("NAICSCodes", []):
            if code_rec.get("SourceID", "").upper() == npdes.upper():
                naics_out.append({
                    "code": code_rec.get("NAICSCode"),
                    "description": code_rec.get("NAICSDesc"),
                })

    sic_sources = results_block.get("SIC", {}).get("Sources", [])
    for src in sic_sources:
        for code_rec in src.get("SICCodes", []):
            if code_rec.get("SourceID", "").upper() == npdes.upper():
                sic_out.append({
                    "code": code_rec.get("SICCode"),
                    "description": code_rec.get("SICDesc"),
                })

    return {"naics": naics_out, "sic": sic_out}


# ---------------------------------------------------------------------------
# CWAEffluentCompliance MeasurementType → human-readable label
# ---------------------------------------------------------------------------
_MEAS_TYPE_LABELS = {
    "Mthly": "monthly average limit",
    "NMth":  "single sample maximum limit",
    "Wkly":  "weekly average limit",
    "Daily": "daily limit",
}


def _quarter_label(period_start: str) -> str:
    """Convert 'MM/DD/YYYY' period start to 'Q2 2023'."""
    try:
        month = int(period_start.split("/")[0])
        year = period_start.split("/")[2]
        q = (month - 1) // 3 + 1
        return f"Q{q} {year}"
    except (IndexError, ValueError):
        return period_start


def _month_range_label(start_str: str, end_str: str) -> str:
    """Convert '04/01/2023', '06/30/2023' to 'Apr-Jun'."""
    _MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        sm = int(start_str.split("/")[0])
        em = int(end_str.split("/")[0])
        return f"{_MONTHS[sm - 1]}-{_MONTHS[em - 1]}"
    except (IndexError, ValueError):
        return ""


def _parse_cwa_effluent_operating_history(
    results_block: dict, npdes: str, echo_url: str
) -> list[dict]:
    """
    Extract operatingHistory entries from CWAEffluentCompliance.Sources[].Parameters[].

    This is the only ECHO DFR block that carries per-parameter, per-quarter
    exceedance percentages (e.g. "287%").  CWA3YrCompliance only has
    facility-level pass/fail flags with no parameter names.

    Groups by (quarter, parameterName, dischargePoint) so that a quarter with
    both Mthly and NMth violations produces one consolidated entry, not two.
    Also appends a summary entry per parameter describing the overall pattern.
    """
    entries: list[dict] = []
    eff_block = results_block.get("CWAEffluentCompliance", {})
    header = eff_block.get("Header", {})
    sources = eff_block.get("Sources", [])
    source_doc = f"EPA ECHO CWAEffluentCompliance — NPDES {npdes}"

    # violation_groups[(qtr_num, param_name, discharge_pt)] = [(meas_label, pct_str), ...]
    violation_groups: dict[tuple, list] = {}

    for src in sources:
        for param in src.get("Parameters", []):
            if param.get("SourceID", "").upper() != npdes.upper():
                continue
            param_name = param.get("ParameterName", "Unknown parameter")
            discharge_pt = param.get("DischargePoint", "")
            meas_type = param.get("MeasurementType", "")
            meas_label = _MEAS_TYPE_LABELS.get(meas_type, meas_type)

            for qtr_num in range(1, 14):
                raw_val = param.get(f"Qtr{qtr_num}Value")
                if not raw_val:
                    continue
                pct = raw_val.rstrip("%")
                key = (qtr_num, param_name, discharge_pt)
                violation_groups.setdefault(key, []).append((meas_label, pct))

    if not violation_groups:
        return entries

    # One log entry per (quarter, parameter, discharge point)
    param_quarter_labels: dict[str, list[str]] = {}

    for (qtr_num, param_name, discharge_pt), violations in sorted(violation_groups.items()):
        period_start = header.get(f"Qtr{qtr_num}Start", "")
        period_end = header.get(f"Qtr{qtr_num}End", "")
        qtr_lbl = _quarter_label(period_start) if period_start else f"Quarter {qtr_num}"
        mrange = _month_range_label(period_start, period_end) if period_start and period_end else ""
        qtr_display = f"{qtr_lbl} ({mrange})" if mrange else qtr_lbl

        viol_parts = [f"{meas_label} exceeded by {pct}%" for meas_label, pct in violations]
        viol_str = "; ".join(viol_parts).capitalize()
        tags = ["public_seed", "violation", "operating_history"]
        if "coli" in param_name.lower():
            tags.append("ecoli")
        else:
            tags.append("effluent_exceedance")

        value = (
            f"{param_name} exceedance recorded in {qtr_display}. "
            f"{viol_str} at discharge point {discharge_pt}."
        )
        entries.append(_build_log_entry(
            value=value,
            source_doc=source_doc,
            source_url=echo_url,
            tags=tags,
            confidence=0.95,
        ))
        param_quarter_labels.setdefault(param_name, []).append(qtr_lbl)

    # Summary entry per parameter
    for param_name, q_labels in param_quarter_labels.items():
        n = len(q_labels)
        years = sorted({lbl.split()[-1] for lbl in q_labels})
        year_range = f"{years[0]}-{years[-1]}" if len(years) > 1 else years[0]
        qtrs_str = ", ".join(q_labels)
        summary_tags = ["public_seed", "violation", "operating_history", "summary"]
        if "coli" in param_name.lower():
            summary_tags.append("ecoli")
        value = (
            f"Recurring {param_name} violations documented in {n} of 12 quarters "
            f"({year_range}). Violations occurred in {qtrs_str}."
        )
        entries.append(_build_log_entry(
            value=value,
            source_doc=source_doc,
            source_url=echo_url,
            tags=summary_tags,
            confidence=0.95,
        ))

    print(f"[ECHO] Mapped {len(entries) - len(param_quarter_labels)} violation quarter(s) "
          f"+ {len(param_quarter_labels)} summary entry/entries to operatingHistory")
    return entries


def get_procedures_from_limits(permit_limits: list[dict], npdes: str) -> list[dict]:
    """
    Generate proceduresPreferences entries inferred from the permit limits already
    extracted via get_permit_limits().

    Confidence is 0.85 — these are inferred from the permit limit data, not
    directly quoted from a procedures table or compliance schedule.

    Generates:
    - One monthly DMR reporting entry per (parameter, outfall) that has a Monthly limit
    - One WET testing entry if any acute toxicity parameters are present
    - One E. coli specific entry if E. coli is present
    """
    chart_url = (
        f"https://echodata.epa.gov/echo/eff_rest_services.get_effluent_chart"
        f"?p_id={npdes}&output=JSON"
    )
    source_doc = "EPA ECHO Effluent Chart API — inferred procedure from permit limit"
    fetched_at = datetime.now(timezone.utc).isoformat()

    def _proc_entry(value: str, tags: list[str]) -> dict:
        return {
            "value": value,
            "source": "echo_api",
            "evidenceClass": "echo_api",
            "confidence": 0.85,
            "status": "inferred",
            "tags": tags,
            "lastUpdated": fetched_at,
            "_sourceMeta": {
                "sourceDoc": source_doc,
                "sourcePage": None,
                "sourceUrl": chart_url,
                "confidence": 0.85,
            },
        }

    entries: list[dict] = []
    seen_monthly: set[tuple] = set()
    wet_params: list[str] = []
    ecoli_outfall: Optional[str] = None

    for item in permit_limits:
        param = item.get("parameter", "")
        outfall = item.get("outfall", "")
        param_lower = param.lower()

        # WET / toxicity parameters — collect for one combined entry
        if any(kw in param_lower for kw in ("toxicity", "ceriodaphnia", "pimephales", "fathead")):
            if param not in wet_params:
                wet_params.append(param)
            continue

        # E. coli — one specific entry
        if "coli" in param_lower:
            if ecoli_outfall is None:
                ecoli_outfall = outfall
            continue

        # Monthly DMR reporting — one entry per (parameter, outfall) with a Monthly limit
        has_monthly = any(lim.get("limitType") == "Monthly" for lim in item.get("limits", []))
        if has_monthly:
            key = (param, outfall)
            if key not in seen_monthly:
                seen_monthly.add(key)
                entries.append(_proc_entry(
                    value=f"Monthly DMR reporting required for {param} at outfall {outfall}.",
                    tags=["public_seed", "dmr", "reporting", "procedures"],
                ))

    # WET testing entry
    if wet_params:
        species_parts = []
        if any("ceriodaphnia" in p.lower() for p in wet_params):
            species_parts.append("Ceriodaphnia dubia")
        if any("pimephales" in p.lower() or "fathead" in p.lower() for p in wet_params):
            species_parts.append("Fathead Minnow (Pimephales promelas)")
        species_str = " and ".join(species_parts) if species_parts else "multiple species"
        entries.append(_proc_entry(
            value=(
                f"Whole Effluent Toxicity (WET) testing required — acute toxicity pass/fail "
                f"for {species_str} at outfall 001."
            ),
            tags=["public_seed", "wet_testing", "toxicity", "procedures"],
        ))

    # E. coli entry
    if ecoli_outfall is not None:
        entries.append(_proc_entry(
            value=(
                f"E. coli sampling required at outfall {ecoli_outfall}. "
                "Monthly geometric mean and single sample maximum limits apply."
            ),
            tags=["public_seed", "ecoli", "sampling", "procedures"],
        ))

    print(f"[ECHO] Generated {len(entries)} proceduresPreferences entry/entries from permit limits")
    return entries


# ---------------------------------------------------------------------------
# StatisticalBaseDesc → human-readable limitType label
# ---------------------------------------------------------------------------
_STAT_BASE_TO_LIMIT_TYPE = {
    "DAILY MN":  "Daily",
    "DAILY MX":  "Daily",
    "MO AVG":    "Monthly",
    "MO AV MN":  "Monthly",
    "WKLY AVG":  "Weekly",
    "SINGSAMP":  "Single Sample",
}

# StatisticalBaseTypeDesc → statistic label
_STAT_TYPE_TO_STATISTIC = {
    "Minimum": "minimum",
    "Maximum": "maximum",
    "Average": "average",
}


def get_permit_limits(npdes_id: str) -> list[dict]:
    """
    Query the ECHO effluent chart API and extract all permit limits for an NPDES facility.

    Groups by (outfall, parameterCode, monitoringLocation, unit) — each unique
    group becomes one entry in the returned permitLimits array.  Within each
    group, limits are deduplicated: a limit row that repeats across DMR periods
    with the same (value, qualifier, limitType, statistic, effectiveDate, expirationDate)
    is stored only once.

    Parameters with no numeric limit value (monitoring-only rows where
    LimitValueNmbr is None) are included with value: null so they appear in the
    audit trail as monitored-but-no-numeric-limit.

    Endpoint:
        https://echodata.epa.gov/echo/eff_rest_services.get_effluent_chart?p_id={npdes_id}&output=JSON

    Returns:
        List of dicts, each matching the Nymph seed package permitLimits schema.
    """
    api_url = (
        f"{ECHO_DFR_BASE}/eff_rest_services.get_effluent_chart"
        f"?p_id={npdes_id}&output=JSON"
    )
    print(f"[ECHO] Querying effluent chart (permit limits): {api_url}")

    try:
        data = _get(
            f"{ECHO_DFR_BASE}/eff_rest_services.get_effluent_chart",
            {"p_id": npdes_id, "output": "JSON"},
        )
    except requests.Timeout:
        print(f"[ECHO] WARNING: Effluent chart timed out for {npdes_id}")
        return []
    except requests.HTTPError as exc:
        print(f"[ECHO] WARNING: Effluent chart HTTP error for {npdes_id} — {exc}")
        return []
    except Exception as exc:
        print(f"[ECHO] WARNING: Effluent chart query failed for {npdes_id} — {exc}")
        return []

    results = data.get("Results", {})
    perm_features = results.get("PermFeatures", [])
    if not perm_features:
        print(f"[ECHO] No PermFeatures in effluent chart for {npdes_id}")
        return []

    source_meta = {
        "sourceDoc": "EPA ECHO Effluent Chart API",
        "sourcePage": None,
        "sourceUrl": api_url,
        "confidence": 0.95,
    }

    # group_key → {header fields, seen_limit_keys (set), limits list}
    groups: dict[tuple, dict] = {}

    for feature in perm_features:
        outfall = feature.get("PermFeatureNmbr", "")
        for param in feature.get("Parameters", []):
            param_code = param.get("ParameterCode", "")
            param_desc = param.get("ParameterDesc", "")
            mon_loc = param.get("MonitoringLocationDesc", "")

            for dmr in param.get("DischargeMonitoringReports", []):
                unit = dmr.get("LimitUnitDesc", "")
                stat_base = dmr.get("StatisticalBaseDesc", "")
                stat_type = dmr.get("StatisticalBaseTypeDesc", "")
                qualifier = dmr.get("LimitValueQualifierCode")
                raw_value = dmr.get("LimitValueNmbr")
                effective = dmr.get("LimitBeginDate")
                expiry = dmr.get("LimitEndDate")

                # Convert to float (or None for monitoring-only rows)
                value_num = _safe_float(raw_value)

                limit_type = _STAT_BASE_TO_LIMIT_TYPE.get(stat_base, stat_base)
                statistic = _STAT_TYPE_TO_STATISTIC.get(stat_type, stat_type.lower() if stat_type else "")

                # Group by (outfall, paramCode, monitoringLocation, unit)
                group_key = (outfall, param_code, mon_loc, unit)

                if group_key not in groups:
                    groups[group_key] = {
                        "parameter": param_desc,
                        "aliases": _lookup_aliases(param_desc),
                        "parameterCode": param_code,
                        "outfall": outfall,
                        "samplePoint": mon_loc,
                        "unit": unit,
                        "limits": [],
                        "_seen_limit_keys": set(),
                        "_sourceMeta": source_meta,
                    }

                # reportOnly: monitoring-only rows (no numeric limit) are included with value=null
                report_only = value_num is None

                # Deduplicate by the full limit signature
                limit_key = (value_num, qualifier, limit_type, statistic, effective, expiry)
                if limit_key in groups[group_key]["_seen_limit_keys"]:
                    continue
                groups[group_key]["_seen_limit_keys"].add(limit_key)

                limit_entry: dict = {
                    "value": value_num,
                    "qualifier": qualifier,
                    "limitType": limit_type,
                    "statistic": statistic,
                    "effectiveDate": effective,
                    "expirationDate": expiry,
                    "seasonality": "Not Seasonal",
                    "reportOnly": report_only,
                    "samplePoint": mon_loc,
                    "evidenceClass": "echo_api",
                    "_sourceMeta": source_meta,
                }
                groups[group_key]["limits"].append(limit_entry)

    # Build final list: strip individual null-value (monitoring-only) limit rows, then
    # exclude the whole group if no numeric limits remain.  This handles mixed groups
    # where (Report) monitoring rows sit alongside real compliance limits.
    result = []
    excluded_params = []
    for entry in groups.values():
        entry.pop("_seen_limit_keys", None)
        if not entry["limits"]:
            continue
        entry["limits"] = [lim for lim in entry["limits"] if lim["value"] is not None]
        if not entry["limits"]:
            excluded_params.append(entry.get("parameter", "unknown"))
            continue
        entry["limits"].sort(key=lambda l: (l.get("limitType") or "", l.get("statistic") or ""))
        result.append(entry)

    if excluded_params:
        print(f"[ECHO] Excluded {len(excluded_params)} monitoring-only/null-value parameter(s): {excluded_params}")

    # Sort entries: outfall first, then parameter name
    result.sort(key=lambda e: (e["outfall"], e["parameter"], e.get("samplePoint", ""), e["unit"]))

    total_limits = sum(len(e["limits"]) for e in result)
    print(f"[ECHO] Permit limits: {len(result)} parameter-group(s), {total_limits} unique limit(s) across {len(perm_features)} outfall(s)")
    return result, excluded_params


def _parse_formal_enforcement_actions(
    results_block: dict, npdes: str, echo_url: str
) -> list[dict]:
    """
    Extract formal enforcement actions from FormalActions.Action[] and
    CaseFormalActions.Action[] in the DFR response.

    FormalActions entries are assigned confidence 0.97.
    CaseFormalActions entries (federal case records) are assigned confidence 0.98.

    All fields are extracted dynamically — only non-null values are included in
    the human-readable value string.
    """
    entries: list[dict] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    def _build_enforcement_value(action: dict) -> str:
        parts: list[str] = []

        # Action classification
        action_type = action.get("ActionType") or action.get("ActionTypeDesc")
        case_type = action.get("CaseType") or action.get("CaseTypeDesc")
        if action_type and case_type:
            parts.append(f"{action_type} ({case_type})")
        elif action_type:
            parts.append(action_type)
        elif case_type:
            parts.append(case_type)

        # Case identification
        case_name = action.get("CaseName") or action.get("CaseDesc")
        case_id = action.get("CaseID") or action.get("CaseCRNumber")
        if case_name:
            parts.append(f"Case: {case_name}")
        if case_id:
            parts.append(f"Case ID: {case_id}")

        # Dates
        issue_date = action.get("IssueDate") or action.get("IssuedDate")
        action_date = action.get("ActionDate") or action.get("ActionTakenDate")
        lead_agency = action.get("LeadAgency") or action.get("AgencyTypeDesc")
        if issue_date:
            parts.append(f"Issued: {issue_date}")
        if action_date and action_date != issue_date:
            parts.append(f"Action date: {action_date}")
        if lead_agency:
            parts.append(f"Lead agency: {lead_agency}")

        # Penalties — only include if present and non-zero
        fed_penalty = _safe_float(action.get("FedPenalty") or action.get("FederalPenalty") or action.get("FedPenaltyAmt"))
        state_penalty = _safe_float(action.get("StateLocalPenalty") or action.get("StatePenaltyAmt"))
        total_penalty = _safe_float(action.get("TotalPenaltyAmt") or action.get("TotalPenalty") or action.get("PenaltyAmount"))
        if fed_penalty and fed_penalty > 0:
            parts.append(f"Federal penalty: ${fed_penalty:,.0f}")
        if state_penalty and state_penalty > 0:
            parts.append(f"State/local penalty: ${state_penalty:,.0f}")
        if total_penalty and total_penalty > 0 and not (fed_penalty or state_penalty):
            parts.append(f"Total penalty: ${total_penalty:,.0f}")

        # Settlement
        settlement_date = action.get("SettlementDate") or action.get("SettledDate")
        if settlement_date:
            parts.append(f"Settlement date: {settlement_date}")

        return ". ".join(parts) + "." if parts else "Formal enforcement action recorded."

    def _make_action_entry(action: dict, confidence: float, source_doc: str) -> dict:
        return {
            "value": _build_enforcement_value(action),
            "source": "echo_api",
            "evidenceClass": "echo_api",
            "confidence": confidence,
            "status": "confirmed",
            "tags": ["public_seed", "enforcement", "penalty", "historical_outcomes"],
            "lastUpdated": fetched_at,
            "_sourceMeta": {
                "sourceDoc": source_doc,
                "sourcePage": None,
                "sourceUrl": echo_url,
                "confidence": confidence,
            },
        }

    # FormalActions.Action[]
    formal_block = results_block.get("FormalActions", {})
    if isinstance(formal_block, dict):
        actions = formal_block.get("Action", []) or []
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and any(v for v in action.values() if v):
                    entries.append(_make_action_entry(
                        action, 0.97,
                        f"EPA ECHO FormalActions — NPDES {npdes}"
                    ))

    # CaseFormalActions.Action[]
    case_block = results_block.get("CaseFormalActions", {})
    if isinstance(case_block, dict):
        actions = case_block.get("Action", []) or []
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and any(v for v in action.values() if v):
                    entries.append(_make_action_entry(
                        action, 0.98,
                        f"EPA ECHO CaseFormalActions — NPDES {npdes}"
                    ))

    if entries:
        print(f"[ECHO] Mapped {len(entries)} formal enforcement action(s) to historicalOutcomes")
    return entries


def _parse_admin_continued_permits(
    permits: list[dict], npdes: str, echo_url: str
) -> list[dict]:
    """
    Check the Permits array for any permit with FacilityStatus == 'Admin Continued'.

    Administrative continuation means the permit has expired but remains in effect
    while the renewal application is under review.  Each such permit produces one
    operatingHistory entry.
    """
    entries: list[dict] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    for permit in permits:
        if (permit.get("FacilityStatus") or "").strip() != "Admin Continued":
            continue
        source_id = permit.get("SourceID") or permit.get("NPDESPermitNumber") or npdes
        exp_date = permit.get("ExpDate") or permit.get("ExpirationDate")
        exp_str = f" (original expiration: {exp_date})" if exp_date else ""
        value = (
            f"Permit {source_id} is operating under administrative continuation{exp_str}. "
            "The permit has expired but remains in full legal effect while the renewal "
            "application is pending agency review."
        )
        entries.append({
            "value": value,
            "source": "echo_api",
            "evidenceClass": "echo_api",
            "confidence": 0.98,
            "status": "confirmed",
            "tags": ["public_seed", "permit", "admin_continued", "operating_history"],
            "lastUpdated": fetched_at,
            "_sourceMeta": {
                "sourceDoc": f"EPA ECHO Permits array — NPDES {npdes}",
                "sourcePage": None,
                "sourceUrl": echo_url,
                "confidence": 0.98,
            },
        })

    if entries:
        print(f"[ECHO] Mapped {len(entries)} admin-continued permit(s) to operatingHistory")
    return entries


def lookup_facility_by_npdes(npdes: str, plant_name: str = "") -> dict:
    """
    Query ECHO DFR for a facility by NPDES permit number.
    Returns a normalized dict with identity, nymphLogs, watershed, dmrLoadings, documents.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    dfr_url = f"{ECHO_DFR_BASE}/dfr_rest_services.get_dfr"
    echo_url_str = f"{dfr_url}?p_id={npdes}&output=JSON"

    result = {
        "npdesPermitNumber": npdes,
        "fetchedAt": fetched_at,
        "raw": {},
        "identity": {
            "plantName": None,
            "street": None,
            "city": None,
            "state": None,
            "zip": None,
            "county": None,
            "coordinates": {"lat": None, "lon": None},
            "plantType": "wastewater",
            "facilityStatus": None,
            "areas": None,
            "universe": None,
            "owner": None,
            "naics": [],
            "sic": [],
        },
        "permit": {
            "status": None,
            "expirationDate": None,
            "registryId": None,
            "majorMinor": None,
        },
        "compliance": {
            "inspections": [],
            "violations": [],
            "formalActions": [],
        },
        "nymphLogs": {
            "historicalOutcomes": [],
            "operatingHistory": [],
        },
        "watershed": [],
        "dmrLoadings": [],
        "documentLinks": [],
        "sourcesSearched": [],
        "warnings": [],
    }

    # -----------------------------------------------------------------------
    # ECHO DFR — Detailed Facility Report
    # -----------------------------------------------------------------------
    print(f"[ECHO] Querying DFR: {echo_url_str}")
    try:
        data = _get(dfr_url, {"p_id": npdes, "output": "JSON"})
        result["raw"]["dfr"] = data
        res = data.get("Results", {})

        result["permit"]["registryId"] = res.get("RegistryID")
        result["sourcesSearched"].append({
            "url": echo_url_str,
            "type": "echo_api",
            "fetchedAt": fetched_at,
            "resultSummary": f"HTTP 200 — message: {res.get('Message', 'unknown')}",
        })

        permits = res.get("Permits", [])
        if not permits:
            result["warnings"].append(f"No Permits entries found in DFR for {npdes}")
            print(f"[ECHO] WARNING: No facility records found for {npdes}")

        # -- Identity --
        # Primary source: ICIS-NPDES record matching the exact NPDES number.
        # It has the most precise coordinates, NAICS, SIC, ExpDate, FacilityStatus.
        icis_permit = _pick_icis_permit(permits, npdes)

        # FRS record has GPS-confirmed street address and entrance-point coordinates.
        frs_record = _pick_frs_record(permits)

        # Associated permit record is the owner/permittee legal entity.
        owner_record = _pick_owner_record(permits, npdes)

        if icis_permit:
            result["identity"]["plantName"] = icis_permit.get("FacilityName")
            result["identity"]["city"] = icis_permit.get("FacilityCity")
            result["identity"]["state"] = icis_permit.get("FacilityState")
            result["identity"]["zip"] = icis_permit.get("FacilityZip")
            result["identity"]["county"] = icis_permit.get("FacilityCountyName")
            result["identity"]["facilityStatus"] = icis_permit.get("FacilityStatus")
            result["identity"]["areas"] = icis_permit.get("Areas")
            result["identity"]["universe"] = icis_permit.get("Universe")
            result["permit"]["expirationDate"] = icis_permit.get("ExpDate")

            # Prefer the ICIS-NPDES coordinates (more precise for lat/lon decimal places)
            lat = _safe_float(icis_permit.get("Latitude"))
            lon = _safe_float(icis_permit.get("Longitude"))
            if lat is not None and lon is not None:
                result["identity"]["coordinates"] = {"lat": lat, "lon": lon}

            # Street address from FRS record (GPS-confirmed entrance point) if available
            if frs_record:
                result["identity"]["street"] = frs_record.get("FacilityStreet")
                # If ICIS coordinates are missing, fall back to FRS
                if result["identity"]["coordinates"]["lat"] is None:
                    lat_frs = _safe_float(frs_record.get("Latitude"))
                    lon_frs = _safe_float(frs_record.get("Longitude"))
                    if lat_frs is not None:
                        result["identity"]["coordinates"] = {"lat": lat_frs, "lon": lon_frs}
            else:
                result["identity"]["street"] = icis_permit.get("FacilityStreet")

            # Owner/permittee — the associated permit record's FacilityName
            if owner_record:
                result["identity"]["owner"] = owner_record.get("FacilityName")

            print(
                f"[ECHO] Facility: {result['identity']['plantName']} — "
                f"{result['identity']['city']}, {result['identity']['state']}"
            )
            if result["identity"]["owner"]:
                print(f"[ECHO] Owner/Permittee: {result['identity']['owner']}")

        # -- NAICS / SIC --
        codes = _parse_naics_sic(res, npdes)
        result["identity"]["naics"] = codes["naics"]
        result["identity"]["sic"] = codes["sic"]
        if codes["naics"]:
            print(f"[ECHO] NAICS: {codes['naics'][0]['code']} — {codes['naics'][0]['description']}")
        if codes["sic"]:
            print(f"[ECHO] SIC:   {codes['sic'][0]['code']} — {codes['sic'][0]['description']}")

        # -- InspectionEnforcementSummary (count-level summary, kept for compliance block) --
        insp_summary = res.get("InspectionEnforcementSummary", {})
        if isinstance(insp_summary, dict):
            for src_block in insp_summary.get("Source", []) if isinstance(insp_summary.get("Source"), list) else []:
                for key, value in src_block.items():
                    if "insp" in key.lower() and value and value != "0":
                        result["compliance"]["inspections"].append({"type": key, "count": value})
            # Flat dict fallback (old response shape)
            for key, value in insp_summary.items():
                if key in ("ProgramDates", "Source"):
                    continue
                if "insp" in key.lower() and value and value != "0":
                    result["compliance"]["inspections"].append({"type": key, "count": value})

        # -- ComplianceHistory.Inspection → nymphLogs.historicalOutcomes --
        insp_entries = _parse_inspections(res, npdes, echo_url_str)
        result["nymphLogs"]["historicalOutcomes"].extend(insp_entries)
        if insp_entries:
            print(f"[ECHO] Mapped {len(insp_entries)} inspection(s) to historicalOutcomes")

        # -- CWARNCCompliance violations → nymphLogs.historicalOutcomes --
        violation_entries = _parse_cwarnc_violations(res, npdes, echo_url_str)
        result["nymphLogs"]["historicalOutcomes"].extend(violation_entries)
        if violation_entries:
            print(f"[ECHO] Mapped {len(violation_entries)} violation quarter(s) to historicalOutcomes")

        # -- CWAEffluentCompliance exceedances → nymphLogs.operatingHistory --
        op_history_entries = _parse_cwa_effluent_operating_history(res, npdes, echo_url_str)
        result["nymphLogs"]["operatingHistory"].extend(op_history_entries)

        # -- Formal enforcement actions → nymphLogs.historicalOutcomes --
        enforcement_entries = _parse_formal_enforcement_actions(res, npdes, echo_url_str)
        result["nymphLogs"]["historicalOutcomes"].extend(enforcement_entries)

        # -- Admin-continued permits → nymphLogs.operatingHistory --
        admin_entries = _parse_admin_continued_permits(permits, npdes, echo_url_str)
        result["nymphLogs"]["operatingHistory"].extend(admin_entries)

        # -- Watersheds --
        result["watershed"] = _parse_watersheds(res)
        if result["watershed"]:
            wnames = [w["name"] for w in result["watershed"] if w.get("name")]
            print(f"[ECHO] Watersheds: {', '.join(wnames)}")
            esa_any = any(w.get("esaAquaticSpeciesFlag") for w in result["watershed"])
            if esa_any:
                print(f"[ECHO] ESA aquatic species flag: YES — sensitive receiving waters")

        # -- DMR Pollutant Loadings --
        result["dmrLoadings"] = _parse_dmr_loadings(res)
        if result["dmrLoadings"]:
            print(f"[ECHO] DMR loadings: {len(result['dmrLoadings'])} loading type(s)")

        # -- Document links --
        for doc_key in ["WebFireDocuments", "CAEDDocuments", "AWSDocs"]:
            docs = res.get(doc_key, {})
            doc_list = []
            if isinstance(docs, list):
                doc_list = docs
            elif isinstance(docs, dict):
                doc_list = docs.get(doc_key, []) or docs.get("Documents", []) or []
            for doc in (doc_list or [])[:10]:
                url = doc.get("LinkURL") or doc.get("DocumentURL") or doc.get("URL")
                if url:
                    result["documentLinks"].append({
                        "url": url,
                        "title": doc.get("DocumentName") or doc.get("Title") or doc.get("FileName"),
                        "type": doc_key,
                        "date": doc.get("DocumentDate") or doc.get("Date"),
                    })

        if result["documentLinks"]:
            print(f"[ECHO] Found {len(result['documentLinks'])} document link(s)")
        else:
            print("[ECHO] No document links found in ECHO DFR")

    except requests.Timeout:
        result["warnings"].append(f"ECHO DFR timed out after {REQUEST_TIMEOUT}s")
        print("[ECHO] ERROR: DFR query timed out")
    except requests.HTTPError as exc:
        result["warnings"].append(f"ECHO DFR HTTP error: {exc}")
        print(f"[ECHO] ERROR: {exc}")
    except Exception as exc:
        result["warnings"].append(f"ECHO DFR query failed: {exc}")
        print(f"[ECHO] ERROR: {exc}")

    # -----------------------------------------------------------------------
    # ECHO CWA Effluent Compliance — permit status
    # -----------------------------------------------------------------------
    time.sleep(0.3)
    eff_url = f"{ECHO_DFR_BASE}/dfr_rest_services.get_cwa_eff_compliance"
    eff_url_str = f"{eff_url}?p_id={npdes}&output=JSON"
    print(f"[ECHO] Querying effluent compliance: {eff_url_str}")
    try:
        edata = _get(eff_url, {"p_id": npdes, "output": "JSON"})
        result["raw"]["eff_compliance"] = edata
        eff_results = edata.get("Results", {})
        result["sourcesSearched"].append({
            "url": eff_url_str,
            "type": "echo_api",
            "fetchedAt": fetched_at,
            "resultSummary": "Effluent compliance record",
        })
        permit_info = eff_results.get("CWAEffluentCompliance", {})
        if isinstance(permit_info, dict):
            result["permit"]["status"] = (
                permit_info.get("PermitStatusDesc") or permit_info.get("PermStatus")
            )
    except Exception as exc:
        result["warnings"].append(f"Effluent compliance query failed: {exc}")
        print(f"[ECHO] WARNING: Effluent compliance query failed — {exc}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Query EPA ECHO for facility data by NPDES number")
    parser.add_argument("--npdes", required=True, help="NPDES permit number (e.g. AL0056626)")
    parser.add_argument("--plant", default="", help="Plant name hint for record selection")
    parser.add_argument("--output", help="Optional JSON output file path")
    args = parser.parse_args()

    result = lookup_facility_by_npdes(args.npdes.strip().upper(), plant_name=args.plant)

    print("\n--- ECHO Result Summary ---")
    ident = result["identity"]
    print(f"  Plant name:    {ident['plantName']}")
    print(f"  Street:        {ident['street']}")
    print(f"  Location:      {ident['city']}, {ident['state']} {ident['zip']}")
    print(f"  County:        {ident['county']}")
    print(f"  Coordinates:   {ident['coordinates']}")
    print(f"  Status:        {ident['facilityStatus']}")
    print(f"  Areas:         {ident['areas']}")
    print(f"  Universe:      {ident['universe']}")
    print(f"  Owner:         {ident['owner']}")
    print(f"  NAICS:         {ident['naics']}")
    print(f"  SIC:           {ident['sic']}")
    print(f"  Registry ID:   {result['permit']['registryId']}")
    print(f"  Permit expires:{result['permit']['expirationDate']}")
    print(f"  Doc links:     {len(result['documentLinks'])}")
    print(f"  Inspections:   {len(result['nymphLogs']['historicalOutcomes'])} log entries")
    print(f"  Watersheds:    {len(result['watershed'])}")
    print(f"  DMR loadings:  {len(result['dmrLoadings'])}")
    if result["warnings"]:
        print(f"  Warnings:      {result['warnings']}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n[ECHO] Full result saved to {args.output}")
    else:
        display = {k: v for k, v in result.items() if k != "raw"}
        print("\n--- Structured Result ---")
        print(json.dumps(display, indent=2, default=str))

    return result


if __name__ == "__main__":
    main()
