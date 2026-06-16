"""Jac FULLSTACK backend adapter (served by jac-cloud as POST /walker/<name>)."""

import requests

from .base import BackendBase, seed_tweets_payload, extract_seeded_counts


class JacBackend(BackendBase):
    # Jac creates one node + edge per item inside a SINGLE walker call, so a large
    # neighborhood (fixed-target @10% = 1000 tweets + 9000 channels) is split into
    # chunks of at most this many (tweets+channels) items to stay under the
    # jac-cloud request timeout (reconciliation spec §15). The seed_tweets walker
    # is idempotent on the eval Profile, so multiple calls accumulate safely.
    SEED_CHUNK_SIZE = 2500

    # jac-cloud /user/register and /user/login both require {username, password}
    # (verified via inspect_schema.py against the running server). The bench
    # username (bench_<run>_<sweep>_<param>) goes through as-is; no email field.
    def _register_body(self, username: str, password: str) -> dict:
        return {"username": username, "password": password}

    def _parse_token(self, body: dict) -> str:
        # jac-cloud login: {"ok": true, "data": {"username", "token", "root_id"}}
        return body["data"]["token"]

    def _extract_tweets(self, body: dict) -> list:
        # jac-cloud envelope: {"ok":true, "data":{"result":<walker meta dict>,
        #   "reports":[{"tweets":[...]} | {"error":"No profile"}]}}.
        # The tweets live in the first WALKER REPORT, not in data.result (which
        # is walker metadata). Error/"No profile" reports have no "tweets" -> [].
        reports = (body.get("data") or {}).get("reports") or body.get("reports") or []
        if reports and isinstance(reports[0], dict):
            return reports[0].get("tweets") or []
        return []

    def _post_seed(self, body: dict) -> dict:
        resp = self.session.post(f"{self.base_url}/walker/seed_tweets", json=body)
        resp.raise_for_status()
        return extract_seeded_counts(resp.json())

    def seed(self, spec: dict) -> dict:
        # Identity comes from the JWT on the session — no author_username.
        body = seed_tweets_payload(spec)
        tweets, channels, likers = body["tweets"], body["channels"], body["likers"]
        if len(tweets) + len(channels) <= self.SEED_CHUNK_SIZE:
            return self._post_seed(body)

        # Chunk tweets first, then channels, each call <= SEED_CHUNK_SIZE items
        # (spec §15). The full liker pool rides every chunk that carries tweets so
        # their Like edges resolve; channel-only chunks need no likers.
        tw, ch = list(tweets), list(channels)
        seeded_tweets = seeded_channels = 0
        while tw or ch:
            tw_batch, tw = tw[:self.SEED_CHUNK_SIZE], tw[self.SEED_CHUNK_SIZE:]
            room = self.SEED_CHUNK_SIZE - len(tw_batch)
            ch_batch, ch = ch[:room], ch[room:]
            counts = self._post_seed({
                "likers": likers if tw_batch else [],
                "tweets": tw_batch,
                "channels": ch_batch,
            })
            seeded_tweets += counts.get("seeded_tweets") or len(tw_batch)
            seeded_channels += counts.get("seeded_channels") or len(ch_batch)
        return {"seeded_tweets": seeded_tweets, "seeded_channels": seeded_channels}

    def health(self) -> bool:
        # jac-cloud exposes walker:pub health as POST /walker/health, not GET.
        try:
            resp = self.session.post(
                f"{self.base_url}/walker/health", json={}, timeout=5
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def reset(self) -> None:
        # Jac has no data-delete endpoint; namespacing is the correctness mechanism
        # (harness-fix-spec §1.2). Logged no-op — never raise.
        print(
            "  [jac] reset(): no server-side data wipe; relying on eval-user namespacing"
        )

    def clear_cache(self) -> None:
        resp = self.session.post(f"{self.base_url}/walker/clear_cache", json={})
        resp.raise_for_status()
