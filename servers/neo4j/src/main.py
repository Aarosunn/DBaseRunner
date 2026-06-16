"""Neo4j HTTP wrapper — FastAPI pod that fronts Neo4j via bolt and serves
the same HTTP endpoints as the other LittleX baselines.

Rationale: comparing Python-bolt-driver wall-clock against Flask-psycopg
wall-clock is unfair — the Python bolt driver's object deserialization
cost dominates Neo4j's client-side timing. Putting FastAPI in-cluster
next to Neo4j means the bolt round-trip happens on loopback between
pods, the FastAPI pod deserializes once and emits JSON, and the bench
driver times a plain HTTP round-trip like the SQL backends.

Endpoints mirror /walker routes on the SQL backends so
bench_own_tweets_selectivity.py works unchanged with
--endpoint-prefix /walker --auth-scheme bearer-username.
"""

import json
import os
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Body
from pydantic import BaseModel
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable


NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "neo4j_password")

app = FastAPI()
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


@app.on_event("startup")
def ensure_constraints():
    # neo4j (JVM) reports its container Running before Bolt is actually
    # listening, so on a cold deploy this query races the DB and raises
    # ServiceUnavailable — which would fail FastAPI startup and crashloop the
    # pod. Retry until neo4j accepts connections (up to ~120s) so the app comes
    # up cleanly on the first scheduling instead of relying on restart luck.
    # neo4j-db cold-boot is the real bottleneck and can run past 2min on a
    # loaded single-node minikube, so wait up to ~280s (just under the harness's
    # 300s health gate) rather than crashing and leaning on restart recovery.
    last_err = None
    for _ in range(140):  # 140 * 2s = up to 280s
        try:
            with driver.session() as s:
                s.run(
                    "CREATE CONSTRAINT profile_jacid IF NOT EXISTS "
                    "FOR (p:Profile) REQUIRE p.jac_id IS UNIQUE"
                )
            return
        except ServiceUnavailable as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError("neo4j not reachable after 280s of startup retries") from last_err


