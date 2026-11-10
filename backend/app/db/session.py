from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one database session per request, closed afterwards.

    Endpoints call `await db.commit()` themselves; anything not committed is rolled back.
    """
    async with request.app.state.db_sessionmaker() as session:
        yield session
