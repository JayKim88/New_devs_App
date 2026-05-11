import asyncio
from typing import AsyncIterator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import logging
from ..config import settings

logger = logging.getLogger(__name__)

class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        
    async def initialize(self):
        """Initialize database connection pool"""
        try:
            # Build async URL from the single configured database_url.
            # Settings only defines `database_url` (e.g. postgresql://…); convert it
            # to the asyncpg driver form expected by create_async_engine.
            database_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            
            # NOTE: Removed `poolclass=QueuePool` — sync QueuePool is incompatible
            # with create_async_engine (SQLAlchemy 2.0). The async engine selects
            # AsyncAdaptedQueuePool by default, which supports the same options below.
            self.engine = create_async_engine(
                database_url,
                pool_size=20,
                max_overflow=30,
                pool_pre_ping=True,
                pool_recycle=3600,
                echo=False
            )
            
            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False
            )
            
            logger.info("✅ Database connection pool initialized")
            
        except Exception as e:
            logger.error(f"❌ Database pool initialization failed: {e}")
            self.engine = None
            self.session_factory = None
    
    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
    
    def get_session(self) -> AsyncSession:
        """Get database session from pool.

        Not async: `session_factory()` is a sync call that returns an
        AsyncSession (which is itself an async context manager). Marking this
        `async def` forced callers into `async with await ...`, but they all
        use `async with db_pool.get_session() as session:` — so keep this sync.
        """
        if not self.session_factory:
            raise Exception("Database pool not initialized")
        return self.session_factory()

# Global database pool instance
db_pool = DatabasePool()

async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
