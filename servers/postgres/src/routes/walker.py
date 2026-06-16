"""Hand-tuned LittleX walker route handlers — FastAPI edition.

Migrated from Flask Blueprint + g.user to FastAPI APIRouter + Depends.
All routes use sync `def` (FastAPI runs them in a threadpool automatically).
No async def — psycopg is synchronous and blocking async would deadlock
the event loop.

Auth: get_current_user dependency replaces before_request check_login.
Public endpoints skip auth by not including the Depends parameter.
"""

import time
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Body
from fastapi.responses import JSONResponse

import psycopg

from src import db


router = APIRouter()


# ---------------------------------------------------------------------------
# Auth dependency — replaces Flask's before_request check_login + g.user
# ---------------------------------------------------------------------------

def get_current_user(authorization: str = Header(default=None)) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="Not Logged In")
    username = authorization.replace("Bearer ", "").strip()
    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, username, handle, bio FROM users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"User with username {username} not found. Did the database get cleared?",
        )
    return row


# ---------------------------------------------------------------------------
# Response helpers — same logic as Flask version but return dicts
# ---------------------------------------------------------------------------

def build_response(reports, result=None, status_code=200):
    if result is None:
        result = reports[0] if len(reports) == 1 else reports
    payload = {"data": {"result": result, "reports": reports}}
    if status_code == 200:
        return payload
    return JSONResponse(status_code=status_code, content=payload)


def singleton_response(payload, status_code=200):
    return build_response([payload], result=payload, status_code=status_code)


def list_response(items, status_code=200):
    return build_response(items, result=items, status_code=status_code)


# ---------------------------------------------------------------------------
# setup_profile
# ---------------------------------------------------------------------------

@router.post("/setup_profile")
def setup_profile(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    new_handle = body.get("username")
    new_bio = body.get("bio")

    sets = []
    params = []
    if new_handle:
        sets.append("handle = %s")
        params.append(new_handle)
    if new_bio is not None:
        sets.append("bio = %s")
        params.append(new_bio)

    if sets:
        params.append(current_user["id"])
        with db.conn() as c, c.cursor() as cur:
            cur.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = %s "
                f"RETURNING id, handle, bio, created_at",
                params,
            )
            row = cur.fetchone()
    else:
        row = current_user

    return singleton_response({
        "id": row["id"],
        "username": row.get("handle", ""),
        "bio": row.get("bio", ""),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else "",
    })


# ---------------------------------------------------------------------------
# load_feed
# ---------------------------------------------------------------------------

