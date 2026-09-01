"""Foundation-only API health surface.

Business routes are frozen in docs/contracts/openapi.json and intentionally not
implemented in Phase 1.
"""

from fastapi import FastAPI

app = FastAPI(
    title="MicroLens Recommendation MVP API",
    version="0.0.0-foundation",
    description="Foundation health surface; business operations are contract-only.",
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api", "phase": "foundation"}


@app.get("/ready", tags=["system"])
def ready() -> dict[str, object]:
    return {
        "status": "ready",
        "service": "api",
        "phase": "foundation",
        "checks": {"process": "ok", "contract_surface": "loaded"},
        "business_routes_ready": False,
    }
