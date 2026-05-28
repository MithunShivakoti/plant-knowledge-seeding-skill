# Plant Knowledge Seeding Skill

## When to use this skill

Use this skill when onboarding a new wastewater treatment plant into Nymph. It works for any NPDES-permitted facility in all 50 US states. It takes a plant's public identifiers and produces a source-backed JSON seed package that populates the entire Nymph plant profile — Overview basics, Process Train, Permit Limits, Nymph Logs, and the document library.

The skill determines the correct state agency and permit portal automatically from the NPDES permit number prefix (e.g. AL → ADEM, TX → TCEQ, FL → FDEP). All 50 state prefixes are mapped in `scripts/seed_runner.py` (`NPDES_STATE_MAP`) and documented in `references/state_portal_patterns.md`.

Do not use this skill to fill private operating data, MOR readings, or internal targets. Those surfaces belong to plant operators, not public records.

---

## Input contract

```
Required:
  plant_name       Full facility name (e.g. "Gilliam Creek WWTP")
  location         City and state (e.g. "Arab, Alabama")
  npdes_number     NPDES permit number (e.g. "AL0056626")

Optional:
  owner            Permittee / utility name (e.g. "City of Arab Sewer Board")
  plant_id         Nymph internal plant ID, used in node IDs
  production       Boolean — set true to mark package as applied (default: pending_review)
```

Run via command line:
```bash
python scripts/seed_runner.py --plant "Gilliam Creek WWTP" --location "Arab, Alabama" --npdes AL0056626
python scripts/seed_runner.py --plant "Eufaula WWTP" --location "Eufaula, Alabama" --npdes AL0061671 --owner "City of Eufaula" --production
```

---

## Source priority

Work sources in this order. Never invent data — if a source does not provide a fact, leave the field empty.

### Tier 1 — Official permit sources (legal facts)

1. **EPA ECHO / ICIS records** — Queried first for every plant regardless of state. Returns facility identity, permit dates, compliance history, inspection counts, enforcement actions, and direct document links. Working endpoint: `https://echodata.epa.gov/echo/dfr_rest_services.get_dfr?p_id={NPDES}&output=JSON`
2. **State NPDES permit portals** — Final permit PDFs (effluent limit tables), fact sheets, draft permits, permit applications. The correct portal is determined automatically from the NPDES prefix — see `references/state_portal_patterns.md` for all 50 states.
   - Working endpoint: `https://echodata.epa.gov/echo/dfr_rest_services.get_dfr?p_id={NPDES}&output=JSON`
3. **Final permits and fact sheets** — Authoritative source for all effluent limits. Stop here for legal limit values.
4. **Draft permits and permit applications** — Best source for physical process detail: unit descriptions, design flows, process diagrams, Form 2A unit process summary tables.
5. **Municipal or utility websites** — Capital improvement plans, annual reports, rate studies, board meeting minutes — supplementary process info.

### Tier 2 — Diagrams (physical process detail)

Look for and extract:
- Process flow diagrams (PFDs) — ordered treatment units with named flows
- Site layout drawings — sampler locations, outfall locations
- Sampler-location drawings — confirms where sampling points are for each outfall
- Inactive or legacy units — record as `status: inactive`, not omitted
- Solids routing details — what happens to WAS, primary sludge, biosolids

### Tier 3 — Secondary useful sources

- Public notices (permit renewal notices, public comment periods)
- Inspection letters and field inspection reports
- Enforcement records and consent orders
- SSO reports (spill volumes, locations, root causes)
- Sludge management references (land application sites, disposal methods)

---

## Extraction workflow

### Step 1 — Resolve facility identity via ECHO

```bash
python scripts/echo_lookup.py --npdes AL0056626
```

ECHO returns: facility name, address, coordinates, permit dates, SIC codes, compliance summary. Validate against the provided plant name and location. Flag mismatches in `runAudit.conflicts`.

### Step 2 — Locate and download permit documents

Use the ECHO response to find permit document URLs. Fall back to the state portal patterns in `references/state_portal_patterns.md`.

Priority document order:
1. Final permit (effluent limits table, schedule of compliance) — use `pdf_extractor.py`
2. Fact sheet / statement of basis (design flow, background, rationale)
3. Draft permit application (process diagrams, unit descriptions, flow data)
4. Inspection reports (historical outcomes, corrective actions)

For permit tables, also run `table_extractor.py` to capture structured limit rows.

### Step 3 — Extract text from PDFs

```bash
python scripts/pdf_extractor.py --url https://example.com/permit.pdf --output extracted.json
python scripts/table_extractor.py --input extracted.json --output tables.json
```

`pdf_extractor.py` preserves page numbers for provenance. Falls back to OCR for scanned documents. `table_extractor.py` extracts structured table rows from effluent limit tables.

### Step 4 — Parse structured data from extracted text

```bash
python scripts/permit_parser.py --input extracted.json --npdes AL0056626 --plant-slug gilliam --output parsed_facts.json
```

