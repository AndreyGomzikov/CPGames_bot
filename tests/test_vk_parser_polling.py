import json
from datetime import datetime

from src.vk_parser import VKParser


def _raw_comment(comment_id: int, text: str = "Комментарий"):
    return {
        "id": comment_id,
        "from_id": 42,
        "text": text,
        "date": int(datetime(2026, 8, 29, 12, 0).timestamp()),
        "likes": {"count": 0},
    }


def test_fetch_comments_uses_pagination_for_more_than_100(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
    )
    calls = []
    all_items = [_raw_comment(i) for i in range(1, 206)]

    def fake_api_request(method, params):
        assert method == "wall.getComments"
        calls.append(dict(params))
        offset = params["offset"]
        count = params["count"]
        return {
            "response": {
                "count": len(all_items),
                "items": all_items[offset : offset + count],
            }
        }

    parser._api_request = fake_api_request
    parser._get_user_name = lambda user_id: "Тестовый пользователь"

    comments = parser.fetch_comments("777")

    assert comments is not None
    assert len(comments) == 205
    assert [call["offset"] for call in calls] == [0, 100, 200]
    assert comments[-1]["external_id"] == 205


def test_first_run_baselines_existing_comments_without_notifications(tmp_path):
    state_file = tmp_path / "vk_parser_state.json"
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(state_file),
        notify_existing=False,
    )

    posts = [
        {
            "external_id": "10",
            "text": "Пост",
            "comments_count": 2,
        }
    ]
    current_comments = [_raw_comment(1, "Старый 1"), _raw_comment(2, "Старый 2")]

    parser.fetch_posts = lambda count: posts

    def fake_fetch_comments(post_id, count=None):
        result = []
        for item in current_comments:
            result.append(
                {
                    "source": "vk",
                    "external_id": item["id"],
                    "post_external_id": str(post_id),
                    "group_id": -123,
                    "author_id": 42,
                    "author_name": "Тест",
                    "text": item["text"],
                    "date": datetime(2026, 8, 29, 12, 0),
                    "likes": 0,
                    "reply_to": None,
                }
            )
        return result

    parser.fetch_comments = fake_fetch_comments

    # Existing comments become the baseline and are not returned as "new".
    assert parser.get_new_comments(3) == []
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(state["comment_ids"]) == {"10:1", "10:2"}

    # A later comment is returned normally.
    current_comments.append(_raw_comment(3, "Новый"))
    new_comments = parser.get_new_comments(3)
    assert [comment["external_id"] for comment in new_comments] == [3]


def test_first_run_does_not_persist_partial_baseline_on_vk_error(tmp_path):
    state_file = tmp_path / "vk_parser_state.json"
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(state_file),
        notify_existing=False,
    )
    parser.fetch_posts = lambda count: [
        {"external_id": "10", "text": "Пост", "comments_count": 1}
    ]
    parser.fetch_comments = lambda post_id, count=None: None

    assert parser.get_new_comments(3) == []
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "comment_ids" not in state


def test_existing_comments_backfill_ignores_seen_state_and_marks_complete(tmp_path):
    state_file = tmp_path / "vk_parser_state.json"
    state_file.write_text(
        json.dumps({"comment_ids": ["10:1", "10:2"]}),
        encoding="utf-8",
    )
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(state_file),
        notify_existing=False,
    )

    assert parser.needs_db_backfill is True

    parser.fetch_posts = lambda count: [
        {
            "external_id": "10",
            "text": "Исторический пост",
            "comments_count": 2,
        }
    ]
    parser.fetch_comments = lambda post_id, count=None: [
        {
            "source": "vk",
            "external_id": 1,
            "post_external_id": str(post_id),
            "group_id": -123,
            "author_id": 42,
            "author_name": "Тест",
            "text": "Старый 1",
            "date": datetime(2026, 8, 29, 12, 0),
            "likes": 0,
            "reply_to": None,
        },
        {
            "source": "vk",
            "external_id": 2,
            "post_external_id": str(post_id),
            "group_id": -123,
            "author_id": 42,
            "author_name": "Тест",
            "text": "Старый 2",
            "date": datetime(2026, 8, 29, 12, 1),
            "likes": 0,
            "reply_to": None,
        },
    ]

    snapshot = parser.get_existing_comments_for_backfill(3)

    assert snapshot is not None
    assert [comment["external_id"] for comment in snapshot] == [1, 2]
    assert all(comment["post_text"] == "Исторический пост" for comment in snapshot)

    # Merely collecting the snapshot must not claim that the DB was populated.
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "db_backfill_complete" not in state

    parser.mark_db_backfill_complete(snapshot)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["db_backfill_complete"] is True
    assert set(state["comment_ids"]) == {"10:1", "10:2"}
    assert parser.needs_db_backfill is False


