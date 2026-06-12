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

import os
import time

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from neo4j import GraphDatabase


NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "neo4j_password")

app = FastAPI()
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


@app.on_event("startup")
def ensure_constraints():
    with driver.session() as s:
        s.run(
            "CREATE CONSTRAINT profile_jacid IF NOT EXISTS "
            "FOR (p:Profile) REQUIRE p.jac_id IS UNIQUE"
        )


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
def load_own_tweets(request: Request):
    uid = _bearer_username(request)
    cypher = (
        "MATCH (p:Profile {jac_id: $uid})-[:POST]->(t:Tweet) "
        "RETURN t.jac_id AS id, t.content AS content, "
        "       t.author_username AS author_username, "
        "       t.created_at AS created_at, "
        "       t.likes AS likes, t.comments AS comments"
    )
    t0 = time.perf_counter()
    with driver.session() as s:
        rows = s.run(cypher, uid=uid).data()
    ms_traversal = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    tweets = [
        {
            "id": r["id"],
            "content": r["content"],
            "author_username": r["author_username"],
            "created_at": r["created_at"],
            "likes": r["likes"] or [],
            "comments": r["comments"] or [],
        }
        for r in rows
    ]
    ms_build = (time.perf_counter() - t1) * 1000.0

    report = {
        "tweets": tweets,
        "ms_traversal": round(ms_traversal, 4),
        "ms_build_payload": round(ms_build, 4),
    }
    return {"data": {"result": tweets, "reports": [report]}}


@app.post("/walker/clear_data")
@app.post("/function/clear_data")
def clear_data():
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    return _resp({"success": True, "message": "Database reset"})


@app.get("/health")
def health():
    return {"status": "ok"}
