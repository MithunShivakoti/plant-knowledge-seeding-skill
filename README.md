# Nymph Plant Knowledge Seeding Skill

An automated pipeline that seeds structured plant knowledge into [Nymph](https://nyad.ai) from public regulatory sources, given an NPDES permit number.

## What It Does

Given a plant name, location, and NPDES permit number, the skill automatically:

- Fetches compliance history, violations, inspections, and enforcement actions from the **EPA ECHO API**
- Downloads the final permit PDF from the state regulatory portal via **browser automation (Playwright)**
- OCRs scanned permit PDFs using **Google Document AI**
- Extracts structured data (process train, permit limits, chemicals, procedures) using **GPT-4o**
- Assembles a complete seed package with **full provenance on every fact**
- Validates everything against **11 guardrails** before output
- Serves results via **FastAPI** with response caching

---

## Quick Start

```bash
git clone https://github.com/mshivako/plant-knowledge-seeding-skill
cd plant-knowledge-seeding-skill
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # fill in your API keys
uvicorn api:app --reload --port 8000
```

The `cache/` directory already contains 6 seeded Alabama plants — the API works immediately on clone with no pipeline runs needed.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| GET | `/plants` | List all cached plants |
| GET | `/seed/{npdes}` | Get cached seed package |
| POST | `/seed` | Run pipeline or return from cache |
| DELETE | `/seed/{npdes}` | Clear cache to force re-run |

### POST /seed request body

```json
{
  "plant": "Eufaula WWTP",
  "location": "Eufaula, Alabama",
  "npdes": "AL0061671"
}
```

---

## CLI Usage

```bash
# Run full pipeline
python scripts/seed_runner.py \
  --plant "Eufaula WWTP" \
  --location "Eufaula, Alabama" \
  --npdes AL0061671

# Readback report on a saved package
python scripts/db_readback.py AL0061671_seed_package.json

# Apply to Firestore (dry run first)
python scripts/db_apply.py AL0061671_seed_package.json --dry-run
python scripts/db_apply.py AL0061671_seed_package.json --force
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

```
OPENAI_API_KEY=
GOOGLE_APPLICATION_CREDENTIALS=gcp_credentials.json
GOOGLE_CLOUD_PROJECT_ID=
DOCUMENT_AI_PROCESSOR_ID=
DOCUMENT_AI_LOCATION=
```

---

## Output Structure

Every seed package contains:

| Field | Description |
|-------|-------------|
| `identity` | Plant name, location, coordinates, owner, NPDES number |
| `identityPatch` | Fields to write to the `plants` table |
| `overview.basics` | `processType`, `influentType`, `designFlowMGD` |
| `overview.processTrain` | Ordered liquid and solids treatment nodes |
| `permitLimits` | All numeric permit limits with frequency and sample type |
| `nymphLogs` | `chemicalsDosing`, `operatingHistory`, `proceduresPreferences`, `historicalOutcomes` |
| `documents` | Permit PDFs with source URLs |
| `runAudit` | Full provenance trail, `factsSkipped`, `conflicts` |

---

## Evidence Classes

Every extracted fact is labeled with how it was obtained:

| Class | Meaning |
|-------|---------|
| `direct_text_extraction` | Explicitly stated in the permit PDF |
| `echo_api` | From EPA ECHO federal database |
| `heuristic_inference` | Inferred from engineering patterns — flag for review |
| `adem_defaults` | From Alabama state monitoring schedule defaults |

---

## Tested Plants

6 Alabama plants are pre-seeded in `cache/`:

| NPDES | Plant | Design Flow | Permit Params |
|-------|-------|-------------|---------------|
| AL0061671 | Eufaula WWTP | 2.7 MGD | 15 |
| AL0056626 | Gilliam Creek WWTP | 0.83 MGD | 14 |
| AL0071897 | Madison WWTP | 8.25 MGD | 15 |
| AL0020842 | Fairhope WWTP | 4.0 MGD | 20 |
| AL0025828 | Alabaster WWTP | 7.6 MGD | 21 |
| AL0055786 | Saraland WWTP | 2.6 MGD | 18 |

---

## Known Limitations

- **`averageFlowMGD` and `peakFlowMGD` are almost always null** — these appear in permit applications (Form 2A), not in final permits. No public index for applications is currently used.
- **Process train connections (RAS/WAS) always empty** — require machine-readable process flow diagrams, which are not present in ADEM permit PDFs.
- **Alabama ADEM only** — the Playwright automation, state monitoring defaults, and regex patterns are calibrated for Alabama ADEM permits. Other states require additional Playwright scripts and defaults entries.
- **PDF table extraction unreliable** — Document AI extracts text accurately but rarely produces structured table rows from scanned ADEM PDFs. All permit limit values come from ECHO, not the PDF itself.
- **`processType` field is not vocabulary-controlled** — GPT-4o returns free-text strings with no enforced vocabulary, making the field unreliable for filtering across plants.
- **Water use classification regex can over-fire** — the pattern has been seen to capture sentence fragments instead of a clean classification code on some permits.
- **ADEM eFile Playwright is fragile** — the portal uses server-side AJAX with 65-second timeouts and hardcoded DOM selectors. Portal changes will break downloads.
- **`POST /seed` has no timeout** — the full pipeline takes 5–10 minutes. HTTP clients with short timeouts will disconnect before the response arrives. A job-queue pattern is recommended for production.
- **`db_apply.py` untested against a live Firestore instance** — always run `--dry-run` first to verify collection paths match your schema.
- **API has no authentication** — `api.py` is open by default. Add an auth middleware before any network-accessible deployment.

---

## Project Structure

```
plant-knowledge-seeding-skill/
├── api.py                          ← FastAPI server
├── requirements.txt
├── .env.example
├── cache/                          ← pre-seeded plant packages (committed)
├── downloads/                      ← permit PDFs (not committed)
├── references/
│   ├── nymph_schema.json
│   ├── state_monitoring_defaults.json
│   ├── parameter_aliases.json
│   ├── process_unit_vocab.json
│   ├── permit_limit_examples.json
│   ├── state_portal_patterns.md
│   └── source_confidence_rules.md
└── scripts/
    ├── seed_runner.py              ← main pipeline orchestrator
    ├── echo_lookup.py              ← EPA ECHO API client
    ├── permit_parser.py            ← GPT-4o permit text extraction
    ├── pdf_extractor.py            ← Document AI + Tesseract OCR
    ├── adem_playwright.py          ← Alabama ADEM eFile automation
    ├── seed_validator.py           ← 11-guardrail validation
    ├── db_readback.py              ← seed package readback report
    ├── db_apply.py                 ← Firestore apply script
    └── provenance_linter.py        ← confidence score linter
```
