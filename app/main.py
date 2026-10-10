from fastapi import FastAPI

from app.api.jobs import router as jobs_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(jobs_router)


@app.get("/health", tags=["Health"])
def health_check() -> dict[str, str]:
    """Health check endpoint to verify that the application is running."""
    return {"status": "ok"}
