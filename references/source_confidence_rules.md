# Source Confidence Rules

Rules for assigning confidence scores (0.0–1.0) to every extracted fact. Apply these before the gpt-4o call and in post-extraction review. All scores are stored in `_sourceMeta.confidence`.

---

## Score bands

| Score | Label | When to use |
|-------|-------|-------------|
| 0.95–1.00 | Certain | Value is explicitly stated in a final permit table or official fact sheet. No ambiguity. |
| 0.80–0.94 | High | Value is explicitly stated in a draft permit, application, or inspection letter. Source is official but not yet final. |
| 0.60–0.79 | Medium | Value is stated in a secondary source (municipal website, annual report, engineering report). Cross-check against permit if possible. |
| 0.40–0.59 | Low | Value is inferred from context — e.g. process type implied by unit names when no explicit statement exists. Flag for human review. |
| 0.00–0.39 | Do not use | Guessed or derived from generic wastewater knowledge with no plant-specific evidence. Leave the field empty instead. |

---

## Rules by field

### identity (plantName, npdesPermitNumber, location)

- ECHO DFR exact match → 0.95
- ECHO DFR with name variation (e.g. abbreviation) → 0.80
- State portal match only → 0.85

### overview.basics

| Field | Source | Score |
|-------|--------|-------|
| designFlowMGD | Final permit or fact sheet | 0.95 |
| designFlowMGD | Permit application Form 2A | 0.85 |
| designFlowMGD | Municipal annual report | 0.65 |
| averageFlowMGD | Fact sheet or DMR summary | 0.85 |
| peakFlowMGD | Permit application | 0.80 |
| processType | Explicit permit language ("activated sludge") | 0.95 |
| processType | Inferred from unit list (aeration basin + clarifier) | 0.55 |
| influentType | Permit states "domestic wastewater" | 0.95 |
| influentType | Inferred from SIC code | 0.60 |

### processTrain nodes

| Source | Score |
|--------|-------|
| Process flow diagram in permit application | 0.90 |
| Unit process summary table (Form 2A) | 0.85 |
| Fact sheet unit description paragraph | 0.75 |
| Inferred from process type only | 0.40 — flag for review |

**Always record inactive units from diagrams.** Assign `status: inactive` and confidence 0.80 if a diagram shows the unit as decommissioned or bypassed.

### permitLimits

| Source | Score |
|--------|-------|
| Final permit effluent limits table | 0.97 |
| Draft permit limits table | 0.85 |
| Fact sheet limit summary | 0.80 |
| Compliance schedule limit | 0.90 |

Never assign < 0.80 for a permit limit that was read directly from a table — if you cannot confirm the value, leave the limit entry out rather than lowering the score.

### nymphLogs

| Category | Source | Score |
|----------|--------|-------|
| chemicalsDosing | Chemical named in permit with feed point | 0.85 |
| chemicalsDosing | Chemical mentioned in permit application | 0.75 |
| chemicalsDosing | Dose rate stated in permit | 0.90 |
| operatingHistory | Seasonal pattern stated in permit | 0.85 |
| operatingHistory | Historical capacity from fact sheet | 0.80 |
| proceduresPreferences | DMR submission schedule from permit | 0.95 |
| proceduresPreferences | SSO notification requirement | 0.95 |
| proceduresPreferences | WET testing schedule | 0.90 |
| historicalOutcomes | Inspection finding from inspection letter | 0.85 |
| historicalOutcomes | Enforcement action from consent order | 0.90 |
| historicalOutcomes | ECHO inspection count summary | 0.80 |

Do not populate chemicalsDosing with guessed dose rates. If the permit says "chlorination" but does not state a dose, record the chemical and feed point only — leave dose rate fields empty.

---

## When to leave a field empty

Leave a field empty (null or []) and record a factsSkipped entry when:

1. No public document in the source set contains evidence for the field.
2. The only available evidence is generic wastewater knowledge (e.g. "activated sludge typically uses...").
3. The confidence would be < 0.40.
4. The field is an operating target or MOR reading — these are always left empty by policy.

---

## Conflict resolution

When two sources give different values for the same field:

1. Trust the final permit over the draft permit.
2. Trust the final permit over the application.
3. Trust the fact sheet over the municipal website.
4. If both sources are final permit pages and they disagree, record both values in `runAudit.conflicts` and use the value from the effluent limits table.

Assign the lower of the two confidence scores to the resolved value, and note the conflict in `_sourceMeta`.
