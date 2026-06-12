"""LittleX hand-tuned Postgres backend — FastAPI edition.

Migrated from Flask: Blueprint → APIRouter, g.user → Depends,
jsonify → return dict, before_request auth → get_current_user dependency.
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from src import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.bootstrap()
    except Exception as exc:
        print(f"[db] bootstrap failed: {exc}")
    yield
    db.close_pool()


app = FastAPI(lifespan=lifespan)


def build_error(message: str, status_code: int):
    return JSONResponse(status_code=status_code, content={"error": message})


def get_validated_body(data: dict, keys: list):
    for key in keys:
        if key not in data:
            raise HTTPException(status_code=422, detail=f"Missing expected key {key}")
    return data


from src.routes.user import router as user_router    # noqa: E402
from src.routes.walker import router as walker_router  # noqa: E402

app.include_router(user_router, prefix="/user")
app.include_router(walker_router, prefix="/walker")
app.include_router(walker_router, prefix="/function")


def _reset_db():
    db.reset()
    return {"data": {"result": {"success": True, "message": "Database reset"}, "reports": [{"success": True, "message": "Database reset"}]}}


app.add_api_route("/walker/clear_data", _reset_db, methods=["POST"])
app.add_api_route("/function/clear_data", _reset_db, methods=["POST"])


@app.get("/health")
def health():
    return {"status": "ok"}
