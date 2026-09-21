"""Парсер комментариев к постам Telegram-канала через Telethon.

По умолчанию читает комментарии к 3 последним постам канала @fabrica_igr.
Количество постов и комментариев на пост настраивается через .env или CLI.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (FloodWaitError, MsgIdInvalidError,
                             PeerIdInvalidError, RPCError)
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from sqlalchemy.exc import IntegrityError, OperationalError

from .comment_policy import get_ignore_reason
from .models import Base, Comment, Post, SessionLocal, engine

load_dotenv()


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом") from exc


API_ID = _env_int("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
SESSION_NAME = os.getenv(
    "SESSION_NAME", str(Path(__file__).resolve().with_name("parser_session"))
)

TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "fabrica_igr")
TELEGRAM_POSTS_LIMIT = _env_int("TELEGRAM_POSTS_LIMIT", 3) or 3
TELEGRAM_COMMENTS_LIMIT = _env_int("TELEGRAM_COMMENTS_LIMIT", 200) or 200
TELEGRAM_EXPORT_DIR = os.getenv("TELEGRAM_EXPORT_DIR", "data")
TELEGRAM_CHECK_INTERVAL = _env_int("TELEGRAM_CHECK_INTERVAL", 60) or 60
TELEGRAM_STATE_FILE = os.getenv(
    "TELEGRAM_STATE_FILE", str(Path(TELEGRAM_EXPORT_DIR) / "telegram_seen.json")
)
TELEGRAM_NOTIFY_EXISTING = os.getenv("TELEGRAM_NOTIFY_EXISTING", "false").strip().lower() in {
    "1", "true", "yes", "on"
}


def create_client() -> TelegramClient:
    """Создаёт Telethon-клиент на базе существующего session-файла."""
    if not API_ID or not API_HASH:
        raise RuntimeError(
            "Не заданы API_ID/API_HASH. Добавьте их в .env "
            "(получить можно на https://my.telegram.org)."
        )

    return TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
        connection=ConnectionTcpAbridged,
        timeout=30,
        retry_delay=3,
        auto_reconnect=True,
    )


def _message_replies_count(message: Any) -> int:
    replies = getattr(message, "replies", None)
    if not replies:
        return 0

    # В Telethon MessageReplies содержит поле `replies`. В старых/чужих
    # обёртках встречалось `count`, поэтому оставляем совместимый fallback.
    value = getattr(replies, "replies", None)
    if value is None:
        value = getattr(replies, "count", 0)
    return int(value or 0)


def _message_discussion_group_id(message: Any) -> str:
    """Return the linked discussion group id exposed by Telegram, if present."""
    replies = getattr(message, "replies", None)
    if not replies:
        return ""
    return str(getattr(replies, "channel_id", None) or "")


def _short_text(text: str | None, size: int = 100) -> str:
    normalized = " ".join((text or "").split())
    return normalized if len(normalized) <= size else normalized[: size - 1] + "…"


def _public_post_url(entity: Any, post_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{post_id}"
    return ""


def _public_comment_url(entity: Any, post_id: int, comment_id: int) -> str:
    post_url = _public_post_url(entity, post_id)
    return f"{post_url}?comment={comment_id}" if post_url else ""


async def _sender_data(client: TelegramClient, message: Any) -> dict[str, str]:
    author_id = str(message.sender_id or "")
    result = {
        "author_id": author_id,
        "author_entity_id": "",
        "author_name": "Неизвестно",
        "author_username": "",
        "author_peer_type": "",
    }

    try:
        sender = await message.get_sender()
        if sender is None and message.sender_id:
            sender = await client.get_entity(message.sender_id)
        if sender is None:
            return result

        first_name = getattr(sender, "first_name", None) or ""
        last_name = getattr(sender, "last_name", None) or ""
        title = getattr(sender, "title", None) or ""
        username = getattr(sender, "username", None) or ""
        name = " ".join(part for part in (first_name, last_name) if part).strip()
        result["author_entity_id"] = str(getattr(sender, "id", None) or "")
        result["author_name"] = name or title or username or author_id or "Неизвестно"
        result["author_username"] = username
        result["author_peer_type"] = sender.__class__.__name__.lower()
    except (RPCError, ValueError):
        pass

    return result


async def get_recent_posts(
    client: TelegramClient,
    entity: Any,
    posts_limit: int,
) -> list[Any]:
    """Возвращает последние N логических постов канала.

    Telegram представляет медиа-альбом как несколько последовательных сообщений
    с одинаковым ``grouped_id``. Поэтому нельзя задавать фиксированный запас
    сообщений (например, ``posts_limit * 5``): несколько больших альбомов могут
    занять весь запас, и функция вернёт меньше N постов.

    Вместо этого читаем историю до появления первого сообщения уже следующего,
    (N + 1)-го, логического поста. Так мы успеваем обработать все части N-го
    альбома и выбрать среди них правильный root для ветки комментариев.
    """
    if posts_limit < 1:
        return []

    grouped: dict[tuple[str, int], Any] = {}
    order: list[tuple[str, int]] = []

    async for message in client.iter_messages(entity, limit=None, reverse=False):
        if getattr(message, "action", None):
            continue

        grouped_id = getattr(message, "grouped_id", None)
        key = ("album", int(grouped_id)) if grouped_id else ("message", int(message.id))

        if key not in grouped:
            # Как только встретили новый логический пост после уже собранных N,
            # можно остановиться: все части первых N постов уже просмотрены.
            if len(order) >= posts_limit:
                break
            grouped[key] = message
            order.append(key)
        else:
            # Для альбома предпочитаем ту часть, на которой Telegram показывает
            # счётчик комментариев. Именно её id нужно использовать как thread root.
            current = grouped[key]
            if _message_replies_count(message) > _message_replies_count(current):
                grouped[key] = message
            elif not getattr(current, "text", None) and getattr(message, "text", None):
                grouped[key] = message

    return [grouped[key] for key in order]


async def get_post_comments(
    client: TelegramClient,
    entity: Any,
    post: Any,
    comments_limit: int,
) -> list[dict[str, Any]]:
    """Читает комментарии (replies в linked discussion group) к одному посту."""
    expected = _message_replies_count(post)
    if expected <= 0:
        return []

    rows: list[dict[str, Any]] = []
    try:
        async for comment in client.iter_messages(
            entity,
            reply_to=post.id,
            limit=comments_limit,
            reverse=False,
        ):
            sender = await _sender_data(client, comment)
            rows.append(
                {
                    "source": "telegram",
                    "channel": getattr(entity, "username", None) or str(getattr(entity, "id", "")),
                    "channel_id": str(getattr(entity, "id", None) or ""),
                    "channel_title": getattr(entity, "title", ""),
                    "discussion_group_id": _message_discussion_group_id(post),
                    "post_id": int(post.id),
                    "post_date": post.date.isoformat() if post.date else "",
                    "post_text": post.text or "",
                    "post_url": _public_post_url(entity, post.id),
                    "declared_comments_count": expected,
                    "comment_id": int(comment.id),
                    "comment_date": comment.date.isoformat() if comment.date else "",
                    "comment_text": comment.text or "",
                    "comment_url": _public_comment_url(entity, post.id, comment.id),
                    "reply_to_msg_id": getattr(comment, "reply_to_msg_id", None),
                    **sender,
                }
            )
    except (MsgIdInvalidError, PeerIdInvalidError) as exc:
        print(f"⚠️ Пост {post.id}: Telegram не отдал ветку комментариев: {exc}")
    except FloodWaitError as exc:
        print(f"⏳ Telegram просит подождать {exc.seconds} сек.")
        await asyncio.sleep(exc.seconds)
        return await get_post_comments(client, entity, post, comments_limit)

    return rows


async def parse_channel_comments(
    channel: str = TELEGRAM_CHANNEL,
    posts_limit: int = TELEGRAM_POSTS_LIMIT,
    comments_limit: int = TELEGRAM_COMMENTS_LIMIT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Собирает комментарии к последним постам канала."""
    client = create_client()
    await client.connect()

    try:
        if not await client.is_user_authorized():
            if not PHONE_NUMBER:
                raise RuntimeError(
                    "Session не авторизована и PHONE_NUMBER не указан. "
                    "Добавьте PHONE_NUMBER в .env и запустите снова."
                )
            if not sys.stdin.isatty():
                raise RuntimeError(
                    "Telegram session не авторизована. Выполните один раз "
                    "./deploy/authorize_telegram.sh в интерактивной SSH-сессии."
                )
            print("🔐 Требуется первая авторизация Telegram…")
            await client.start(phone=PHONE_NUMBER)

        entity = await client.get_entity(channel)
        title = getattr(entity, "title", channel)
        print(f"📡 Канал: {title} (@{getattr(entity, 'username', '') or 'без username'})")
        print(f"🧾 Проверяем последние {posts_limit} поста(ов)…")

        posts = await get_recent_posts(client, entity, posts_limit)
        post_rows: list[dict[str, Any]] = []
        comments: list[dict[str, Any]] = []

        for index, post in enumerate(posts, start=1):
            declared_count = _message_replies_count(post)
            print(
                f"[{index}/{len(posts)}] Пост {post.id}: "
                f"{declared_count} комментариев — {_short_text(post.text)}"
            )
            post_comments = await get_post_comments(
                client, entity, post, comments_limit
            )
            comments.extend(post_comments)
            post_rows.append(
                {
                    "post_id": int(post.id),
                    "post_date": post.date.isoformat() if post.date else "",
                    "post_text": post.text or "",
                    "post_url": _public_post_url(entity, post.id),
                    "declared_comments_count": declared_count,
                    "parsed_comments_count": len(post_comments),
                }
            )
            print(f"    ✅ Получено: {len(post_comments)}")

        return post_rows, comments
    finally:
        await client.disconnect()


