import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.session import SessionFactory

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: Annotated[str | None, Security(api_key_header)]) -> None:
    if key is None or not secrets.compare_digest(key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]
