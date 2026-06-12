-- Hand-optimized PostgreSQL schema for LittleX.
--
-- This schema is NOT a port of the SQLAlchemy ORM models — it is a
-- ground-up redesign optimized for the LittleX read/write workload.
-- Key choices:
--
--   * Composite covering index `(author_id, created_at DESC) INCLUDE (content)`
--     so that `load_feed`'s "fetch this user's recent tweets" path is an
--     index-only scan — Postgres never has to visit the heap.
--
--   * Likes, follows, and comments live in their own normalized tables,
--     not as embedded arrays or ORM relationships. This is the standard
--     relational pattern and lets Postgres push every join through an
--     index.
--
--   * pg_trgm GIN index on `tweets.content` so the search benchmark's
--     `ILIKE '%query%'` patterns hit an index instead of a sequential
--     scan. Without this, search is O(N) regardless of selectivity.
--
--   * Indexes on BOTH directions of every association table. The PK
--     covers (leading column, trailing column) lookups; the explicit
--     index on the trailing column covers reverse lookups.
--
--   * Foreign keys with ON DELETE CASCADE so deleting a tweet
--     automatically removes its likes/comments without app-level loops.
--
-- This file is run once at startup by src/__init__.py via `db.bootstrap()`.
-- Every statement is idempotent (`IF NOT EXISTS`) so re-running is safe.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL    PRIMARY KEY,
    username    TEXT         NOT NULL UNIQUE,
    handle      TEXT         NOT NULL DEFAULT '',
    password    TEXT         NOT NULL,
    bio         TEXT         NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- handle is used by the Viewer special-case lookup in import_data.
CREATE INDEX IF NOT EXISTS idx_users_handle ON users (handle);

-- ---------------------------------------------------------------------
-- tweets
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tweets (
    id          BIGSERIAL    PRIMARY KEY,
    author_id   BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- THE critical index: composite covering index for "fetch this user's
-- tweets in created_at DESC order". With INCLUDE (content), Postgres can
-- answer the entire load_feed sub-query as an index-only scan and never
-- touch the heap.
CREATE INDEX IF NOT EXISTS idx_tweets_author_created
    ON tweets (author_id, created_at DESC) INCLUDE (content);

-- pg_trgm GIN for substring search. Postgres' LIKE/ILIKE planner uses
-- gin_trgm_ops for `%query%` patterns, so the search bench drops from a
-- sequential scan to an index scan.
CREATE INDEX IF NOT EXISTS idx_tweets_content_trgm
    ON tweets USING gin (content gin_trgm_ops);

-- ---------------------------------------------------------------------
-- likes
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS likes (
    tweet_id    BIGINT       NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    user_id     BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tweet_id, user_id)
);

-- Reverse direction: "what tweets does this user like" needs an index on
-- the trailing column of the composite PK.
CREATE INDEX IF NOT EXISTS idx_likes_user ON likes (user_id);

-- ---------------------------------------------------------------------
-- follows
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS follows (
    follower_id  BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    followee_id  BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_id, followee_id)
);

-- "Who follows this user" — reverse lookup on the trailing PK column.
CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows (followee_id);

-- ---------------------------------------------------------------------
-- comments
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comments (
    id             BIGSERIAL    PRIMARY KEY,
    tweet_id       BIGINT       NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    author_handle  TEXT         NOT NULL,
    content        TEXT         NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_comments_tweet_id ON comments (tweet_id);

-- ---------------------------------------------------------------------
-- channels + channel_members
--
-- Present purely so the own-tweets selectivity sweep can create
-- "noise" fan-out edges (channel memberships) alongside a user's real
-- tweets. load_own_tweets never queries these tables — the selectivity
-- story on a relational backend is the same regardless of how many
-- channels a user belongs to, since tweets live in their own table.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channels (
    id          BIGSERIAL    PRIMARY KEY,
    name        TEXT         NOT NULL,
    description TEXT         NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS channel_members (
    user_id     BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id  BIGINT       NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_members_channel ON channel_members (channel_id);
