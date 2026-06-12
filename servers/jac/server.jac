"""Little X - Social graph backend with TF-IDF feed ranking."""

import datetime;

# commenting this out temporarily to avoid the scikit-learn dependency
'''import from sklearn.feature_extraction.text { TfidfVectorizer }
import from sklearn.metrics.pairwise { cosine_similarity }

glob _vectorizer: Any = TfidfVectorizer();

def _compute_sim(query: str, content: str) -> float {
    if not query.strip() {
        return 1.0;
    }
    try {
        t = _vectorizer.fit_transform([query, content]);
        return float(cosine_similarity(t[0:1], t[1:2])[0][0]);
    } except Exception {
        return 0.0;
    }
}'''

def _compute_sim(query: str, content: str) -> float {
    return 0.0;
}

def _now() -> str {
    return datetime.datetime.utcnow().isoformat() + "Z";
}

node Profile {
    has username: str = "";
    has bio: str = "";
    has created_at: str = "";
    # i'm unable to find how to filter based on the jac_id, so adding it as an attribute for now
    has jac_id: str = "";
}

node Tweet {
    has content: str = "";
    has author_username: str = "";
    has created_at: str = "";
    has comments: list = [];
    has jac_id: str = "";
}

node Channel {
    has name: str = "";
    has description: str = "";
    has creator_username: str = "";
    has created_at: str = "";
}

edge Follow {}
edge Like {}
edge Post {}
edge Member {}
edge ChannelPost {}

walker setup_profile {
    has username: str = "";
    has bio: str = "";

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            p = here ++> Profile(username=self.username, bio=self.bio, created_at=_now());
            grant(p[0], level=ConnectPerm);
            p[0].jac_id = jid(p[0]);
            report {"id": jid(p[0]), "username": p[0].username, "bio": p[0].bio, "created_at": p[0].created_at};
        } else {
            profile = profiles[0];
            if self.username {
                profile.username = self.username;
            }
            if self.bio {
                profile.bio = self.bio;
            }
            report {"id": jid(profile), "username": profile.username, "bio": profile.bio, "created_at": profile.created_at};
        }
    }
}

walker get_profile {
    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile found"};
            disengage;
        }
        p = profiles[0];
        my_id = jid(p);

        followers: list = [];
        following: list = [];

        follow_in = [p->:Follow:->Profile];
        for other in follow_in {
            following.append({"id": jid(other), "username": other.username});
        }
        follow_out = [p<-:Follow:<-];
        for other in follow_out {
            followers.append({"id": jid(other), "username": other.username});
        }

        tweets = [
            {
                "id": jid(t),
                "content": t.content,
                "author_username": t.author_username,
                "created_at": t.created_at,
                "likes": [u.username for u in [t<-:Like:<-Profile]],
                "comments": t.comments
            }
            for t in [p-->(?:Tweet)]
        ];
        tweets.sort(key=lambda t: t["created_at"], reverse=True);
        report {
            "id": my_id,
            "username": p.username,
            "bio": p.bio,
            "followers": followers,
            "following": following,
            "tweets": tweets
        };
    }
}

walker:pub get_all_profiles {
    can run with Root entry {
        results: list = [];
        for r in allroots() {
            prof = [r-->(?:Profile)];
            if prof {
                results.append({"id": jid(prof[0]), "username": prof[0].username, "bio": prof[0].bio});
            }
        }
        report results;
    }
}

walker follow_user {
    has target_id: str;

    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        if not my_profiles {
            report {"error": "No profile"};
            disengage;
        }
        target = [allroots()-->(?:Profile, jac_id == self.target_id)];
        if not target {
            report {"error": "User not found"};
            disengage;
        }
        my_profiles[0] +>:Follow():+>target[0];
        report {"success": True};
        disengage;
    }
}

walker unfollow_user {
    has target_id: str;

    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        if not my_profiles {
            report {"error": "No profile"};
            disengage;
        }
        my_profile = my_profiles[0];
        target_edge = [edge my_profile->:Follow:->Profile];
        if not target_edge {
            report {"error": "User not found"};
            disengage;
        }
        del target_edge[0];
        report {"success": True};
        disengage;
    }
}