def export_results(
    posts: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    output_dir: str | os.PathLike[str] = TELEGRAM_EXPORT_DIR,
) -> tuple[Path, Path]:
    """Сохраняет результат одновременно в CSV и JSON."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = directory / f"telegram_comments_{timestamp}.csv"
    json_path = directory / f"telegram_comments_{timestamp}.json"

    fields = [
        "source",
        "channel",
        "channel_id",
        "channel_title",
        "discussion_group_id",
        "post_id",
        "post_date",
        "post_text",
        "post_url",
        "declared_comments_count",
        "comment_id",
        "comment_date",
        "author_id",
        "author_entity_id",
        "author_name",
        "author_username",
        "author_peer_type",
        "comment_text",
        "comment_url",
        "reply_to_msg_id",
    ]

    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(comments)

    with json_path.open("w", encoding="utf-8") as file:
        json.dump(
            {"posts": posts, "comments": comments},
            file,
            ensure_ascii=False,
            indent=2,
        )

    return csv_path, json_path


def _comment_key(row: dict[str, Any]) -> str:
    """Stable deduplication key for one Telegram comment."""
    return f"{row.get('channel', '')}:{row.get('post_id', '')}:{row.get('comment_id', '')}"


def _load_seen(path: str | os.PathLike[str]) -> set[str]:
    state_path = Path(path)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    values = payload.get("seen", []) if isinstance(payload, dict) else []
    return {str(value) for value in values}


def _save_seen(path: str | os.PathLike[str], values: set[str]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the state bounded for long-running production use.
    ordered = sorted(values)[-20000:]
    temp = state_path.with_suffix(state_path.suffix + ".tmp")
    temp.write_text(
        json.dumps({"seen": ordered}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(state_path)


def _parse_telegram_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return datetime.now()
    else:
        return datetime.now()

    # Database timestamps in this project are stored as naive datetimes.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _telegram_ignore_reason(row: dict[str, Any]) -> str | None:
    """Classify a Telegram comment before it can be queued for AI."""
    channel = str(row.get("channel") or TELEGRAM_CHANNEL).lstrip("@")
    return get_ignore_reason(
        row.get("comment_text", ""),
        source="telegram",
        author_id=row.get("author_id"),
        author_entity_id=row.get("author_entity_id"),
        author_name=row.get("author_name"),
        author_username=row.get("author_username"),
        author_peer_type=row.get("author_peer_type"),
        group_id=channel,
        own_author_ids=(row.get("channel_id"), row.get("discussion_group_id")),
        own_usernames=(channel,),
    )


def _mark_existing_telegram_comment_ignored(row: dict[str, Any], reason: str) -> None:
    """Neutralize an already queued own-brand comment from an older release."""
    channel = str(row.get("channel") or TELEGRAM_CHANNEL).lstrip("@")
    post_id = str(row.get("post_id") or "")
    comment_id = str(row.get("comment_id") or "")
    if not post_id or not comment_id:
        return

    db = SessionLocal()
    try:
        existing = (
            db.query(Comment)
            .filter(
                Comment.source == "telegram",
                Comment.external_id == comment_id,
                Comment.post_external_id == post_id,
                Comment.group_id == channel,
            )
            .first()
        )
        if existing and not existing.is_processed:
            existing.status = "ignored"
            existing.is_processed = True
            existing.tg_notified = True
            existing.ai_suggested_reply = None
            existing.updated_at = datetime.now()
            db.commit()
            print(
                f"⏩ Ранее сохранённый Telegram comment {comment_id} "
                f"помечен ignored ({reason})"
            )
    finally:
        db.close()


def persist_telegram_comment(row: dict[str, Any], *, baseline: bool) -> bool:
    """Persist one Telegram comment into the shared application database.

    The Telethon worker never sends SMM messages itself.  New rows are stored
    with ``tg_notified=False`` and main.py delivers the rich notification
    (AI suggestion + button), which keeps a single notification path.

    Baseline rows are stored with ``tg_notified=True`` so a first production
    scan with TELEGRAM_NOTIFY_EXISTING=false cannot be replayed later.
    """
    channel = str(row.get("channel") or TELEGRAM_CHANNEL).lstrip("@")
    post_id = str(row.get("post_id") or "")
    comment_id = str(row.get("comment_id") or "")
    if not post_id or not comment_id:
        print("⚠️ Telegram comment пропущен: нет post_id/comment_id")
        return False

    ignore_reason = _telegram_ignore_reason(row)
    if ignore_reason:
        # Also neutralize a row queued by an older release, if it still exists.
        _mark_existing_telegram_comment_ignored(row, ignore_reason)
        # Return True so the watcher still records this comment in its seen-state
        # and does not reconsider it on every polling cycle.
        print(f"⏩ Telegram comment {comment_id} проигнорирован ({ignore_reason})")
        return True

    db = SessionLocal()
    try:
        existing = (
            db.query(Comment)
            .filter(
                Comment.source == "telegram",
                Comment.external_id == comment_id,
                Comment.post_external_id == post_id,
                Comment.group_id == channel,
            )
            .first()
        )
        if existing:
            return True

        post = (
            db.query(Post)
            .filter(
                Post.source == "telegram",
                Post.external_id == post_id,
                Post.group_id == channel,
            )
            .first()
        )
        if not post:
            post = Post(
                source="telegram",
                external_id=post_id,
                group_id=channel,
                date=_parse_telegram_datetime(row.get("post_date")),
                text=str(row.get("post_text") or ""),
                url=str(row.get("post_url") or f"https://t.me/{channel}/{post_id}"),
                comments_count=int(row.get("declared_comments_count") or 0),
            )
            db.add(post)
            db.flush()

        comment_url = str(row.get("comment_url") or "")
        if not comment_url:
            comment_url = f"https://t.me/{channel}/{post_id}?comment={comment_id}"

        comment = Comment(
            source="telegram",
            external_id=comment_id,
            post_external_id=post_id,
            group_id=channel,
            author_id=str(row.get("author_id") or ""),
            author_name=str(row.get("author_name") or "Неизвестно"),
            text=str(row.get("comment_text") or "Без текста"),
            date=_parse_telegram_datetime(row.get("comment_date")),
            likes=0,
            reply_to=(
                str(row.get("reply_to_msg_id"))
                if row.get("reply_to_msg_id") is not None
                else None
            ),
            is_processed=False,
            tg_notified=bool(baseline),
            url=comment_url,
            status="baseline" if baseline else "new",
        )
        db.add(comment)
        db.commit()
        print(
            f"💾 Telegram comment {comment_id} сохранён в БД "
            f"({'baseline' if baseline else 'new'})"
        )
        return True
    except IntegrityError:
        # Another process/scan may have inserted the same row between SELECT
        # and INSERT. Treat that as successfully persisted for dedup state.
        db.rollback()
        return True
    except OperationalError as exc:
        db.rollback()
        print(f"⚠️ База данных временно недоступна для Telegram comment {comment_id}: {exc}")
        return False
    except Exception as exc:
        db.rollback()
        print(f"⚠️ Не удалось сохранить Telegram comment {comment_id}: {exc}")
        return False
    finally:
        db.close()


async def watch_channel_comments(
    channel: str,
    posts_limit: int,
    comments_limit: int,
    interval: int,
    state_file: str,
    notify_existing: bool,
) -> None:
    """Continuously scan recent posts and queue unseen comments for main.py."""
    if interval < 10:
        raise ValueError("--interval должен быть >= 10 секунд")

    # Ensure tables exist even when the worker starts a moment before app.
    Base.metadata.create_all(bind=engine)

    first_scan = not Path(state_file).exists()
    while True:
        try:
            posts, comments = await parse_channel_comments(
                channel=channel,
                posts_limit=posts_limit,
                comments_limit=comments_limit,
            )
            seen = _load_seen(state_file)
            current_by_key = {_comment_key(row): row for row in comments}

            # Re-check own-brand rows even if they are already in seen-state.
            # This cleans up pending rows created by older versions before they
            # can be picked up by main.py and sent to the AI.
            for row in current_by_key.values():
                reason = _telegram_ignore_reason(row)
                if reason == "own_brand":
                    _mark_existing_telegram_comment_ignored(row, reason)

            if first_scan:
                candidate_rows = list(current_by_key.values())
                baseline = not notify_existing
                if baseline:
                    print(
                        f"ℹ️ Первый запуск: {len(candidate_rows)} существующих комментариев "
                        "сохраняются как baseline без SMM-уведомлений."
                    )
            else:
                candidate_rows = [
                    row for key, row in current_by_key.items() if key not in seen
                ]
                baseline = False

            persisted_keys: set[str] = set()
            queued = 0
            baselined = 0
            # Oldest first makes SMM notifications arrive chronologically.
            for row in reversed(candidate_rows):
                row_key = _comment_key(row)
                if persist_telegram_comment(row, baseline=baseline):
                    persisted_keys.add(row_key)
                    if baseline:
                        baselined += 1
                    else:
                        queued += 1

            # Existing comments that were already in the state remain seen.
            # New keys are marked only after they were persisted (or found as
            # duplicates), so transient DB failures are retried next scan.
            seen.update(persisted_keys)
            if first_scan and not candidate_rows:
                # Create the state file even for a channel with no comments.
                persisted_keys = set()
            _save_seen(state_file, seen)
            first_scan = False

            print(
                f"✅ Telegram scan: posts={len(posts)}, comments={len(comments)}, "
                f"new={0 if baseline else len(candidate_rows)}, queued={queued}, "
                f"baseline={baselined}"
            )
        except (RPCError, RuntimeError, ValueError, OSError) as exc:
            print(f"⚠️ Ошибка Telegram worker: {exc}")
        except Exception as exc:
            # A production worker must survive unexpected per-scan failures.
            print(f"⚠️ Неожиданная ошибка Telegram worker: {exc}")

        await asyncio.sleep(interval)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Парсер комментариев к последним постам Telegram-канала"
    )
    parser.add_argument(
        "--channel",
        default=TELEGRAM_CHANNEL,
        help="username/ссылка канала (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--posts",
        type=int,
        default=TELEGRAM_POSTS_LIMIT,
        help="сколько последних постов проверить (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--comments-per-post",
        type=int,
        default=TELEGRAM_COMMENTS_LIMIT,
        help="максимум комментариев с одного поста (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        default=TELEGRAM_EXPORT_DIR,
        help="каталог для CSV/JSON (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="работать постоянно и передавать новые комментарии в main.py",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=TELEGRAM_CHECK_INTERVAL,
        help="интервал проверки в секундах для --watch (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--state-file",
        default=TELEGRAM_STATE_FILE,
        help="файл дедупликации для --watch (по умолчанию: %(default)s)",
    )
    parser.add_argument(
        "--notify-existing",
        action="store_true",
        default=TELEGRAM_NOTIFY_EXISTING,
        help="на первом запуске поставить в очередь и существующие комментарии",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.posts < 1:
        raise ValueError("--posts должен быть >= 1")
    if args.comments_per_post < 1:
        raise ValueError("--comments-per-post должен быть >= 1")

    if args.watch:
        await watch_channel_comments(
            channel=args.channel,
            posts_limit=args.posts,
            comments_limit=args.comments_per_post,
            interval=args.interval,
            state_file=args.state_file,
            notify_existing=args.notify_existing,
        )
        return 0

    posts, comments = await parse_channel_comments(
        channel=args.channel,
        posts_limit=args.posts,
        comments_limit=args.comments_per_post,
    )
    csv_path, json_path = export_results(posts, comments, args.output_dir)

    print("-" * 60)
    print(f"✅ Проверено постов: {len(posts)}")
    print(f"✅ Собрано комментариев: {len(comments)}")
    print(f"💾 CSV:  {csv_path.resolve()}")
    print(f"💾 JSON: {json_path.resolve()}")
    return 0


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем")
        return 130
    except (RPCError, RuntimeError, ValueError) as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
