from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import datetime

from src import get_db, require_keys
from src.models import User

router = APIRouter()


# ---------------------------------------------------------------------------
# Response helpers — keep same shape as Flask version
# ---------------------------------------------------------------------------

def build_response(data: dict, status_code: int = 200):
    return {"data": data}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/register")
def register(body: dict, db: Session = Depends(get_db)):
    require_keys(body, "username", "password")

    existing_user = db.execute(select(User).filter_by(username=body["username"])).scalar()
    if existing_user:
        raise HTTPException(status_code=400, detail="User with username already exists")

    new_user = User(
        username=body["username"],
        handle=body["username"],
        password=body["password"],
        created_at=datetime.utcnow(),
        bio="",
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return build_response({"username": body["username"], "token": body["username"], "root_id": new_user.id})


@router.post("/login")
def login(body: dict, db: Session = Depends(get_db)):
    require_keys(body, "username", "password")

    existing_user = db.execute(
        select(User).filter_by(username=body["username"], password=body["password"])
    ).scalar()
    if not existing_user:
        raise HTTPException(status_code=400, detail="User with provided username/password not found")

    return build_response({"username": body["username"], "token": body["username"], "root_id": existing_user.id})