walker create_tweet {
    has content: str;

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile"};
            disengage;
        }
        profile = profiles[0];
        tweet = profile +>:Post():+> Tweet(
            content=self.content,
            author_username=profile.username,
            created_at=_now(),
            comments=[]
        );
        grant(tweet[0], level=ConnectPerm);
        t = tweet[0];
        t.jac_id = jid(t);
        report {"id": jid(t), "content": t.content, "author_username": t.author_username, "created_at": t.created_at, "likes": [], "comments": t.comments};
    }
}

walker delete_tweet {
    has tweet_id: str;

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile"};
            disengage;
        }
        profile = profiles[0];
        tweet = [profile-->(?:Tweet, jac_id == self.tweet_id)];
        if tweet {
            del tweet[0];
            report {"success": True};
            disengage;
        }
        channel_tweet = [profile-->(?:Channel)-->(?:Tweet, jac_id == self.tweet_id)];
        if channel_tweet {
            del channel_tweet[0];
            report {"success": True};
            disengage;
        }
        report {"error": "Tweet not found"};
    }
}

def find_tweet(tweet_id: str) -> Tweet | None {
    tweet = [allroots()-->(?:Profile)-->(?:Tweet, jac_id == tweet_id)];
    if tweet {
        return tweet[0];
    }
    channel_tweet = [allroots()-->(?:Profile)-->(?:Channel)-->(?:Tweet)];
    if channel_tweet {
        return channel_tweet[0];
    }
    return None;
}

walker like_tweet {
    has tweet_id: str;

    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        if not my_profiles {
            report {"error": "No profile"};
            disengage;
        }
        me = my_profiles[0];
        username = me.username;
        existing_liked_tweet = [me->:Like:->(?:Tweet, jac_id == self.tweet_id)];
        if existing_liked_tweet {
            target = existing_liked_tweet[0];
            me del --> target;
            report {"liked": False, "likes":[u.username for u in [target<-:Like:<-Profile]]};
            disengage;
        }
        target = find_tweet(self.tweet_id);
        if target {
            me +>: Like :+> target;
            report {"liked": True, "likes":[u.username for u in [target<-:Like:<-Profile]]};
            disengage;
        }
        report {"error": "Tweet not found"};
    }
}

walker add_comment {
    has tweet_id: str;
    has content: str;

    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        if not my_profiles {
            report {"error": "No profile"};
            disengage;
        }
        username = my_profiles[0].username;
        tweet = find_tweet(self.tweet_id);
        if tweet {
            comment = {"username": username, "content": self.content, "created_at": _now()};
            tweet.comments.append(comment);
            report {"success": True, "comment": comment};
            disengage;
        }
        report {"error": "Tweet not found"};
    }
}

walker get_trending {
    can run with Root entry {
        tag_counts: dict = {};
        for r in allroots() {
            prof = [r-->(?:Profile)];
            if prof {
                for tweet in [prof[0]-->(?:Tweet)] {
                    for word in tweet.content.split() {
                        if word.startswith("#") and len(word) > 1 {
                            tag = word.lower().rstrip(".,!?;:");
                            if tag in tag_counts {
                                tag_counts[tag] += 1;
                            } else {
                                tag_counts[tag] = 1;
                            }
                        }
                    }
                }
            }
        }
        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True);
        report [{"tag": t[0], "count": t[1]} for t in sorted_tags[:8]];
    }
}

walker load_feed {
    has search_query: str = "";

    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        if not my_profiles {
            report [];
            disengage;
        }
        my_profile = my_profiles[0];
        all_tweets: list = [];

        # explicitly look for things WE posted, not just things we have a connection to (like likes)
        for tweet in [my_profile->:Post:->(?:Tweet)] {
            all_tweets.append({
                "id": jid(tweet),
                "content": tweet.content,
                "author_username": tweet.author_username,
                "created_at": tweet.created_at,
                "likes": [u.username for u in [tweet<-:Like:<-(?:Profile)]],
                "comments": tweet.comments,
                "is_mine": True
            });
        }

        for followed in [my_profile->:Follow:->(?:Profile)] {
            for tweet in [followed-->(?:Tweet)] {
                all_tweets.append({
                    "id": jid(tweet),
                    "content": tweet.content,
                    "author_username": tweet.author_username,
                    "created_at": tweet.created_at,
                    "likes": [u.username for u in [tweet<-:Like:<-(?:Profile)]],
                    "comments": tweet.comments,
                    "is_mine": False
                });
            }
        }

        if self.search_query {
            q = self.search_query.lower().strip();
            all_tweets = [t for t in all_tweets if q in t["content"].lower()];
            all_tweets.sort(key=lambda t: t["created_at"], reverse=True);
        } else {
            all_tweets.sort(key=lambda t: t["created_at"], reverse=True);
        }

        report all_tweets;
    }
}

