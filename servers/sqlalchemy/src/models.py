from src import db
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Table, Column, ForeignKey, Index
from typing import List, Optional, Set
from datetime import datetime

# association tables use SqlAlchemy CORE tables, not the ORM version
# https://docs.sqlalchemy.org/en/21/orm/basic_relationships.html#many-to-many
# self referential docs
# https://docs.sqlalchemy.org/en/21/orm/join_conditions.html#self-referential-many-to-many-relationship
#
# NOTE on indexing: a composite PK in Postgres only indexes lookups on the
# *leading* column. The PK on (followee_id, follower_id) handles "who follows
# user X" queries efficiently, but NOT "who does X follow". We add an explicit
# index on the trailing column so both directions are O(log n). Same idea for
# `likes` — the (tweet_id, user_id) PK lets us check membership and find
# likers of a tweet, but we need a separate index on user_id to answer
# "what tweets does this user like".
following_table = Table(
    "following",
    db.Model.metadata,
    Column('followee_id', ForeignKey('user.id'), primary_key=True),
    Column('follower_id', ForeignKey('user.id'), primary_key=True),
    Index('idx_following_follower', 'follower_id'),
)

like_table = Table(
    "likes",
    db.Model.metadata,
    Column('tweet_id', ForeignKey('tweet.id'), primary_key=True),
    Column('user_id', ForeignKey('user.id'), primary_key=True),
    Index('idx_likes_user', 'user_id'),
)

class User(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    # public facing
    handle: Mapped[str]
    # login username — UNIQUE because the app semantically requires it
    # (registration is keyed off username, login looks it up by it). The
    # unique constraint also gives us a btree index for free, which a
    # competent ORM developer would expect.
    username: Mapped[str] = mapped_column(unique=True, index=True)
    # plaintest password okay since this is just a benchmarking baseline
    password: Mapped[str]
    bio: Mapped[Optional[str]]
    created_at: Mapped[datetime]
    following: Mapped[Set['User']] = relationship(
        'User',
        secondary=following_table,
        primaryjoin=id == following_table.c.follower_id,
        secondaryjoin=id == following_table.c.followee_id,
        back_populates="followers"
    )
    followers: Mapped[Set['User']] =  relationship(
        'User',
        secondary=following_table,
        primaryjoin=id == following_table.c.followee_id,
        secondaryjoin=id == following_table.c.follower_id,
        back_populates="following"
    )
    tweets: Mapped[List['Tweet']] = relationship(back_populates='author')
    
    # include_relationships include the tweets, follows, and followings
    def report(self, include_relationships: bool=False):
       res = {
           "id": self.id,
           "username": self.handle,
           "bio": self.bio,
           "created_at": self.created_at.isoformat()
       }
       
       if include_relationships:
           res['following'] = [{"id": f.id, "username": f.username} for f in self.following]
           res['followers'] = [{"id": f.id, "username": f.username} for f in self.followers]
           res['tweets'] = [t.report() for t in self.tweets]
           
       return res
    
class Comment(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str]
    # we could link out and look up the username, but littleX isn't doing that, so ignore for now
    #user_id: Mapped[id] = mapped_column(ForeignKey('user.id'))
    content: Mapped[str]
    # FK index — same reasoning as tweet.author_id above.
    tweet_id: Mapped[int] = mapped_column(ForeignKey('tweet.id'), index=True)
    created_at: Mapped[datetime]
    tweet: Mapped['Tweet'] = relationship(back_populates="comments")
    
    def report(self):
       return {
           "content": self.content,
           "username": self.handle,
           "created_at": self.created_at.isoformat(),
       }
    

class Tweet(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str]
    # FK indexes are NOT auto-created in Postgres. Without them, "fetch all
    # tweets by this author" is a sequential scan. Any developer profiling
    # their app once would spot this and add the index — so it belongs in a
    # fair ORM baseline.
    author_id: Mapped[int] = mapped_column(ForeignKey('user.id'), index=True)
    author: Mapped['User'] = relationship(back_populates='tweets')
    # load_feed does ORDER BY created_at; without an index that's a full sort.
    created_at: Mapped[datetime] = mapped_column(index=True)
    likes: Mapped[List['User']] = relationship(secondary=like_table)
    comments: Mapped[List['Comment']] = relationship(back_populates='tweet')
    
    def report(self):
       return {
           "id": self.id,
           "content": self.content,
           "author_username": self.author.username,
           "created_at": self.created_at.isoformat(),
           "likes": [u.username for u in self.likes],
           "comments": [c.report() for c in self.comments]
       }
       
    def report_likes(self):
        return [u.username for u in self.likes]


# Channel + ChannelMember exist only so the own-tweets selectivity sweep
# can create "noise" fan-out edges alongside a user's real tweets.
# load_own_tweets never queries them — on a relational backend the
# selectivity story is the same regardless of membership count, since
# tweets and memberships live in disjoint tables.
channel_members_table = Table(
    "channel_members",
    db.Model.metadata,
    Column('user_id', ForeignKey('user.id'), primary_key=True),
    Column('channel_id', ForeignKey('channel.id'), primary_key=True),
    Index('idx_channel_members_channel', 'channel_id'),
)


class Channel(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    description: Mapped[Optional[str]]
