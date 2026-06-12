"""User auth routes — register / login.

Migrated from Flask Blueprint to FastAPI APIRouter.
Direct psycopg, no ORM. Both endpoints are single-statement.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException

from src import db


router = APIRouter()


def _resp(payload, status_code=200):
    return {"data": payload}


@router.post("/register")
def register(body: dict):
    # Validate required keys
    for key in ("username", "password"):
        if key not in body:
            raise HTTPException(status_code=422, detail=f"Missing expected key {key}")

    try:
        with db.conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, handle, password, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (body["username"], body["username"], body["password"], datetime.utcnow()),
            )
            row = cur.fetchone()
            uid = row["id"]
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            raise HTTPException(status_code=400, detail="User with username already exists")
        raise HTTPException(status_code=500, detail=f"register failed: {exc}")

    return _resp({
        "username": body["username"],
        "token": body["username"],
        "root_id": str(uid),
    })


@router.post("/login")
def login(body: dict):
    for key in ("username", "password"):
        if key not in body:
            raise HTTPException(status_code=422, detail=f"Missing expected key {key}")

    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE username = %s AND password = %s",
            (body["username"], body["password"]),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="User with provided username/password not found")

    return _resp({
        "username": body["username"],
        "token": body["username"],
        "root_id": str(row["id"]),
    })