walker create_channel {
    has name: str;
    has description: str = "";

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile"};
            disengage;
        }
        profile = profiles[0];
        channel_nodes = profile +>:Member():+> Channel(
            name=self.name,
            description=self.description,
            creator_username=profile.username,
            created_at=_now()
        );
        ch = channel_nodes[0];
        grant(ch, level=ConnectPerm);
        report {
            "id": jid(ch),
            "name": ch.name,
            "description": ch.description,
            "creator_username": ch.creator_username,
            "created_at": ch.created_at,
            "member_count": 1,
            "is_member": True
        };
    }
}

walker join_channel {
    has channel_id: str;

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile"};
            disengage;
        }
        profile = profiles[0];
        for r in allroots() {
            prof_list = [r-->(?:Profile)];
            if prof_list {
                for ch in [prof_list[0]-->(?:Channel)] {
                    if jid(ch) == self.channel_id {
                        already = [edge profile ->:Member:-> ch];
                        if not already {
                            profile +>:Member():+> ch;
                        }
                        report {"success": True};
                        disengage;
                    }
                }
            }
        }
        report {"error": "Channel not found"};
    }
}

walker leave_channel {
    has channel_id: str;

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile"};
            disengage;
        }
        profile = profiles[0];
        for ch in [profile-->(?:Channel)] {
            if jid(ch) == self.channel_id {
                member_edges = [edge profile ->:Member:-> ch];
                if member_edges {
                    del member_edges[0];
                }
                report {"success": True};
                disengage;
            }
        }
        report {"error": "Channel not found"};
    }
}

walker get_channels {
    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        my_profile = my_profiles[0] if my_profiles else None;
        my_channel_ids: list = [];
        if my_profile {
            my_channel_ids = [jid(ch) for ch in [my_profile-->(?:Channel)]];
        }

        seen: dict = {};
        channels: list = [];
        for r in allroots() {
            prof_list = [r-->(?:Profile)];
            if prof_list {
                for ch in [prof_list[0]-->(?:Channel)] {
                    ch_id = jid(ch);
                    if ch_id not in seen {
                        member_count = 0;
                        for r2 in allroots() {
                            p2 = [r2-->(?:Profile)];
                            if p2 {
                                edges_to_ch = [edge p2[0] ->:Member:-> ch];
                                if edges_to_ch {
                                    member_count += 1;
                                }
                            }
                        }
                        seen[ch_id] = True;
                        channels.append({
                            "id": ch_id,
                            "name": ch.name,
                            "description": ch.description,
                            "creator_username": ch.creator_username,
                            "created_at": ch.created_at,
                            "member_count": member_count,
                            "is_member": ch_id in my_channel_ids
                        });
                    }
                }
            }
        }
        channels.sort(key=lambda c: c["created_at"], reverse=True);
        report channels;
    }
}

walker get_channel_detail {
    has channel_id: str;

    can run with Root entry {
        my_profiles = [-->(?:Profile)];
        my_profile = my_profiles[0] if my_profiles else None;
        is_member = False;

        for r in allroots() {
            prof_list = [r-->(?:Profile)];
            if prof_list {
                for ch in [prof_list[0]-->(?:Channel)] {
                    if jid(ch) == self.channel_id {
                        member_count = 0;
                        for r2 in allroots() {
                            p2 = [r2-->(?:Profile)];
                            if p2 {
                                edges_to_ch = [edge p2[0] ->:Member:-> ch];
                                if edges_to_ch {
                                    member_count += 1;
                                }
                            }
                        }
                        if my_profile {
                            my_edges = [edge my_profile ->:Member:-> ch];
                            is_member = True if my_edges else False;
                        }
                        posts: list = [];
                        for tweet in [ch-->(?:Tweet)] {
                            posts.append({
                                "id": jid(tweet),
                                "content": tweet.content,
                                "author_username": tweet.author_username,
                                "created_at": tweet.created_at,
                                "likes": [u.username for u in [tweet<-:Like:<-Profile]],
                                "comments": tweet.comments
                            });
                        }
                        posts.sort(key=lambda p: p["created_at"], reverse=True);
                        report {
                            "id": jid(ch),
                            "name": ch.name,
                            "description": ch.description,
                            "creator_username": ch.creator_username,
                            "created_at": ch.created_at,
                            "member_count": member_count,
                            "is_member": is_member,
                            "posts": posts
                        };
                        disengage;
                    }
                }
            }
        }
        report {"error": "Channel not found"};
    }
}

