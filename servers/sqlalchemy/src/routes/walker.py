import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Body
from sqlalchemy import select, delete, insert, union_all
from sqlalchemy.orm import Session, aliased, selectinload, joinedload
from datetime import datetime

from src import get_db, require_keys
from src.models import (
    User, Tweet, Comment, Channel,
    like_table, following_table, channel_members_table,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization:
        raise HTTPException(status_code=401, detail="Not Logged In")
    username = authorization.replace("Bearer ", "")
    user = db.execute(select(User).filter_by(username=username)).scalar()
    if not user:
        raise HTTPException(
            status_code=400,
            detail=f"User with username {username} not found. Did the database get cleared?",
        )
    return user


# ---------------------------------------------------------------------------
# Response helpers — keep same shape as Flask version
# ---------------------------------------------------------------------------

def build_response(reports, result=None, status_code: int = 200):
    if result is None:
        result = reports[0] if len(reports) == 1 else reports
    return {
        "data": {
            "result": result,
            "reports": reports,
        }
    }


def singleton_response(payload: dict, status_code: int = 200):
    """For endpoints returning a single object (create/update/follow/etc)."""
    return build_response([payload], result=payload, status_code=status_code)


def list_response(items: list, status_code: int = 200):
    """For endpoints returning a collection (load_feed, get_all_profiles)."""
    return build_response(items, result=items, status_code=status_code)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/setup_profile")
def setup_profile(
    body: Optional[dict] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Match JacSQL: username and bio are optional; only update what's provided.
    data = body or {}
    if data.get("username"):
        current_user.handle = data["username"]
    if "bio" in data:
        current_user.bio = data["bio"]
    db.commit()
    db.refresh(current_user)
    return singleton_response(current_user.report())


@router.post("/load_feed")
def load_feed(
    body: Optional[dict] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Match JacSQL: search_query is optional and defaults to "".
    data = body or {}
    search_query = data.get("search_query", "")

    other_user = aliased(User)
    eligible_users = aliased(User, union_all(
        select(other_user.id)
        .where(User.id == current_user.id)
        .join(other_user, User.following),
        select(User.id).where(User.id == current_user.id)
    ).subquery())

    tweets_query = (
        select(Tweet)
        .join_from(eligible_users, eligible_users.tweets)
        .options(
            joinedload(Tweet.author),
            selectinload(Tweet.likes),
            selectinload(Tweet.comments),
        )
    )

    if search_query:
        tweets_query = tweets_query.filter(Tweet.content.ilike(f"%{search_query}%"))

    tweets_query = tweets_query.order_by(Tweet.created_at)
    if "limit" in data:
        tweets_query = tweets_query.limit(data["limit"])

    results = db.execute(tweets_query).unique().scalars().all()
    return list_response([r.report() for r in results])


@router.post("/get_profile")
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Re-fetch current_user with eager-loaded relationships to avoid N+1.
    user = db.execute(
        select(User)
        .where(User.id == current_user.id)
        .options(
            selectinload(User.following),
            selectinload(User.followers),
            selectinload(User.tweets).joinedload(Tweet.author),
            selectinload(User.tweets).selectinload(Tweet.likes),
            selectinload(User.tweets).selectinload(Tweet.comments),
        )
    ).unique().scalar()
    return singleton_response(user.report(True))


@router.post("/get_all_profiles")
def get_all_profiles(db: Session = Depends(get_db)):
    results = db.execute(select(User.id, User.handle, User.bio)).all()
    profiles = [{"id": r[0], "username": r[1], "bio": r[2]} for r in results]
    return list_response(profiles)


@router.post("/follow_user")
def follow_user(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_keys(body, "target_id")

    target = db.execute(select(User).filter_by(id=body["target_id"])).scalar()
    if not target:
        raise HTTPException(status_code=400, detail="User not found")

    if current_user not in target.followers:
        target.followers.add(current_user)
        db.commit()

    return singleton_response({"success": True})


@router.post("/unfollow_user")
def unfollow_user(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_keys(body, "target_id")

    result = db.execute(
        delete(following_table)
        .where(following_table.c.followee_id == body["target_id"])
        .where(following_table.c.follower_id == current_user.id)
    )

    if result.rowcount == 0:
        raise HTTPException(status_code=400, detail="User not found")

    db.commit()
    return singleton_response({"success": True})


@router.post("/create_tweet")
def create_tweet(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_keys(body, "content")

    new_tweet = Tweet(
        content=body["content"],
        author_id=current_user.id,
        created_at=datetime.utcnow(),
        likes=[],
        comments=[],
    )
    db.add(new_tweet)
    db.commit()
    db.refresh(new_tweet)
    return singleton_response(new_tweet.report())


@router.post("/delete_tweet")
def delete_tweet(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_keys(body, "tweet_id")

    result = db.execute(
        delete(Tweet)
        .where(Tweet.id == body["tweet_id"])
        .where(Tweet.author_id == current_user.id)
    )

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Tweet not found")

    db.commit()
    return singleton_response({"success": True})


@router.post("/like_tweet")
def like_tweet(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_keys(body, "tweet_id")

    tweet = db.execute(select(Tweet).where(Tweet.id == body["tweet_id"])).scalar()
    if not tweet:
        raise HTTPException(status_code=404, detail="Tweet not found")

    existing_like = db.execute(
        select(like_table)
        .where(like_table.c.tweet_id == body["tweet_id"])
        .where(like_table.c.user_id == current_user.id)
    ).first() is not None

    if not existing_like:
        tweet.likes.append(current_user)
    else:
        tweet.likes.remove(current_user)

    # Keep the denormalized like_count in sync with the likes relationship (R6).
    tweet.like_count = len(tweet.likes)
    db.commit()
    return singleton_response({"liked": not existing_like, "likes": tweet.report_likes()})


@router.post("/add_comment")
def add_comment(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_keys(body, "tweet_id", "content")

    tweet = db.execute(select(Tweet).where(Tweet.id == body["tweet_id"])).scalar()
    if not tweet:
        raise HTTPException(status_code=404, detail="Tweet not found")

    new_comment = Comment(
        handle=current_user.handle,
        content=body["content"],
        tweet_id=tweet.id,
        created_at=datetime.utcnow(),
    )
    db.add(new_comment)
    db.commit()
    return singleton_response({"success": True, "comment": new_comment.report()})


@router.post("/create_channel")
def create_channel(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Untimed setup for the own-tweets selectivity sweep: creates a
    # channel row and enrolls current_user as a member.
    require_keys(body, "name")
    description = body.get("description", "") or ""
    new_channel = Channel(name=body["name"], description=description)
    db.add(new_channel)
    db.flush()
    db.execute(channel_members_table.insert().values(
        user_id=current_user.id, channel_id=new_channel.id,
    ))
    db.commit()
    return singleton_response({"id": new_channel.id, "name": new_channel.name})


@router.post("/load_own_tweets")
def load_own_tweets(
    body: Optional[dict] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Mirrors Jac's `walker load_own_tweets`: returns ALL of the caller's own tweets
    # (no follow-traversal, NO like_count predicate — reconciliation spec §4).
    # `threshold` is the filter-pushdown seam (spec §10): default off; pass
    # {"threshold": k} for the future FP sweep (#21) — it binds into the WHERE and
    # pushes down to the index. server_timing (fair-timing spec §3): ms_fetch = SQL
    # round-trip; ms_build = per-tweet .report() ORM hydration (the tax Jac avoids).
    t_entry = time.perf_counter()
    threshold = (body or {}).get("threshold")
    stmt = (
        select(Tweet)
        .where(Tweet.author_id == current_user.id)
        .options(
            joinedload(Tweet.author),
            selectinload(Tweet.likes),
            selectinload(Tweet.comments),
        )
        # No ORDER BY: jac/neo4j return unordered and the Phase-5 oracle is a multiset
        # compare, so a sort here is asymmetric work (sqla would pay it; the graph
        # backends don't) — review fix.
    )
    if threshold is not None:
        stmt = stmt.where(Tweet.like_count > threshold)
    tweets = db.execute(stmt).unique().scalars().all()
    ms_fetch = (time.perf_counter() - t_entry) * 1000

    t1 = time.perf_counter()
    payload = [t.report() for t in tweets]
    ms_build = (time.perf_counter() - t1) * 1000

    report = {
        "tweets": payload,
        "server_timing": {
            "ms_fetch": round(ms_fetch, 4),
            "ms_build": round(ms_build, 4),
            "server_total": round((time.perf_counter() - t_entry) * 1000, 4),
        },
    }
    return build_response([report], result=payload)


@router.post("/import_data")
def import_data(
    body: Optional[dict] = Body(default=None),
    db: Session = Depends(get_db),
):
    if body is None:
        raise HTTPException(status_code=422, detail="Expected JSON body")

    all_user_ids = db.scalars(select(User.id)).all()
    for user in body["data"].values():
        user_obj = db.execute(
            select(User).filter_by(username=user["email"])
        ).scalar()
        # insert all tweets at once; get the tweet IDs back in insertion order
        if len(user["tweets"]) > 0:
            ids = db.scalars(
                insert(Tweet).returning(Tweet.id, sort_by_parameter_order=True),
                [{
                    "content": t["content"],
                    "author_id": user_obj.id,
                    "created_at": datetime.fromisoformat(t["timestamp"]),
                } for t in user["tweets"]],
            ).all()
            for idx, tweet in enumerate(user["tweets"]):
                tweet_id = ids[idx]
                if tweet["likes"] > 0:
                    likes = [
                        {"tweet_id": tweet_id, "user_id": all_user_ids[user_idx]}
                        for user_idx in range(0, min(tweet["likes"], len(all_user_ids)))
                    ]
                    db.execute(like_table.insert(), likes)

        follows = [
            {"followee_id": followee, "follower_id": user_obj.id}
            for followee in user["following"]
        ]
        if len(follows) > 0:
            db.execute(following_table.insert(), follows)

    viewer_obj = db.execute(select(User).filter_by(handle="Viewer")).scalar()
    viewer_follows = [
        {"followee_id": user_id, "follower_id": viewer_obj.id}
        for user_id in all_user_ids
        if user_id != viewer_obj.id
    ]
    db.execute(following_table.insert(), viewer_follows)

    db.commit()
    return singleton_response({"success": True})


# ---------------------------------------------------------------------------
# seed_tweets — PUBLIC (no auth). Single-hop benchmark seeding (seed-design-spec).
# Body: {author_username, likers:[name...],
#        tweets:[{content, created_at, like_count, likers:[name...],
#                 comments:[{author, content, created_at}]}]}
# ---------------------------------------------------------------------------

def _seed_parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seed_upsert_user(db: Session, username: str) -> User:
    user = db.execute(select(User).filter_by(username=username)).scalar()
    if user is None:
        user = User(username=username, handle=username, password="",
                    bio="", created_at=datetime.utcnow())
        db.add(user)
        db.flush()
    return user


@router.post("/seed_tweets")
def seed_tweets(
    body: Optional[dict] = Body(default=None),
    db: Session = Depends(get_db),
):
    if body is None:
        raise HTTPException(status_code=422, detail="Expected JSON body")

    author = _seed_upsert_user(db, body["author_username"])
    liker_ids = {name: _seed_upsert_user(db, name).id
                 for name in body.get("likers", [])}

    tweets = body.get("tweets", [])
    if tweets:
        ids = db.scalars(
            insert(Tweet).returning(Tweet.id, sort_by_parameter_order=True),
            [{
                "content": t["content"],
                "author_id": author.id,
                "created_at": _seed_parse_ts(t["created_at"]),
                "like_count": t["like_count"],
            } for t in tweets],
        ).all()
        for idx, t in enumerate(tweets):
            tweet_id = ids[idx]
            likes = [{"tweet_id": tweet_id, "user_id": liker_ids[name]}
                     for name in t.get("likers", []) if name in liker_ids]
            if likes:
                db.execute(like_table.insert(), likes)
            for com in t.get("comments", []):
                db.add(Comment(handle=com["author"], content=com["content"],
                               tweet_id=tweet_id,
                               created_at=_seed_parse_ts(com["created_at"])))

    # Channel noise for the type-selectivity neighborhood (spec §6.4). Polymorphic
    # STI is the future unoptimized variant; here (optimized) channels live in their
    # own table, so load_own_tweets never touches them.
    channels = body.get("channels", [])
    for ch in channels:
        new_channel = Channel(name=ch.get("name", ch.get("key", "")), description="")
        db.add(new_channel)
        db.flush()
        db.execute(channel_members_table.insert().values(
            user_id=author.id, channel_id=new_channel.id))

    db.commit()
    return singleton_response({"success": True, "seeded": len(tweets),
                               "seeded_tweets": len(tweets),
                               "seeded_channels": len(channels)})


# ---------------------------------------------------------------------------
# clear_data — PUBLIC. Best-effort full wipe (harness --reset only).
# ---------------------------------------------------------------------------

@router.post("/clear_data")
def clear_data(db: Session = Depends(get_db)):
    db.execute(delete(Comment))
    db.execute(like_table.delete())
    db.execute(following_table.delete())
    db.execute(channel_members_table.delete())
    db.execute(delete(Channel))
    db.execute(delete(Tweet))
    db.execute(delete(User))
    db.commit()
    return singleton_response({"success": True})