def _bearer_username(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not Logged In")
    return auth[len("bearer "):].strip()


def _resp(payload, report=None):
    r = report if report is not None else payload
    return {"data": {"result": payload, "reports": [r]}}


class UserReg(BaseModel):
    username: str
    password: str = ""


class Profile(BaseModel):
    username: str = ""
    bio: str = ""


class CreateTweet(BaseModel):
    content: str


class CreateChannel(BaseModel):
    name: str
    description: str = ""


@app.post("/user/register")
def register(u: UserReg):
    with driver.session() as s:
        s.run(
            "MERGE (p:Profile {jac_id: $uid}) "
            "ON CREATE SET p.username = $uid, p.handle = $uid, p.bio = ''",
            uid=u.username,
        )
    return _resp({"token": u.username})


@app.post("/user/login")
def login(u: UserReg):
    # Shape-compatible with the other baselines: hand the caller a token
    # that equals the username; bearer-username auth on subsequent calls.
    return _resp({"token": u.username})


@app.post("/walker/setup_profile")
@app.post("/function/setup_profile")
def setup_profile(body: Profile, request: Request):
    uid = _bearer_username(request)
    with driver.session() as s:
        s.run(
            "MATCH (p:Profile {jac_id: $uid}) "
            "SET p.handle = $handle, p.bio = $bio",
            uid=uid, handle=(body.username or uid), bio=body.bio,
        )
    return _resp({"success": True})


@app.post("/walker/create_tweet")
@app.post("/function/create_tweet")
def create_tweet(body: CreateTweet, request: Request):
    uid = _bearer_username(request)
    with driver.session() as s:
        s.run(
            """
            MATCH (p:Profile {jac_id: $uid})
            CREATE (p)-[:POST]->(:Tweet {
                jac_id: $uid + '_t_' + toString(timestamp()) + '_' +
                         toString(toInteger(rand()*1000000)),
                content: $content,
                author_username: p.username,
                created_at: toString(datetime()),
                likes: [],
                comments: []
            })
            """,
            uid=uid, content=body.content,
        )
    return _resp({"success": True})


@app.post("/walker/create_channel")
@app.post("/function/create_channel")
def create_channel(body: CreateChannel, request: Request):
    uid = _bearer_username(request)
    with driver.session() as s:
        s.run(
            """
            MATCH (p:Profile {jac_id: $uid})
            CREATE (p)-[:MEMBER]->(:Channel {
                jac_id: $uid + '_c_' + $name,
                name: $name,
                description: $description
            })
            """,
            uid=uid, name=body.name, description=body.description,
        )
    return _resp({"success": True})


@app.post("/walker/load_own_tweets")
@app.post("/function/load_own_tweets")
def load_own_tweets(request: Request, body: Optional[dict] = Body(default=None)):
    # server_total = handler entry → return (includes in-handler _bearer_username
    # auth resolve → lands in the residual, fair-timing spec §3).
    # Reconciliation spec §4: NO like_count predicate — the :POST edge-type already
    # pre-separates the :MEMBER->:Channel noise, so this returns ALL own tweets.
    # `threshold` is the filter-pushdown seam (spec §10): default off; pass
    # {"threshold": k} for the future FP sweep (#21) → binds into a Cypher WHERE.
    t_entry = time.perf_counter()
    uid = _bearer_username(request)
    threshold = (body or {}).get("threshold")
    predicate = "WHERE t.like_count > $threshold " if threshold is not None else ""
    cypher = (
        "MATCH (p:Profile {jac_id: $uid})-[:POST]->(t:Tweet) "
        + predicate +
        "RETURN t.jac_id AS id, t.content AS content, "
        "       t.author_username AS author_username, "
        "       t.created_at AS created_at, t.like_count AS like_count, "
        "       t.likes AS likes, t.comments AS comments"
    )
    params = {"uid": uid}
    if threshold is not None:
        params["threshold"] = threshold
    t0 = time.perf_counter()
    with driver.session() as s:
        rows = s.run(cypher, **params).data()
    ms_fetch = (time.perf_counter() - t0) * 1000.0  # cypher run + .data()

    t1 = time.perf_counter()
    tweets = [
        {
            "id": r["id"],
            "content": r["content"],
            "author_username": r["author_username"],
            "created_at": r["created_at"],
            "like_count": r["like_count"] if r["like_count"] is not None else len(r["likes"] or []),
            "likes": r["likes"] or [],
            "comments": [
                json.loads(c) if isinstance(c, str) else c
                for c in (r["comments"] or [])
            ],
        }
        for r in rows
    ]
    ms_build = (time.perf_counter() - t1) * 1000.0  # list-comprehension build

    report = {
        "tweets": tweets,
        "server_timing": {
            "ms_fetch": round(ms_fetch, 4),
            "ms_build": round(ms_build, 4),
            "server_total": round((time.perf_counter() - t_entry) * 1000.0, 4),
        },
    }
    return {"data": {"result": tweets, "reports": [report]}}


@app.post("/walker/clear_data")
@app.post("/function/clear_data")
def clear_data():
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    return _resp({"success": True, "message": "Database reset"})


# ---------------------------------------------------------------------------
# seed_tweets — PUBLIC. Single-hop benchmark seeding (seed-design-spec).
# Body: {author_username, likers:[name...],
#        tweets:[{content, created_at, like_count, likers:[name...],
#                 comments:[{author, content, created_at}]}]}
# Likers are stored idiomatically as a name array on the Tweet (no nodes);
# comments are stored as JSON strings (load_own_tweets parses them back).
# Batch via UNWIND, not per-tweet round-trips.
# ---------------------------------------------------------------------------

@app.post("/walker/seed_tweets")
@app.post("/function/seed_tweets")
def seed_tweets(body: Optional[dict] = Body(default=None)):
    if body is None:
        raise HTTPException(status_code=422, detail="Expected JSON body")
    author = body["author_username"]
    tweets_param = [
        {
            "idx": i,
            "content": tw["content"],
            "created_at": tw["created_at"],
            "like_count": tw["like_count"],
            "likes": tw.get("likers", []),
            "comments": [json.dumps(c) for c in tw.get("comments", [])],
        }
        for i, tw in enumerate(body.get("tweets", []))
    ]
    # Channel noise for the type-selectivity neighborhood (spec §6.4). The :MEMBER
    # edge-type keeps it off the :POST path, so load_own_tweets never traverses it
    # (the optimized backend pre-separates the type via edge-type).
    channels = body.get("channels", [])
    with driver.session() as s:
        s.run(
            """
            MERGE (p:Profile {jac_id: $author})
              ON CREATE SET p.username = $author, p.handle = $author, p.bio = ''
            WITH p
            UNWIND $tweets AS tw
            CREATE (p)-[:POST]->(:Tweet {
                jac_id: $author + '_t_' + toString(tw.idx),
                content: tw.content,
                author_username: p.username,
                created_at: tw.created_at,
                like_count: tw.like_count,
                likes: tw.likes,
                comments: tw.comments
            })
            """,
            author=author,
            tweets=tweets_param,
        )
        if channels:
            s.run(
                """
                MERGE (p:Profile {jac_id: $author})
                  ON CREATE SET p.username = $author, p.handle = $author, p.bio = ''
                WITH p
                UNWIND $channels AS ch
                CREATE (p)-[:MEMBER]->(:Channel {
                    jac_id: $author + '_c_' + ch.key,
                    name: ch.name
                })
                """,
                author=author,
                channels=channels,
            )
    return _resp({"success": True, "seeded": len(tweets_param),
                  "seeded_tweets": len(tweets_param),
                  "seeded_channels": len(channels)})


# ---------------------------------------------------------------------------
# Pydantic models for new endpoints
# ---------------------------------------------------------------------------

class LikeTweet(BaseModel):
    tweet_id: str


class AddComment(BaseModel):
    tweet_id: str
    content: str


class FollowUser(BaseModel):
    target_id: str


# ---------------------------------------------------------------------------
# like_tweet — toggle: appends username to t.likes if absent, removes if present
# ---------------------------------------------------------------------------

@app.post("/walker/like_tweet")
@app.post("/function/like_tweet")
def like_tweet(body: LikeTweet, request: Request):
    uid = _bearer_username(request)
    with driver.session() as s:
        result = s.run(
            """
            MATCH (t:Tweet {jac_id: $tweet_id})
            WITH t, $uid IN t.likes AS already_liked
            SET t.likes = CASE
                WHEN already_liked THEN [x IN t.likes WHERE x <> $uid]
                ELSE t.likes + [$uid]
            END
            SET t.like_count = size(t.likes)
            RETURN t.likes AS likes, NOT already_liked AS liked
            """,
            tweet_id=body.tweet_id,
            uid=uid,
        ).single()
    if result is None:
        raise HTTPException(status_code=404, detail="Tweet not found")
    return _resp({"liked": result["liked"], "likes": result["likes"]})


# ---------------------------------------------------------------------------
# add_comment — appends {username, content, created_at} to t.comments
# ---------------------------------------------------------------------------

@app.post("/walker/add_comment")
@app.post("/function/add_comment")
def add_comment(body: AddComment, request: Request):
    uid = _bearer_username(request)
    comment = {
        "username": uid,
        "content": body.content,
        "created_at": datetime.utcnow().isoformat(),
    }
    with driver.session() as s:
        result = s.run(
            """
            MATCH (t:Tweet {jac_id: $tweet_id})
            SET t.comments = t.comments + [$comment]
            RETURN t
            """,
            tweet_id=body.tweet_id,
            comment=json.dumps(comment),
        ).single()
    if result is None:
        raise HTTPException(status_code=404, detail="Tweet not found")
    return _resp({"success": True, "comment": comment})


# ---------------------------------------------------------------------------
# follow_user — MERGE Follow edge between two Profile nodes
# ---------------------------------------------------------------------------

@app.post("/walker/follow_user")
@app.post("/function/follow_user")
def follow_user(body: FollowUser, request: Request):
    uid = _bearer_username(request)
    with driver.session() as s:
        s.run(
            """
            MATCH (a:Profile {jac_id: $follower}), (b:Profile {jac_id: $target})
            MERGE (a)-[:Follow]->(b)
            """,
            follower=uid,
            target=body.target_id,
        )
    return _resp({"success": True})


# ---------------------------------------------------------------------------
# import_data — bulk seed: tweets (with likes array), follows, Viewer follows all
#
# follows: dataset uses integer 1-based indices into the all-profiles list
# (same convention as Postgres import_data). Profiles are sorted by jac_id
# to produce a stable ordering.
# ---------------------------------------------------------------------------

@app.post("/walker/import_data")
@app.post("/function/import_data")
def import_data(body: Optional[dict] = Body(default=None)):
    if body is None:
        raise HTTPException(status_code=422, detail="Expected JSON body")
    with driver.session() as s:
        rows = s.run(
            "MATCH (p:Profile) RETURN p.jac_id AS jac_id ORDER BY p.jac_id"
        ).data()
        all_jac_ids = [r["jac_id"] for r in rows]
        jac_id_by_idx = {i + 1: jid for i, jid in enumerate(all_jac_ids)}

        for username, payload in body.get("data", {}).items():
            user_jac_id = payload.get("email") or username
            for tweet in payload.get("tweets", []):
                n_likes = tweet.get("likes", 0)
                likes = all_jac_ids[: min(n_likes, len(all_jac_ids))]
                s.run(
                    """
                    MATCH (p:Profile {jac_id: $uid})
                    CREATE (p)-[:POST]->(:Tweet {
                        jac_id: $tweet_id,
                        content: $content,
                        author_username: p.username,
                        created_at: $created_at,
                        likes: $likes,
                        comments: []
                    })
                    """,
                    uid=user_jac_id,
                    tweet_id=f"{user_jac_id}_t_{uuid.uuid4().hex[:8]}",
                    content=tweet.get("content", ""),
                    created_at=tweet.get("timestamp", ""),
                    likes=likes,
                )
            for followee_idx in payload.get("following", []):
                target_jac_id = jac_id_by_idx.get(followee_idx)
                if target_jac_id:
                    s.run(
                        """
                        MATCH (a:Profile {jac_id: $a}), (b:Profile {jac_id: $b})
                        MERGE (a)-[:Follow]->(b)
                        """,
                        a=user_jac_id,
                        b=target_jac_id,
                    )
        # Viewer follows everyone — mirrors Postgres import_data behaviour
        s.run(
            """
            MATCH (v:Profile {handle: 'Viewer'}), (p:Profile)
            WHERE p.jac_id <> v.jac_id
            MERGE (v)-[:Follow]->(p)
            """
        )
    return _resp({"success": True})


@app.get("/health")
def health():
    return {"status": "ok"}