walker create_channel_tweet {
    has channel_id: str;
    has content: str;

    can run with Root entry {
        profiles = [-->(?:Profile)];
        if not profiles {
            report {"error": "No profile"};
            disengage;
        }
        profile = profiles[0];
        for ch in [profile-->(?:Channel)] {
            if jid(ch) == self.channel_id { 
                tweet = ch +>:ChannelPost():+> Tweet(
                    content=self.content,
                    author_username=profile.username,
                    created_at=_now(),
                    comments=[]
                );
                grant(tweet[0], level=ConnectPerm);
                t = tweet[0];
                report {
                    "id": jid(t),
                    "content": t.content,
                    "author_username": t.author_username,
                    "created_at": t.created_at,
                    "likes": [],
                    "comments": t.comments
                };
                disengage;
            }
        }
        report {"error": "Not a member of this channel"};
    }
}

walker bench_feed {
    has clear_before: bool = False;
    has index_enabled: str = "";

    can run with Root entry {
        import os;
        import from jaclang { JacRuntimeInterface as Jac }
        import from time { perf_counter }

        # Set index flag via env var (empty = use config default)
        prev_env = os.environ.get("JAC_INDEX_ENABLED");
        if self.index_enabled {
            os.environ["JAC_INDEX_ENABLED"] = self.index_enabled;
        }

        ctx = Jac.get_context();
        l1_size_before = len(ctx.mem.__mem__);

        if self.clear_before {
            # Stub out all edges so source/target refs are released
            for (uid, anchor) in list(ctx.mem.__mem__.items()) {
                if hasattr(anchor, "edges") and anchor.is_populated() {
                    anchor.edges = [e.make_stub() for e in anchor.edges];
                }
            }
            ctx.mem.__mem__.clear();
            l2 = getattr(ctx.mem, "l2", None);
            if l2 is not None {
                l2.close();
            }
        }

        l1_size_after_clear = len(ctx.mem.__mem__);

        t0 = perf_counter();
        my_profiles = [-->(?:Profile)];
        t_profile = perf_counter();

        if not my_profiles {
            # Restore env
            if prev_env is not None {
                os.environ["JAC_INDEX_ENABLED"] = prev_env;
            } elif self.index_enabled {
                del os.environ["JAC_INDEX_ENABLED"];
            }
            report {"error": "No profile"};
            disengage;
        }
        my_profile = my_profiles[0];
        all_tweets: list = [];

        t_tweets_start = perf_counter();
        for tweet in [my_profile-->(?:Tweet)] {
            all_tweets.append(jid(tweet));
        }
        t_my_tweets = perf_counter();

        for followed in [my_profile->:Follow:->(?:Profile)] {
            for tweet in [followed-->(?:Tweet)] {
                all_tweets.append(jid(tweet));
            }
        }
        t_follow_tweets = perf_counter();

        l1_size_after = len(ctx.mem.__mem__);

        # Restore env
        if prev_env is not None {
            os.environ["JAC_INDEX_ENABLED"] = prev_env;
        } elif self.index_enabled {
            del os.environ["JAC_INDEX_ENABLED"];
        }

        report {
            "tweet_count": len(all_tweets),
            "index_enabled": self.index_enabled,
            "l1_before": l1_size_before,
            "l1_after_clear": l1_size_after_clear,
            "l1_after": l1_size_after,
            "ms_get_profile": round((t_profile - t0) * 1000, 3),
            "ms_my_tweets": round((t_my_tweets - t_tweets_start) * 1000, 3),
            "ms_follow_tweets": round((t_follow_tweets - t_my_tweets) * 1000, 3),
            "ms_total_traversal": round((t_follow_tweets - t0) * 1000, 3)
        };
    }
}