The parser calls gpt-4o to extract:
- Overview basics: flow rates, process type, influent type
- Process train: ordered liquid and solids nodes (including inactive units)
- Permit limits: array of parameter objects with unit, statistic, seasonality, frequency, sample type, outfall
- Chemicals mentioned with feed points (for chemicalsDosing)
- Permit-required procedures (for proceduresPreferences)
- Documented historical outcomes from inspections/enforcement

Every extracted fact gets: `sourceDoc`, `sourcePage`, `confidence`, `extractedAt`.

### Step 5 — Assemble the seed package

`seed_runner.py` assembles all parsed facts into the canonical output shape defined in `references/nymph_schema.json`. It:
- Normalises keys to `liquid`/`solids` (not `liquidTrain`/`solidsTrain`)
- Normalises `permitLimits` to an array with `parameter` field per item
- Deduplicates conflicting values and logs them in `runAudit.conflicts`
- Skips unsupported surfaces and logs them in `runAudit.factsSkipped`
- Fills `runAudit.sourcesSearched` with every URL and document fetched
- Adds `identityPatch` as the explicit plants-table write object
- Adds `auditRun` for the agent_runs table row
- Adds `nonGoals` to explicitly confirm what was left empty

### Step 6 — Lint provenance

```bash
python scripts/provenance_linter.py --input seed_package.json
```

Checks that every populated fact has `_sourceMeta` with `sourceDoc`, `sourcePage`, and `confidence`. Reports any facts missing provenance before the package is submitted.

### Step 7 — Validate

```bash
python scripts/seed_validator.py --input seed_package.json
```

Must pass with zero errors before any write. Validator enforces all 11 guardrails (see section below).

### Step 8 — Review gate

Print the human review note. Do NOT apply to production automatically. A human reviewer must check:
- Process train order matches the permit flow diagram
- Permit limits match the final permit table exactly
- No operating targets were mistakenly populated from limits
- Nymph Logs contain only plant-specific facts, not generic wastewater text
- `identityPatch` contains the correct plants-table values

The seed package JSON is the deliverable. Apply it by importing into Nymph's DB using your own write layer — the skill does not write to the DB directly.

### Step 9 — Readback (if applied)

After applying to the DB, run:
```bash
python scripts/db_readback.py --npdes AL0056626 --expected seed_package.json
```

Verifies the applied seed against the expected package. Required by guardrail 11.

---

## Review gates (do not skip)

| Gate | Check |
|------|-------|
| G1 — Identity | NPDES number, plant name, and city/state all present before seeding |
| G2 — Sources | Final permit used for legal limits; application used for physical process detail |
| G3 — Provenance | Every extracted fact has sourceDoc, sourcePage, and confidence |
| G4 — Process units | Nodes have id, type, label, train, order, stage, status — not a paragraph |
| G5 — Inactive units | Inactive units recorded as status=inactive — not omitted |
| G6 — Permit fidelity | Unit, statistic, season, frequency, and sampleType preserved exactly |
| G7 — No limit→target conversion | Permit limits not converted into operatingTargets |
| G8 — No MOR readings | MOR daily readings left empty unless public plant-specific DMR data available |
| G9 — Plant-specific logs | Every Nymph Log entry cites a specific document and page |
| G10 — Audit trail | All searches, skipped facts, conflicts, and assumptions in runAudit |
| G11 — Readback | Applied seeds verified through readback from the persistence layer |

---

## Output checklist

Before delivering the seed package, confirm:

- [ ] `identity.npdesPermitNumber` matches the input
- [ ] `identity.location` is present (city and state string)
- [ ] `identityPatch` has plantName, city, state, plantType — separate from profile JSON
- [ ] `overview.basics.designFlowMGD` is a float, not null, if the permit names a design flow
- [ ] `overview.processTrain.liquid` has at least one node if any process diagram was found
- [ ] `overview.processTrain` uses `liquid` and `solids` keys (not `liquidTrain`/`solidsTrain`)
- [ ] `permitLimits` is an array; each item has `parameter`, `unit`, and at least one limit
- [ ] Every permit limit entry has `value`, `limitType`, `statistic`, `unit`, `frequency`, `sampleType`
- [ ] `nymphLogs.chemicalsDosing` is populated only if chemicals are named in public docs
- [ ] All Nymph Log entries have `confidence` between 0 and 1
- [ ] `documents` lists every source PDF with official agency `sourceUrl`
- [ ] `auditRun.skippedUnsupportedSurfaces` includes "targets" and "MOR readings"
- [ ] `nonGoals.operatingTargets` and `nonGoals.morReadings` are explicitly set
- [ ] `runAudit.seedSummary` explicitly states that operating targets and MOR readings were left empty
- [ ] `seed_validator.py` passes with zero errors
- [ ] `provenance_linter.py` passes with zero errors

---

## Non-goals

The skill must never populate:

- `operatingTargets` — internal targets set by operators, not from permits
- MOR daily readings — not public unless a state provides open discharge monitoring data
- Private troubleshooting history — must come from actual public enforcement or inspection records
- Generic wastewater text — every Nymph Log entry must be plant-specific