def test_existing_comments_backfill_does_not_complete_on_vk_error(tmp_path):
    state_file = tmp_path / "vk_parser_state.json"
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(state_file),
        notify_existing=False,
    )
    parser.fetch_posts = lambda count: [
        {"external_id": "10", "text": "Пост", "comments_count": 1}
    ]
    parser.fetch_comments = lambda post_id, count=None: None

    assert parser.get_existing_comments_for_backfill(3) is None
    assert parser.needs_db_backfill is True
    assert not state_file.exists()


def test_backfill_preserves_full_vk_post_text(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=False,
    )
    long_post = "Контекст поста " + ("очень важный для AI " * 20)
    parser.fetch_posts = lambda count: [
        {"external_id": "10", "text": long_post, "comments_count": 1}
    ]
    parser.fetch_comments = lambda post_id, count=None: [
        {
            "source": "vk",
            "external_id": 1,
            "post_external_id": str(post_id),
            "group_id": -123,
            "author_id": 42,
            "author_name": "Тест",
            "text": "Сколько игроков?",
            "date": datetime(2026, 8, 29, 12, 0),
            "likes": 0,
            "reply_to": None,
        }
    ]

    snapshot = parser.get_existing_comments_for_backfill(3)

    assert snapshot is not None
    assert snapshot[0]["post_text"] == long_post
    assert len(snapshot[0]["post_text"]) > 100


def test_new_vk_comment_preserves_full_parent_post_text(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
    )
    long_post = "Полный текст родительского поста " + ("с деталями игры " * 20)
    parser.fetch_posts = lambda count: [
        {"external_id": "10", "text": long_post, "comments_count": 1}
    ]
    parser.fetch_comments = lambda post_id, count=None: [
        {
            "source": "vk",
            "external_id": 99,
            "post_external_id": str(post_id),
            "group_id": -123,
            "author_id": 42,
            "author_name": "Тест",
            "text": "Когда доставка?",
            "date": datetime(2026, 8, 29, 12, 0),
            "likes": 0,
            "reply_to": None,
        }
    ]

    comments = parser.get_new_comments(3)

    assert len(comments) == 1
    assert comments[0]["post_text"] == long_post
    assert len(comments[0]["post_text"]) > 100


def test_fetch_comments_filters_own_vk_group_before_author_lookup(tmp_path):
    parser = VKParser(
        "token",
        "147452506",
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
    )

    parser._api_request = lambda method, params: {
        "response": {
            "count": 2,
            "items": [
                {
                    "id": 1,
                    "from_id": -147452506,
                    "date": 1,
                    "text": "Ответ от Фабрики игр",
                    "likes": {"count": 0},
                },
                {
                    "id": 2,
                    "from_id": 12345,
                    "date": 2,
                    "text": "Вопрос покупателя",
                    "likes": {"count": 0},
                },
            ],
        }
    }

    looked_up = []

    def fake_name(user_id):
        looked_up.append(user_id)
        return "Покупатель"

    parser._get_user_name = fake_name

    comments = parser.fetch_comments("58648")

    assert comments is not None
    assert [comment["external_id"] for comment in comments] == [2]
    assert looked_up == [12345]


def _normalized_comment(
    comment_id: int,
    text: str,
    *,
    author_id: int = 42,
    minute: int = 0,
    second: int = 0,
    post_id: str = "10",
    reply_to=None,
    sequence_index=None,
):
    row = {
        "source": "vk",
        "external_id": comment_id,
        "post_external_id": post_id,
        "group_id": -123,
        "author_id": author_id,
        "author_name": f"User {author_id}",
        "text": text,
        "text_original": text,
        "text_normalized": text,
        "date": datetime(2026, 9, 13, 12, minute, second),
        "likes": 0,
        "reply_to": reply_to,
    }
    if sequence_index is not None:
        row["_vk_sequence_index"] = sequence_index
    return row