walker bench_single_hop {
    has clear_before: bool = False;
    has index_enabled: str = "";

    can run with Root entry {
        import os;
        import from jaclang { JacRuntimeInterface as Jac }
        import from time { perf_counter }

        prev_env = os.environ.get("JAC_INDEX_ENABLED");
        if self.index_enabled {
            os.environ["JAC_INDEX_ENABLED"] = self.index_enabled;
        }

        ctx = Jac.get_context();

        if self.clear_before {
            for (uid, anchor) in list(ctx.mem.__mem__.items()) {
                if hasattr(anchor, "edges") and anchor.is_populated() {
                    anchor.edges = [e.make_stub() for e in anchor.edges];
                }
            }
            ctx.mem.__mem__.clear();
            l2 = getattr(ctx.mem, "l2", None);
            if l2 is not None {
                l2.close();
            }
        }

        l1_after_clear = len(ctx.mem.__mem__);

        # Get profile first (always needed)
        my_profiles = [-->(?:Profile)];
        if not my_profiles {
            if prev_env is not None {
                os.environ["JAC_INDEX_ENABLED"] = prev_env;
            } elif self.index_enabled {
                del os.environ["JAC_INDEX_ENABLED"];
            }
            report {"error": "No profile"};
            disengage;
        }
        my_profile = my_profiles[0];

        # Single query: get ONLY tweets from profile
        # Profile has Follow edges (noise) + Post edges (targets)
        # INDEX ON should only load Tweet nodes
        # INDEX OFF must iterate ALL edges and load ALL neighbors
        t0 = perf_counter();
        tweets = [my_profile-->(?:Tweet)];
        t1 = perf_counter();

        l1_after = len(ctx.mem.__mem__);

        if prev_env is not None {
            os.environ["JAC_INDEX_ENABLED"] = prev_env;
        } elif self.index_enabled {
            del os.environ["JAC_INDEX_ENABLED"];
        }

        report {
            "tweet_count": len(tweets),
            "index_enabled": self.index_enabled,
            "l1_after_clear": l1_after_clear,
            "l1_after": l1_after,
            "ms_query": round((t1 - t0) * 1000, 3)
        };
    }
}

walker bench_pushdown {
    has operation: str = "global_profile";
    has target_id: str = "";
    has clear_before: bool = False;
    has index_enabled: str = "";

    can run with Root entry {
        import os;
        import from jaclang { JacRuntimeInterface as Jac }
        import from time { perf_counter }

        prev_env = os.environ.get("JAC_INDEX_ENABLED");
        if self.index_enabled {
            os.environ["JAC_INDEX_ENABLED"] = self.index_enabled;
        }

        ctx = Jac.get_context();
        l1_before = len(ctx.mem.__mem__);

        if self.clear_before {
            for (uid, anchor) in list(ctx.mem.__mem__.items()) {
                if hasattr(anchor, "edges") and anchor.is_populated() {
                    anchor.edges = [e.make_stub() for e in anchor.edges];
                }
            }
            ctx.mem.__mem__.clear();
            l2 = getattr(ctx.mem, "l2", None);
            if l2 is not None {
                l2.close();
            }
        }

        l1_after_clear = len(ctx.mem.__mem__);

        if self.operation == "global_profile" {
            # [allroots()-->(?:Profile, jac_id == X)] — needle in haystack across all users
            t0 = perf_counter();
            result = [allroots()-->(?:Profile, jac_id == self.target_id)];
            t1 = perf_counter();
            found = len(result) > 0;
            result_count = len(result);
        } elif self.operation == "global_tweet" {
            # [allroots()-->(?:Profile)-->(?:Tweet, jac_id == X)] — 2-hop global lookup
            t0 = perf_counter();
            result = [allroots()-->(?:Profile)-->(?:Tweet, jac_id == self.target_id)];
            t1 = perf_counter();
            found = len(result) > 0;
            result_count = len(result);
        } elif self.operation == "local_tweet" {
            # [profile-->(?:Tweet, jac_id == X)] — targeted lookup within user's tweets
            my_profiles = [-->(?:Profile)];
            if not my_profiles {
                if prev_env is not None {
                    os.environ["JAC_INDEX_ENABLED"] = prev_env;
                } elif self.index_enabled {
                    del os.environ["JAC_INDEX_ENABLED"];
                }
                report {"error": "No profile"};
                disengage;
            }
            profile = my_profiles[0];
            t0 = perf_counter();
            result = [profile-->(?:Tweet, jac_id == self.target_id)];
            t1 = perf_counter();
            found = len(result) > 0;
            result_count = len(result);
        } elif self.operation == "load_feed_production" {
            # Full load_feed path — same as production walker, measures type filter pushdown
            t0 = perf_counter();
            my_profiles = [-->(?:Profile)];
            if not my_profiles {
                if prev_env is not None {
                    os.environ["JAC_INDEX_ENABLED"] = prev_env;
                } elif self.index_enabled {
                    del os.environ["JAC_INDEX_ENABLED"];
                }
                report {"error": "No profile"};
                disengage;
            }
            my_profile = my_profiles[0];
            all_tweets: list = [];
            for tweet in [my_profile-->(?:Tweet)] {
                all_tweets.append(jid(tweet));
            }
            for followed in [my_profile->:Follow:->(?:Profile)] {
                for tweet in [followed-->(?:Tweet)] {
                    all_tweets.append(jid(tweet));
                }
            }
            t1 = perf_counter();
            found = True;
            result_count = len(all_tweets);
        } else {
            if prev_env is not None {
                os.environ["JAC_INDEX_ENABLED"] = prev_env;
            } elif self.index_enabled {
                del os.environ["JAC_INDEX_ENABLED"];
            }
            report {"error": "Unknown operation: " + self.operation};
            disengage;
        }

        l1_after = len(ctx.mem.__mem__);

        if prev_env is not None {
            os.environ["JAC_INDEX_ENABLED"] = prev_env;
        } elif self.index_enabled {
            del os.environ["JAC_INDEX_ENABLED"];
        }

        report {
            "operation": self.operation,
            "index_enabled": self.index_enabled,
            "found": found,
            "result_count": result_count,
            "l1_before": l1_before,
            "l1_after_clear": l1_after_clear,
            "l1_after": l1_after,
            "ms_query": round((t1 - t0) * 1000, 3)
        };
    }
}

