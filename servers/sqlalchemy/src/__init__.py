from fastapi import FastAPI, HTTPException, Depends, Header
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase, declared_attr
from contextlib import asynccontextmanager
from typing import Optional
import re
import os


# ---------------------------------------------------------------------------
# Base / db shim
#
# models.py does `from src import db` and then uses:
#   db.Model  — as the ORM declarative base class
#   db.Model.metadata  — as the MetaData for association tables
#
# Flask-SQLAlchemy's db.Model auto-derives __tablename__ from the class name
# (lowercase). We replicate that so models.py can stay unmodified.
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    @declared_attr.directive
    def __tablename__(cls) -> str:
        # Convert CamelCase to snake_case, e.g. ChannelMember -> channel_member
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()
        return name


class _DbShim:
    """Minimal shim so models.py can do `db.Model` without Flask-SQLAlchemy."""
    Model = Base


db = _DbShim()

# Re-export Base at module level so tests can do `from src.models import Base`
# (models.py inherits from db.Model which IS Base, so Base.metadata covers all
# tables declared in models.py).


# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+pysqlite:///:memory:")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Accept either psycopg3 (postgresql+psycopg://) or psycopg2 URLs; normalise
    # to psycopg2 because we ship psycopg2-binary in requirements.txt.
    url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+psycopg2://")
    engine = create_engine(
        url,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency: yield one Session per request, always close it."""
    db_session = SessionLocal()
    try:
        yield db_session
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Helpers used by routes
# ---------------------------------------------------------------------------

def require_keys(body: dict, *keys: str):
    """Raise HTTP 422 if any of *keys are missing from body."""
    for k in keys:
        if k not in body:
            raise HTTPException(status_code=422, detail=f"Missing expected key {k}")


# ---------------------------------------------------------------------------
# App + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Import models here (after db shim is ready) so table metadata is
    # registered before create_all runs.
    import src.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)

# Import models to ensure they are registered with Base.metadata before
# the routers are wired up (routers import model classes).
import src.models  # noqa: F401

from src.routes.user import router as user_router
from src.routes.walker import router as walker_router

app.include_router(user_router, prefix="/user")
app.include_router(walker_router, prefix="/walker")
app.include_router(walker_router, prefix="/function")


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /walker/clear_data  and  /function/clear_data
# ---------------------------------------------------------------------------

def _clear_data(db_session: Session = Depends(get_db)):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return {
        "data": {
            "result": {"success": True, "message": "Database reset"},
            "reports": [{"success": True, "message": "Database reset"}],
        }
    }


app.add_api_route("/walker/clear_data", _clear_data, methods=["POST"])
app.add_api_route("/function/clear_data", _clear_data, methods=["POST"])
