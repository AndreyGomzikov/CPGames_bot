import os
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from .config import EXPORT_DIR
from .models import Comment, Post
from .normalizer import CommentNormalizer


def save_post(db: Session, post_data: dict) -> Post:
    """Сохраняет пост, исключая дубликаты по source + external_id + group_id"""
    post_data = CommentNormalizer.normalize_post(post_data)

    existing = db.query(Post).filter(
        Post.source == post_data['source'],
        Post.external_id == post_data['external_id'],
        Post.group_id == post_data['group_id']
    ).first()

    if existing:
        existing.likes = post_data.get('likes', existing.likes)
        existing.comments_count = post_data.get(
            'comments_count', existing.comments_count)
        existing.text = post_data.get('text', existing.text)
        existing.text_normalized = post_data.get(
            'text_normalized', existing.text_normalized)
        existing.date = post_data.get('date', existing.date)
        existing.url = post_data.get('url', existing.url)
        existing.is_pinned = post_data.get('is_pinned', existing.is_pinned)
        db.commit()
        return existing
    else:
        post = Post(
            source=post_data['source'],
            external_id=post_data['external_id'],
            group_id=post_data['group_id'],
            date=post_data['date'],
            text=post_data.get('text', ''),
            text_normalized=post_data.get('text_normalized', ''),
            url=post_data.get('url', ''),
            likes=post_data.get('likes', 0),
            comments_count=post_data.get('comments_count', 0),
            is_pinned=post_data.get('is_pinned', False)
        )
        db.add(post)
        db.flush()
        db.commit()
        return post


def save_comment(db: Session, comment_data: dict) -> Comment:
    comment_data = CommentNormalizer.normalize_comment(comment_data)

    existing = db.query(Comment).filter(
        Comment.source == comment_data['source'],
        Comment.external_id == comment_data['external_id'],
        Comment.post_external_id == comment_data['post_external_id'],
        Comment.group_id == comment_data['group_id']
    ).first()

    if existing:
        existing.likes = comment_data.get('likes', existing.likes)
        existing.text = comment_data.get('text', existing.text)
        existing.text_normalized = comment_data.get(
            'text_normalized', existing.text_normalized)
        db.commit()
        return existing

    comment = Comment(
        source=comment_data['source'],
        external_id=comment_data['external_id'],
        post_external_id=comment_data['post_external_id'],
        group_id=comment_data['group_id'],
        author_id=comment_data.get('author_id'),
        author_name=comment_data.get('author_name', 'Unknown'),
        text=comment_data.get('text', ''),
        text_normalized=comment_data.get('text_normalized', ''),
        date=comment_data['date'],
        likes=comment_data.get('likes', 0),
        reply_to=comment_data.get('reply_to'),
        url=comment_data.get('url', ''),
        is_processed=False,
        tg_notified=False
    )
    db.add(comment)
    db.commit()
    return comment


def save_comments_batch(db: Session, comments_data: List[Dict]) -> int:
    saved_count = 0
    for comment_data in comments_data:
        if 'post_text' in comment_data:
            post_data = {
                'source': comment_data['source'],
                'external_id': comment_data['post_external_id'],
                'group_id': comment_data['group_id'],
                'date': comment_data.get('post_date', datetime.now()),
                'text': comment_data['post_text'],
                'url': f"https://vk.com/wall{comment_data['group_id']}_{comment_data['post_external_id']}",
                'likes': comment_data.get('post_likes', 0),
                'comments_count': 0,
                'is_pinned': False
            }
            save_post(db, post_data)

        saved = save_comment(db, comment_data)
        if saved.id:
            saved_count += 1

    db.commit()
    return saved_count


def save_comments_to_csv(comments: List[Dict], filename: str = None) -> str:
    """Сохраняет список комментариев в CSV в папку EXPORT_DIR"""
    if not comments:
        return None
    if filename is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = os.path.join(EXPORT_DIR, f"comments_{timestamp}.csv")

    import csv
    fieldnames = [
        'id', 'source', 'external_id', 'post_external_id', 'group_id',
        'author_id', 'author_name', 'text', 'text_normalized', 'date',
        'likes', 'reply_to', 'is_processed', 'tg_notified', 'url'
    ]
    with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(comments)
    return filename