walker:pub clear_cache {
    can run with Root entry {
        import from jaclang { JacRuntimeInterface as Jac }
        ctx = Jac.get_context();
        # Convert all populated edges to stubs so that source/target
        # references no longer keep node anchors alive in memory.
        # This mirrors what graph-query-bench does before cold iterations.
        stubbed = 0;
        for (uid, anchor) in list(ctx.mem.__mem__.items()) {
            if hasattr(anchor, "edges") and anchor.is_populated() {
                anchor.edges = [e.make_stub() for e in anchor.edges];
                stubbed += 1;
            }
        }
        # Clear L1 in-memory anchor cache
        ctx.mem.__mem__.clear();
        # Clear L2 cache (Redis in scale, LocalCacheMemory otherwise)
        l2 = getattr(ctx.mem, "l2", None);
        if l2 is not None {
            l2.close();
        }
        report {
            "success": True,
            "stubbed_nodes": stubbed,
            "message": "Edges stubbed, L1 and L2 caches cleared"
        };
    }
}

walker:pub import_data {
    has data: dict[dict[str, str, str, int, str, bool, list[dict[str, str, int, str]], list[str], str]];

    can run with Root entry {
        print(f'There are {len(self.data.keys())} profiles');
        usernames = list(self.data.keys());
        all_profiles = [];
        # generate list of all profiles to use for generating likes
        for r in allroots() {
            prof = [r-->(?:Profile)];
            if not prof {
                continue;
            }
            all_profiles += prof;
        }
        for r in allroots() {
            prof = [r-->(?:Profile)];
            if not prof {
                continue;
            }
            username = prof[0].username;

            if username not in self.data and username != 'Viewer' {
                continue;
            }

            for s in allroots() {
                target_list = [s-->(?:Profile)];
                # override for the viewer user. they follow everyone
                if target_list and (username == 'Viewer' or jid(target_list[0]) in self.data[username]['following']) {
                    # don't let the viewer follow themself
                    if (username == 'Viewer' and jid(target_list[0]) == jid(prof[0])) {
                        continue;
                    }
                    prof[0] +>:Follow():+> target_list[0];
                }
            }

            # ignore users we aren't importing
            if username not in self.data {
                continue;
            }
            for tweet_data in self.data[username]['tweets'] {
                tweet = prof[0] +>:Post():+> Tweet(
                    content=tweet_data['content'],
                    author_username=username,
                    created_at=tweet_data['timestamp'],
                    comments=[]
                );
                tweet[0].jac_id = jid(tweet[0]);
                grant(tweet[0], level=ConnectPerm);

                # get the first $tweet_data.likes usernames for the likes. 
                for profile in all_profiles[:min(tweet_data['likes'], len(usernames))] {
                    profile +>: Like :+> tweet[0];
                }
            }
        }
    }
}