from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import relationship

from .database import Base, SessionLocal, engine


class Post(Base):
    __tablename__ = 'posts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False)
    external_id = Column(String(50), nullable=False)
    group_id = Column(String(50))
    date = Column(DateTime, nullable=False)
    text = Column(Text)
    text_normalized = Column(Text)
    url = Column(String(255))
    likes = Column(Integer, default=0)
    comments_count = Column(Integer, default=0)
    is_pinned = Column(Boolean, default=False)
    status = Column(String(20), default='new')

    # Связь с комментариями
    comments = relationship(
        "Comment",
        back_populates="post",
        cascade="all, delete-orphan",
        primaryjoin="and_(Post.source == foreign(Comment.source), Post.group_id == foreign(Comment.group_id), Post.external_id == foreign(Comment.post_external_id))"
    )

    __table_args__ = (
        UniqueConstraint('source', 'external_id', 'group_id',
                         name='uq_post_source_external_group'),
    )


class Comment(Base):
    __tablename__ = 'comments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False)
    external_id = Column(String(50), nullable=False)
    post_external_id = Column(String(50), nullable=False)
    group_id = Column(String(50))
    author_id = Column(String(50))
    author_name = Column(String(255))
    text = Column(Text)
    text_normalized = Column(Text)
    date = Column(DateTime, nullable=False)
    likes = Column(Integer, default=0)
    reply_to = Column(String(50))
    is_processed = Column(Boolean, default=False)
    tg_notified = Column(Boolean, default=False)
    ai_suggested_reply = Column(Text, nullable=True)
    url = Column(String(255))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    status = Column(String(20), default='new')

    post = relationship(
        "Post",
        back_populates="comments",
        primaryjoin="and_(Post.source == foreign(Comment.source), Post.group_id == foreign(Comment.group_id), Post.external_id == foreign(Comment.post_external_id))"
    )

    __table_args__ = (
        UniqueConstraint('source', 'external_id', 'post_external_id', 'group_id',
                         name='uq_comment_source_external_post_group'),
    )


class Prompt(Base):
    __tablename__ = 'prompts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    text = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
