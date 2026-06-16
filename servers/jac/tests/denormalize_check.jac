# Runnable TDD harness for the likers-denormalize fix (jac-likers-n1-denormalize).
# Run against a FRESH store (jac persists to .jac/data across runs):
#     rm -rf .jac/data && jac run tests/denormalize_check.jac
# from servers/jac/ (so `import from server` resolves). Exits non-zero on any failed
# assert (RED before the fix: Tweet has no `likes` field; GREEN after).
#   1. seed_tweets denormalizes the per-tweet likers onto tweet.likes
#   2. like_tweet keeps tweet.likes in sync (append on like, remove on unlike)
#   3. load_own_tweets reads tweet.likes (NOT a reverse Like-edge walk): a name
#      placed in the field with NO Like edge still appears in the load output.

import from server { seed_tweets, load_own_tweets, like_tweet, Profile, Tweet }

with entry {
    # ── 1. seed denormalizes likers onto the field ──────────────────────────
    root spawn seed_tweets(
        likers=["a", "b"],
        tweets=[{"content": "[t_0000] hi", "created_at": "t",
                 "like_count": 2, "likers": ["a", "b"], "comments": []}],
        channels=[]
    );
    prof = [root --> (?:Profile)][0];
    tw = [t for t in [prof --> (?:Tweet)] if t.content == "[t_0000] hi"][0];
    assert tw.likes == ["a", "b"] , "seed must denormalize likers onto tweet.likes";

    # ── 2. like_tweet syncs the field (append on like, remove on unlike) ─────
    root spawn seed_tweets(
        likers=[],
        tweets=[{"content": "[t_0001] fresh", "created_at": "t",
                 "like_count": 0, "likers": [], "comments": []}],
        channels=[]
    );
    fresh = [t for t in [prof --> (?:Tweet)] if t.content == "[t_0001] fresh"][0];
    assert fresh.likes == [] , "fresh tweet starts with empty likes";
    root spawn like_tweet(tweet_id=fresh.jac_id);
    assert "eval" in fresh.likes , "like_tweet must append the liker to tweet.likes";
    root spawn like_tweet(tweet_id=fresh.jac_id);
    assert "eval" not in fresh.likes , "unlike must remove the liker from tweet.likes";

    # ── 3. load reads the FIELD, not the Like edges (N+1 gone) ───────────────
    # Diverge: put a marker in the field with NO corresponding Like edge.
    tw.likes.append("ghost_no_edge");
    print("LOAD_OUTPUT_START");
    root spawn load_own_tweets();
    print("LOAD_OUTPUT_END");
    print("ALL_FIELD_ASSERTS_PASSED");
}