@router.post("/load_feed")
def load_feed(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    search_query = body.get("search_query", "") or ""
    limit_clause = ""
    params = [current_user["id"], current_user["id"], search_query, search_query]
    if "limit" in body:
        limit_clause = "LIMIT %s"
        params.append(int(body["limit"]))

    sql = f"""
        WITH eligible AS (
            SELECT id FROM users WHERE id = %s
            UNION
            SELECT followee_id AS id FROM follows WHERE follower_id = %s
        ),
        feed AS (
            SELECT
                t.id,
                t.content,
                t.created_at,
                u.username AS author_username
            FROM tweets t
            JOIN eligible e ON e.id = t.author_id
            JOIN users u ON u.id = t.author_id
            WHERE %s = '' OR t.content ILIKE '%%' || %s || '%%'
            ORDER BY t.created_at DESC
            {limit_clause}
        )
        SELECT COALESCE(json_agg(
            json_build_object(
                'id', f.id,
                'content', f.content,
                'author_username', f.author_username,
                'created_at', f.created_at,
                'likes', COALESCE((
                    SELECT json_agg(u2.username)
                    FROM likes l JOIN users u2 ON u2.id = l.user_id
                    WHERE l.tweet_id = f.id
                ), '[]'::json),
                'comments', COALESCE((
                    SELECT json_agg(json_build_object(
                        'username', c.author_handle,
                        'content', c.content,
                        'created_at', c.created_at
                    ) ORDER BY c.created_at)
                    FROM comments c WHERE c.tweet_id = f.id
                ), '[]'::json)
            )
        ), '[]'::json) AS feed
        FROM feed f
    """
    with db.conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return list_response(row["feed"] or [])


# ---------------------------------------------------------------------------
# get_profile
# ---------------------------------------------------------------------------

@router.post("/get_profile")
def get_profile(current_user: dict = Depends(get_current_user)):
    sql = """
        SELECT json_build_object(
            'id', u.id,
            'username', u.handle,
            'bio', u.bio,
            'created_at', u.created_at,
            'following', COALESCE((
                SELECT json_agg(json_build_object('id', u2.id, 'username', u2.username))
                FROM follows f
                JOIN users u2 ON u2.id = f.followee_id
                WHERE f.follower_id = u.id
            ), '[]'::json),
            'followers', COALESCE((
                SELECT json_agg(json_build_object('id', u3.id, 'username', u3.username))
                FROM follows f
                JOIN users u3 ON u3.id = f.follower_id
                WHERE f.followee_id = u.id
            ), '[]'::json),
            'tweets', COALESCE((
                SELECT json_agg(json_build_object(
                    'id', t.id,
                    'content', t.content,
                    'author_username', u.username,
                    'created_at', t.created_at,
                    'likes', COALESCE((
                        SELECT json_agg(u4.username)
                        FROM likes l JOIN users u4 ON u4.id = l.user_id
                        WHERE l.tweet_id = t.id
                    ), '[]'::json),
                    'comments', COALESCE((
                        SELECT json_agg(json_build_object(
                            'username', c.author_handle,
                            'content', c.content,
                            'created_at', c.created_at
                        ))
                        FROM comments c WHERE c.tweet_id = t.id
                    ), '[]'::json)
                ) ORDER BY t.created_at DESC)
                FROM tweets t WHERE t.author_id = u.id
            ), '[]'::json)
        ) AS profile
        FROM users u WHERE u.id = %s
    """
    with db.conn() as c, c.cursor() as cur:
        cur.execute(sql, (current_user["id"],))
        row = cur.fetchone()
    if not row or not row["profile"]:
        raise HTTPException(status_code=404, detail="Profile not found")
    return singleton_response(row["profile"])


# ---------------------------------------------------------------------------
# get_all_profiles — PUBLIC (no auth)
# ---------------------------------------------------------------------------

@router.post("/get_all_profiles")
def get_all_profiles():
    with db.conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, handle, bio FROM users")
        rows = cur.fetchall()
    profiles = [{"id": r["id"], "username": r["handle"], "bio": r["bio"]} for r in rows]
    return list_response(profiles)


# ---------------------------------------------------------------------------
# follow_user
# ---------------------------------------------------------------------------

@router.post("/follow_user")
def follow_user(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    if "target_id" not in body:
        raise HTTPException(status_code=422, detail="Missing expected key target_id")
    try:
        target_id = int(body["target_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid target_id")

    try:
        with db.conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO follows (follower_id, followee_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (current_user["id"], target_id),
            )
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="User not found")

    return singleton_response({"success": True})


# ---------------------------------------------------------------------------
# unfollow_user
# ---------------------------------------------------------------------------

@router.post("/unfollow_user")
def unfollow_user(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    if "target_id" not in body:
        raise HTTPException(status_code=422, detail="Missing expected key target_id")
    try:
        target_id = int(body["target_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid target_id")

    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM follows WHERE follower_id = %s AND followee_id = %s",
            (current_user["id"], target_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=400, detail="User not found")
    return singleton_response({"success": True})


# ---------------------------------------------------------------------------
# create_tweet
# ---------------------------------------------------------------------------

@router.post("/create_tweet")
def create_tweet(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    if "content" not in body:
        raise HTTPException(status_code=422, detail="Missing expected key content")

    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tweets (author_id, content)
            VALUES (%s, %s)
            RETURNING id, created_at
            """,
            (current_user["id"], body["content"]),
        )
        row = cur.fetchone()

    return singleton_response({
        "id": row["id"],
        "content": body["content"],
        "author_username": current_user.get("username", ""),
        "created_at": row["created_at"].isoformat(),
        "likes": [],
        "comments": [],
    })


# ---------------------------------------------------------------------------
# delete_tweet
# ---------------------------------------------------------------------------

@router.post("/delete_tweet")
def delete_tweet(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    if "tweet_id" not in body:
        raise HTTPException(status_code=422, detail="Missing expected key tweet_id")
    try:
        tid = int(body["tweet_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid tweet_id")

    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM tweets WHERE id = %s AND author_id = %s",
            (tid, current_user["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Tweet not found")
    return singleton_response({"success": True})


# ---------------------------------------------------------------------------
# like_tweet
# ---------------------------------------------------------------------------

@router.post("/like_tweet")
def like_tweet(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    if "tweet_id" not in body:
        raise HTTPException(status_code=422, detail="Missing expected key tweet_id")
    try:
        tid = int(body["tweet_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid tweet_id")

    uid = current_user["id"]

    with db.conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tweets WHERE id = %s", (tid,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Tweet not found")

        cur.execute(
            "INSERT INTO likes (tweet_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tid, uid),
        )
        liked = cur.rowcount > 0
        if not liked:
            cur.execute(
                "DELETE FROM likes WHERE tweet_id = %s AND user_id = %s",
                (tid, uid),
            )

        # Keep the denormalized like_count in sync with the likes table (R6).
        cur.execute(
            "UPDATE tweets SET like_count = "
            "(SELECT COUNT(*) FROM likes WHERE tweet_id = %s) WHERE id = %s",
            (tid, tid),
        )

        cur.execute(
            """
            SELECT COALESCE(json_agg(u.username), '[]'::json) AS likes
            FROM likes l JOIN users u ON u.id = l.user_id
            WHERE l.tweet_id = %s
            """,
            (tid,),
        )
        row = cur.fetchone()

    return singleton_response({"liked": liked, "likes": row["likes"] or []})


# ---------------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------------

@router.post("/add_comment")
def add_comment(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    for key in ("tweet_id", "content"):
        if key not in body:
            raise HTTPException(status_code=422, detail=f"Missing expected key {key}")
    try:
        tid = int(body["tweet_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid tweet_id")

    try:
        with db.conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO comments (tweet_id, author_handle, content)
                VALUES (%s, %s, %s)
                RETURNING id, created_at
                """,
                (tid, current_user.get("handle", ""), body["content"]),
            )
            row = cur.fetchone()
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(status_code=404, detail="Tweet not found")

    return singleton_response({
        "success": True,
        "comment": {
            "username": current_user.get("handle", ""),
            "content": body["content"],
            "created_at": row["created_at"].isoformat(),
        },
    })


# ---------------------------------------------------------------------------
# create_channel
# ---------------------------------------------------------------------------

@router.post("/create_channel")
def create_channel(
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    if "name" not in body:
        raise HTTPException(status_code=422, detail="Missing expected key name")
    description = body.get("description", "") or ""

    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO channels (name, description) VALUES (%s, %s) RETURNING id",
            (body["name"], description),
        )
        channel_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO channel_members (user_id, channel_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (current_user["id"], channel_id),
        )
    return singleton_response({"id": channel_id, "name": body["name"]})


# ---------------------------------------------------------------------------
# load_own_tweets — THE benchmarked endpoint. Locked response schema:
#   {"data": {"result": [...tweets], "reports": [{"tweets": [...],
#             "server_timing": {"ms_fetch": float, "ms_build": float,
#                               "server_total": float}}]}}
# Fair-timing spec §3: ms_fetch = json_agg SQL round-trip; ms_build = 0.0
# (PG builds the JSON payload in-SQL, inside ms_fetch — NOT "for free");
# server_total = handler entry → return.
# ---------------------------------------------------------------------------

@router.post("/load_own_tweets")
def load_own_tweets(body: dict = Body(default={}),
                    current_user: dict = Depends(get_current_user)):
    # Type-selectivity reconciliation (spec §4): NO like_count predicate — return
    # ALL of the caller's own tweets. `threshold` is the filter-pushdown seam
    # (spec §10): default off; pass {"threshold": k} for the future FP sweep (#21),
    # where it binds `like_count > %s` and pushes down to the covering index.
    t_entry = time.perf_counter()
    threshold = body.get("threshold")
    predicate = ""
    params = [current_user["id"]]
    if threshold is not None:
        predicate = " AND t.like_count > %s"
        params.append(threshold)
    # No tweet-level ORDER BY: jac/neo4j return unordered and the Phase-5 oracle is a
    # multiset compare, so a tweet sort here would be asymmetric work vs the graph
    # backends (review fix). Comment-level ORDER BY kept — per-tweet, negligible.
    sql = f"""
        SELECT COALESCE(json_agg(
            json_build_object(
                'id', t.id,
                'content', t.content,
                'author_username', u.username,
                'created_at', t.created_at,
                'like_count', t.like_count,
                'likes', COALESCE((
                    SELECT json_agg(u2.username)
                    FROM likes l JOIN users u2 ON u2.id = l.user_id
                    WHERE l.tweet_id = t.id
                ), '[]'::json),
                'comments', COALESCE((
                    SELECT json_agg(json_build_object(
                        'username', c.author_handle,
                        'content', c.content,
                        'created_at', c.created_at
                    ) ORDER BY c.created_at)
                    FROM comments c WHERE c.tweet_id = t.id
                ), '[]'::json)
            )
        ), '[]'::json) AS tweets
        FROM tweets t
        JOIN users u ON u.id = t.author_id
        WHERE t.author_id = %s{predicate}
    """
    t0 = time.perf_counter()
    with db.conn() as c, c.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    ms_fetch = (time.perf_counter() - t0) * 1000  # json_agg round-trip (builds payload in-SQL)
    tweets = row["tweets"] or []
    report = {
        "tweets": tweets,
        "server_timing": {
            "ms_fetch": round(ms_fetch, 4),
            "ms_build": 0.0,  # PG builds the payload in-SQL, inside ms_fetch
            "server_total": round((time.perf_counter() - t_entry) * 1000, 4),
        },
    }
    return build_response([report], result=tweets)


# ---------------------------------------------------------------------------
# import_data — PUBLIC (no auth required)
# ---------------------------------------------------------------------------

@router.post("/import_data")
def import_data(body: dict = Body(default={})):
    with db.conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, username FROM users")
        all_users = cur.fetchall()
        all_user_ids = [u["id"] for u in all_users]
        username_to_id = {u["username"]: u["id"] for u in all_users}

        for username, payload in body.get("data", {}).items():
            user_id = username_to_id.get(payload.get("email") or username)
            if user_id is None:
                continue
            tweets = payload.get("tweets", [])
            for tweet in tweets:
                tweet_id = cur.execute(
                    "INSERT INTO tweets (author_id, content, created_at) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (user_id, tweet["content"],
                     datetime.fromisoformat(tweet["timestamp"])),
                ).fetchone()["id"]
                if tweet["likes"] > 0:
                    likes = [
                        (tweet_id, all_user_ids[idx])
                        for idx in range(0, min(tweet["likes"], len(all_user_ids)))
                    ]
                    cur.executemany(
                        "INSERT INTO likes (tweet_id, user_id) VALUES (%s, %s)",
                        likes,
                    )
            for followee_id in payload.get("following", []):
                cur.execute(
                    "INSERT INTO follows (follower_id, followee_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, followee_id),
                )

        viewer = cur.execute(
            "SELECT id FROM users WHERE handle = %s", ["Viewer"]
        ).fetchone()
        if viewer:
            viewer_id = viewer["id"]
            viewer_follows = [
                (viewer_id, uid) for uid in all_user_ids if uid != viewer_id
            ]
            cur.executemany(
                "INSERT INTO follows (follower_id, followee_id) VALUES (%s, %s)",
                viewer_follows,
            )

    return singleton_response({"success": True})


# ---------------------------------------------------------------------------
# seed_tweets — PUBLIC (no auth). Single-hop benchmark seeding (seed-design-spec).
# Body: {author_username, likers:[name...],
#        tweets:[{content, created_at, like_count, likers:[name...],
#                 comments:[{author, content, created_at}]}]}
# Tweets are attached to author_username (upserted). Likers are upserted by
# username; like_count is stored verbatim and kept == len(likers) by the seed
# generator. One transaction per call.
# ---------------------------------------------------------------------------

def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _upsert_user(cur, username):
    return cur.execute(
        "INSERT INTO users (username, handle, password, created_at) "
        "VALUES (%s, %s, '', now()) "
        "ON CONFLICT (username) DO UPDATE SET handle = EXCLUDED.handle "
        "RETURNING id",
        (username, username),
    ).fetchone()["id"]


@router.post("/seed_tweets")
def seed_tweets(body: dict = Body(default={})):
    author_username = body["author_username"]
    tweets = body.get("tweets", [])
    liker_pool = body.get("likers", [])
    channels = body.get("channels", [])   # type-selectivity noise (spec §6.4)

    with db.conn() as c, c.cursor() as cur:
        author_id = _upsert_user(cur, author_username)
        liker_ids = {name: _upsert_user(cur, name) for name in liker_pool}

        # Channel noise: TPT keeps it in its own table, so load_own_tweets never
        # touches it — the optimized backend pre-separates the type (spec §4).
        for ch in channels:
            channel_id = cur.execute(
                "INSERT INTO channels (name) VALUES (%s) RETURNING id",
                (ch.get("name", ch.get("key", "")),),
            ).fetchone()["id"]
            cur.execute(
                "INSERT INTO channel_members (user_id, channel_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (author_id, channel_id),
            )

        for tw in tweets:
            tweet_id = cur.execute(
                "INSERT INTO tweets (author_id, content, like_count, created_at) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (author_id, tw["content"], tw["like_count"],
                 _parse_ts(tw["created_at"])),
            ).fetchone()["id"]

            likes = [(tweet_id, liker_ids[name])
                     for name in tw.get("likers", []) if name in liker_ids]
            if likes:
                cur.executemany(
                    "INSERT INTO likes (tweet_id, user_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    likes,
                )

            comments = [
                (tweet_id, com["author"], com["content"], _parse_ts(com["created_at"]))
                for com in tw.get("comments", [])
            ]
            if comments:
                cur.executemany(
                    "INSERT INTO comments (tweet_id, author_handle, content, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    comments,
                )

    return singleton_response({"success": True, "seeded": len(tweets),
                               "seeded_tweets": len(tweets),
                               "seeded_channels": len(channels)})


# ---------------------------------------------------------------------------
# clear_data — PUBLIC. Best-effort full wipe (harness --reset only).
# ---------------------------------------------------------------------------

@router.post("/clear_data")
def clear_data():
    with db.conn() as c, c.cursor() as cur:
        cur.execute(
            "TRUNCATE comments, likes, follows, channel_members, "
            "channels, tweets, users RESTART IDENTITY CASCADE"
        )
    return singleton_response({"success": True})
