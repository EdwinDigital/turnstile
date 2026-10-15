from __future__ import annotations

from ..config import Settings
from .in_memory import InMemoryRepository
from .repository import PostgreSqlOpsDbProxy, QueryRepository


def create_repository(settings: Settings, *, autocommit: bool = False) -> QueryRepository:
    if settings.data_backend == "demo":
        if settings.production:
            raise RuntimeError("DATA_BACKEND=demo is forbidden when PRODUCTION=true")
        if settings.database_url:
            raise RuntimeError("DATABASE_URL must be unset when DATA_BACKEND=demo")
        return InMemoryRepository()

    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is required when DATA_BACKEND=postgresql; "
            "use DATA_BACKEND=demo only for an explicit local demo"
        )
    return PostgreSqlOpsDbProxy(
        settings.database_url, apim_api_id=settings.apim_api_id, autocommit=autocommit,
    )