def test_merge_consecutive_vk_comments_from_same_author(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
        merge_window_seconds=180,
    )
    comments = [
        _normalized_comment(
            1,
            "А когда Keyflower рассылать будете?",
            second=10,
            sequence_index=0,
        ),
        _normalized_comment(
            2,
            "Сказали, что вроде на этой неделе",
            second=35,
            sequence_index=1,
        ),
    ]

    merged = parser._merge_consecutive_user_comments(comments)

    assert len(merged) == 1
    assert merged[0]["external_id"] == 2
    assert merged[0]["text"] == (
        "А когда Keyflower рассылать будете?\n"
        "Сказали, что вроде на этой неделе"
    )
    assert merged[0]["merged_external_ids"] == [1, 2]
    assert merged[0]["merged_count"] == 2
    assert "_vk_sequence_index" not in merged[0]


def test_merge_does_not_cross_intervening_vk_comment(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
        merge_window_seconds=180,
    )
    comments = [
        _normalized_comment(1, "Первая часть", second=10, sequence_index=0),
        _normalized_comment(
            2,
            "Чужой комментарий",
            author_id=99,
            second=20,
            sequence_index=1,
        ),
        _normalized_comment(3, "Вторая часть", second=30, sequence_index=2),
    ]

    merged = parser._merge_consecutive_user_comments(comments)

    assert [row["external_id"] for row in merged] == [1, 2, 3]
    assert all(row.get("merged_count", 1) == 1 for row in merged)


def test_merge_does_not_cross_filtered_community_gap(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
        merge_window_seconds=180,
    )
    # Sequence index 1 represents a VK item that was filtered earlier (for
    # example, a reply from the community itself). The remaining user comments
    # must not be treated as truly consecutive.
    comments = [
        _normalized_comment(1, "До ответа группы", second=10, sequence_index=0),
        _normalized_comment(3, "После ответа группы", second=30, sequence_index=2),
    ]

    merged = parser._merge_consecutive_user_comments(comments)

    assert [row["external_id"] for row in merged] == [1, 3]


def test_merge_respects_time_window_and_reply_target(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
        merge_window_seconds=60,
    )
    comments = [
        _normalized_comment(1, "Первая", second=0, sequence_index=0),
        _normalized_comment(2, "Слишком поздно", minute=2, sequence_index=1),
        _normalized_comment(
            3,
            "Ответ в другой ветке",
            minute=2,
            second=10,
            reply_to=777,
            sequence_index=2,
        ),
    ]

    merged = parser._merge_consecutive_user_comments(comments)

    assert [row["external_id"] for row in merged] == [1, 2, 3]


def test_get_new_comments_merges_fragments_but_marks_every_vk_id_seen(tmp_path):
    state_file = tmp_path / "state.json"
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(state_file),
        notify_existing=True,
        merge_window_seconds=180,
    )
    parser.fetch_posts = lambda count: [
        {
            "external_id": "10",
            "text": "Пост про Keyflower",
            "comments_count": 2,
        }
    ]
    parser.fetch_comments = lambda post_id, count=None: [
        _normalized_comment(
            1,
            "А когда Keyflower рассылать будете?",
            second=10,
            post_id=str(post_id),
        ),
        _normalized_comment(
            2,
            "Сказали, что вроде на этой неделе",
            second=30,
            post_id=str(post_id),
        ),
    ]

    new_comments = parser.get_new_comments(3)

    assert len(new_comments) == 1
    assert new_comments[0]["external_id"] == 2
    assert new_comments[0]["post_text"] == "Пост про Keyflower"
    assert new_comments[0]["merged_external_ids"] == [1, 2]
    assert "А когда Keyflower" in new_comments[0]["text"]
    assert "Сказали, что вроде" in new_comments[0]["text"]

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(state["comment_ids"]) == {"10:1", "10:2"}


def test_fetch_comments_includes_thread_replies(tmp_path):
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        notify_existing=True,
    )

    calls = []

    def fake_api_request(method, params):
        calls.append(dict(params))
        assert method == "wall.getComments"
        if "comment_id" not in params:
            return {
                "response": {
                    "count": 4,
                    "current_level_count": 2,
                    "items": [
                        {
                            "id": 10,
                            "from_id": 42,
                            "date": 100,
                            "text": "Родитель",
                            "likes": {"count": 0},
                            "thread": {"count": 2, "items": []},
                        },
                        {
                            "id": 20,
                            "from_id": 77,
                            "date": 400,
                            "text": "Следующий верхний",
                            "likes": {"count": 0},
                            "thread": {"count": 0, "items": []},
                        },
                    ],
                }
            }
        assert params["comment_id"] == 10
        return {
            "response": {
                "count": 2,
                "items": [
                    {
                        "id": 11,
                        "from_id": 43,
                        "date": 200,
                        "text": "Первый ответ",
                        "reply_to_comment": 10,
                        "likes": {"count": 0},
                    },
                    {
                        "id": 12,
                        "from_id": 44,
                        "date": 300,
                        "text": "Ответ на ответ",
                        "reply_to_comment": 11,
                        "likes": {"count": 0},
                    },
                ],
            }
        }

    parser._api_request = fake_api_request
    parser._get_user_name = lambda user_id: f"User {user_id}"

    comments = parser.fetch_comments("777")

    assert comments is not None
    assert [row["external_id"] for row in comments] == [10, 11, 12, 20]
    assert comments[1]["reply_to"] == 10
    assert comments[2]["reply_to"] == 11
    assert any(call.get("comment_id") == 10 for call in calls)


