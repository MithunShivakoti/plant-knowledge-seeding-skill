"""
api.py — FastAPI wrapper around the Nymph Plant Knowledge Seeding pipeline.

Endpoints:
  GET  /               — health check
  GET  /plants         — list all cached seed packages
  GET  /seed/{npdes}   — return cached seed package for an NPDES number
  POST /seed           — run the full pipeline (returns from cache if already seeded)

Cache: completed seed packages are stored in cache/{npdes}_seed_package.json.
       Subsequent requests for the same NPDES are served instantly from cache.

Usage:
  uvicorn api:app --reload --port 8000
"""

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

app = FastAPI(
    title="Nymph Plant Knowledge Seeding Skill",
    description="Public-source plant knowledge seeding pipeline for Nymph WWTP platform.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SeedRequest(BaseModel):
    plant: str
    location: str
    npdes: str
    owner: str = ""


class PlantSummary(BaseModel):
    npdes: str
    plantName: str | None
    city: str | None
    state: str | None
    status: str | None
    cachedAt: str | None = None


class SeedResponse(BaseModel):
    npdes: str
    status: str
    cached: bool
    data: dict


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(npdes: str) -> Path:
    return CACHE_DIR / f"{npdes.upper()}_seed_package.json"


def _load_cache(npdes: str) -> dict | None:
    p = _cache_path(npdes)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(npdes: str, data: dict) -> None:
    p = _cache_path(npdes)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Pipeline runner (blocking — executed in thread pool)
# ---------------------------------------------------------------------------

def _run_blocking(plant: str, location: str, npdes: str, owner: str) -> dict:
    from seed_runner import run_pipeline
    return run_pipeline(
        plant_name=plant,
        location=location,
        npdes=npdes,
        owner=owner,
        output_path=str(_cache_path(npdes)),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", tags=["health"])
def health():
    """Health check."""
    return {
        "status": "ok",
        "service": "Nymph Plant Knowledge Seeding Skill",
        "cached_plants": len(list(CACHE_DIR.glob("*_seed_package.json"))),
    }


@app.get("/plants", tags=["plants"], response_model=dict)
def list_cached_plants():
    """List all plants with cached seed packages."""
    plants = []
    for f in sorted(CACHE_DIR.glob("*_seed_package.json")):
        npdes = f.stem.replace("_seed_package", "")
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            identity = data.get("identity", {})
            plants.append({
                "npdes":      npdes,
                "plantName":  identity.get("plantName"),
                "city":       identity.get("city"),
                "state":      identity.get("state"),
                "status":     data.get("auditRun", {}).get("proposalStatus"),
                "processType": data.get("overview", {}).get("basics", {}).get("processType"),
                "designFlowMGD": data.get("overview", {}).get("basics", {}).get("designFlowMGD"),
                "permitParams": len(data.get("permitLimits", [])),
                "liquidNodes":  len(data.get("overview", {}).get("processTrain", {}).get("liquid", [])),
            })
        except Exception:
            plants.append({"npdes": npdes, "error": "Could not parse cache file"})
    return {"plants": plants, "count": len(plants)}


@app.get("/seed/{npdes}", tags=["seed"], response_model=SeedResponse)
def get_cached_seed(npdes: str):
    """Return a cached seed package by NPDES number. 404 if not yet seeded."""
    npdes = npdes.upper().strip()
    data = _load_cache(npdes)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No cached seed package for {npdes}. POST /seed to run the pipeline.",
        )
    return SeedResponse(npdes=npdes, status="success", cached=True, data=data)


@app.post("/seed", tags=["seed"], response_model=SeedResponse)
async def seed_plant(req: SeedRequest):
    """
    Run the full seeding pipeline for a plant.
    Returns immediately from cache if the NPDES has already been seeded.
    If not cached, runs the pipeline (takes ~5-10 min per plant).
    """
    npdes = req.npdes.upper().strip()

    # Serve from cache if available
    cached = _load_cache(npdes)
    if cached is not None:
        return SeedResponse(npdes=npdes, status="success", cached=True, data=cached)

    # Run pipeline in thread pool (non-blocking for the event loop)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            _executor,
            _run_blocking,
            req.plant,
            req.location,
            npdes,
            req.owner,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {exc}")

    if not result:
        raise HTTPException(status_code=500, detail="Pipeline returned empty result")

    _save_cache(npdes, result)
    return SeedResponse(npdes=npdes, status="success", cached=False, data=result)


@app.delete("/seed/{npdes}", tags=["seed"])
def invalidate_cache(npdes: str):
    """Remove a cached seed package so the next POST /seed re-runs the pipeline."""
    npdes = npdes.upper().strip()
    p = _cache_path(npdes)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"No cache entry for {npdes}")
    p.unlink()
    return {"npdes": npdes, "status": "cache_cleared"}
