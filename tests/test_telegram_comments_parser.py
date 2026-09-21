import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src import bot


class AsyncMessages:
    def __init__(self, values):
        self.values = list(values)

    def __aiter__(self):
        self._iter = iter(self.values)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeMessage:
    def __init__(
        self,
        message_id,
        text="",
        replies=0,
        grouped_id=None,
        sender_id=1,
        sender=None,
        discussion_group_id=None,
    ):
        self.id = message_id
        self.text = text
        self.date = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.replies = (
            SimpleNamespace(replies=replies, channel_id=discussion_group_id)
            if replies is not None
            else None
        )
        self.grouped_id = grouped_id
        self.action = None
        self.sender_id = sender_id
        self.reply_to_msg_id = None
        self._sender = sender or SimpleNamespace(
            first_name="Иван",
            last_name="Иванов",
            username="ivan",
        )

    async def get_sender(self):
        return self._sender


class FakeClient:
    def __init__(self, timeline=None, replies=None):
        self.timeline = list(timeline or [])
        self.replies = replies or {}

    def iter_messages(self, entity, **kwargs):
        reply_to = kwargs.get("reply_to")
        if reply_to is not None:
            values = self.replies.get(reply_to, [])
        else:
            values = self.timeline

        limit = kwargs.get("limit")
        if limit is not None:
            values = values[:limit]
        return AsyncMessages(values)

    async def get_entity(self, entity):
        return entity


def test_message_replies_count_supports_telethon_field():
    message = SimpleNamespace(replies=SimpleNamespace(replies=17))
    assert bot._message_replies_count(message) == 17


@pytest.mark.asyncio
async def test_recent_posts_deduplicates_album_and_prefers_comment_root():
    messages = [
        FakeMessage(103, "", replies=0, grouped_id=500),
        FakeMessage(102, "Альбом", replies=7, grouped_id=500),
        FakeMessage(101, "Второй пост", replies=2),
        FakeMessage(100, "Третий пост", replies=0),
    ]
    posts = await bot.get_recent_posts(FakeClient(timeline=messages), object(), 3)

    assert [post.id for post in posts] == [102, 101, 100]


@pytest.mark.asyncio
async def test_recent_posts_handles_large_albums_without_fixed_scan_limit():
    messages = []
    # Два альбома по 10 сообщений каждый раньше полностью съедали scan_limit=15
    # при запросе трёх постов, и третий пост не находился.
    for message_id in range(120, 110, -1):
        messages.append(FakeMessage(message_id, grouped_id=1000))
    for message_id in range(110, 100, -1):
        messages.append(FakeMessage(message_id, grouped_id=999))
    messages.extend(
        [
            FakeMessage(100, "Третий пост", replies=3),
            FakeMessage(99, "Четвёртый пост", replies=1),
        ]
    )

    posts = await bot.get_recent_posts(FakeClient(timeline=messages), object(), 3)

    assert len(posts) == 3
    assert [post.grouped_id for post in posts[:2]] == [1000, 999]
    assert posts[2].id == 100


@pytest.mark.asyncio
async def test_get_post_comments_collects_author_and_links():
    entity = SimpleNamespace(id=777, username="fabrica_igr", title="Фабрика Игр")
    post = FakeMessage(555, "Пост", replies=2, discussion_group_id=888)
    comments = [
        FakeMessage(9001, "Первый комментарий"),
        FakeMessage(9002, "Второй комментарий"),
    ]
    client = FakeClient(replies={555: comments})

    rows = await bot.get_post_comments(client, entity, post, 100)

    assert len(rows) == 2
    assert rows[0]["post_id"] == 555
    assert rows[0]["channel_id"] == "777"
    assert rows[0]["discussion_group_id"] == "888"
    assert rows[0]["comment_text"] == "Первый комментарий"
    assert rows[0]["author_name"] == "Иван Иванов"
    assert rows[0]["post_url"] == "https://t.me/fabrica_igr/555"
    assert rows[0]["comment_url"] == "https://t.me/fabrica_igr/555?comment=9001"


@pytest.mark.asyncio
async def test_get_post_comments_exposes_channel_sender_identity():
    entity = SimpleNamespace(id=777, username="fabrica_igr", title="Фабрика Игр")
    post = FakeMessage(555, "Пост", replies=1, discussion_group_id=888)

    class Channel:
        def __init__(self):
            self.id = 777
            self.title = "Название может отличаться"
            self.username = "fabrica_igr"
            self.first_name = None
            self.last_name = None

    comment = FakeMessage(9001, "Ответ группы", sender_id=-100777, sender=Channel())
    rows = await bot.get_post_comments(FakeClient(replies={555: [comment]}), entity, post, 100)

    assert rows[0]["author_id"] == "-100777"
    assert rows[0]["author_entity_id"] == "777"
    assert rows[0]["author_username"] == "fabrica_igr"
    assert rows[0]["author_peer_type"] == "channel"
    assert bot._telegram_ignore_reason(rows[0]) == "own_brand"


def test_export_results_writes_csv_and_json(tmp_path):
    posts = [{"post_id": 1, "parsed_comments_count": 1}]
    comments = [
        {
            "source": "telegram",
            "channel": "fabrica_igr",
            "channel_id": "777",
            "channel_title": "Фабрика Игр",
            "discussion_group_id": "888",
            "post_id": 1,
            "post_date": "2026-08-26T00:00:00+00:00",
            "post_text": "Пост",
            "post_url": "https://t.me/fabrica_igr/1",
            "declared_comments_count": 1,
            "comment_id": 2,
            "comment_date": "2026-08-26T00:01:00+00:00",
            "author_id": "3",
            "author_entity_id": "3",
            "author_name": "Тест",
            "author_username": "test",
            "author_peer_type": "user",
            "comment_text": "Комментарий",
            "comment_url": "https://t.me/fabrica_igr/1?comment=2",
            "reply_to_msg_id": None,
        }
    ]

    csv_path, json_path = bot.export_results(posts, comments, tmp_path)

    with csv_path.open(encoding="utf-8-sig") as file:
        csv_rows = list(csv.DictReader(file))
    assert csv_rows[0]["comment_text"] == "Комментарий"

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["posts"][0]["post_id"] == 1
    assert payload["comments"][0]["comment_id"] == 2


def test_watch_cli_is_available():
    args = bot.build_arg_parser().parse_args(["--watch"])
    assert args.watch is True
    assert args.interval == bot.TELEGRAM_CHECK_INTERVAL
    assert args.state_file == bot.TELEGRAM_STATE_FILE


def test_seen_state_roundtrip(tmp_path):
    state_file = tmp_path / "telegram_seen.json"
    values = {"fabrica_igr:2111:1", "fabrica_igr:2111:2"}

    bot._save_seen(state_file, values)

    assert bot._load_seen(state_file) == values