def test_recent_comment_waits_across_poll_cycles_and_then_merges(tmp_path, monkeypatch):
    import src.vk_parser as vk_parser_module

    state_file = tmp_path / "state.json"
    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(state_file),
        notify_existing=True,
        merge_window_seconds=180,
    )
    parser.fetch_posts = lambda count: [
        {"external_id": "10", "text": "Пост про Keyflower", "comments_count": 2}
    ]

    first = _normalized_comment(
        1,
        "А когда Keyflower рассылать будете?",
        minute=0,
        second=10,
        post_id="10",
        sequence_index=0,
    )
    second = _normalized_comment(
        2,
        "Сказали, что вроде на этой неделе",
        minute=1,
        second=10,
        post_id="10",
        sequence_index=1,
    )
    current = [first]
    parser.fetch_comments = lambda post_id, count=None: list(current)

    base_ts = datetime(2026, 9, 13, 12, 0, 0).timestamp()

    # First fragment is only 20 seconds old: hold it and do not mark it seen.
    monkeypatch.setattr(vk_parser_module.time, "time", lambda: base_ts + 30)
    assert parser.get_new_comments(3) == []
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "comment_ids" not in state

    # Continuation arrives in the next poll. The whole group stays pending
    # because its newest fragment is still inside the 180-second window.
    current.append(second)
    monkeypatch.setattr(vk_parser_module.time, "time", lambda: base_ts + 120)
    assert parser.get_new_comments(3) == []

    # After the quiet window expires, both fragments are emitted once, merged.
    monkeypatch.setattr(vk_parser_module.time, "time", lambda: base_ts + 310)
    result = parser.get_new_comments(3)
    assert len(result) == 1
    assert result[0]["merged_external_ids"] == [1, 2]
    assert "А когда Keyflower" in result[0]["text"]
    assert "Сказали, что вроде" in result[0]["text"]

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(state["comment_ids"]) == {"10:1", "10:2"}


def test_api_request_retries_transient_timeout_and_then_succeeds(tmp_path, monkeypatch):
    import requests
    import src.vk_parser as vk_parser_module

    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        retry_attempts=3,
        retry_backoff_seconds=[0, 0],
    )
    calls = []

    class FakeResponse:
        def json(self):
            return {"response": {"items": [{"id": 1}]}}

    def fake_get(url, params, timeout):
        calls.append((url, dict(params), timeout))
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("temporary VK timeout")
        return FakeResponse()

    monkeypatch.setattr(vk_parser_module.requests, "get", fake_get)
    monkeypatch.setattr(vk_parser_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(vk_parser_module.random, "uniform", lambda _a, _b: 0)

    result = parser._api_request("wall.getComments", {"post_id": 777})

    assert "response" in result
    assert len(calls) == 2
    assert all(call[2] == 30 for call in calls)
    assert calls[0][1]["access_token"] == "token"
    assert calls[0][1]["v"] == "5.131"


def test_api_request_returns_transport_error_only_after_all_retries(tmp_path, monkeypatch):
    import requests
    import src.vk_parser as vk_parser_module

    parser = VKParser(
        "token",
        -123,
        delay=0,
        state_file=str(tmp_path / "state.json"),
        retry_attempts=3,
        retry_backoff_seconds=[0, 0],
    )
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, timeout))
        raise requests.exceptions.ReadTimeout("VK still unavailable")

    monkeypatch.setattr(vk_parser_module.requests, "get", fake_get)
    monkeypatch.setattr(vk_parser_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(vk_parser_module.random, "uniform", lambda _a, _b: 0)

    result = parser._api_request("users.get", {"user_ids": 42}, timeout=10)

    assert len(calls) == 3
    assert all(timeout == 10 for _, timeout in calls)
    assert result["error"]["transport_error"] is True
    assert result["error"]["attempts"] == 3
    assert "VK still unavailable" in result["error"]["error_msg"]
