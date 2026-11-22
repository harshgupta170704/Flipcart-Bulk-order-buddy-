"""Health / meta endpoints."""

from fastapi import APIRouter

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["meta"])


@router.get("/")
def root() -> dict:
    settings = get_settings()
    return {"app": settings.app_name, "version": __version__, "status": "ok"}


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}
