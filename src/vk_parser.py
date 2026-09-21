# CPGames_Bot/vk_parser.py
import datetime
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from dotenv import load_dotenv

from .config import PARSER_STATE_FILE
from .normalizer import CommentNormalizer

load_dotenv()


class VKParser:
    """Poll VK wall posts and return comments that have not been seen before.

    On a fresh production state the parser snapshots already existing comments
    instead of notifying the SMM specialist about historical data. Set
    ``VK_NOTIFY_EXISTING=true`` only when replaying existing comments is desired.
    """

    def __init__(
        self,
        token: str,
        group_id: int,
        delay: float = 0.5,
        *,
        state_file: Optional[str] = None,
        notify_existing: Optional[bool] = None,
        merge_window_seconds: Optional[int] = None,
        retry_attempts: Optional[int] = None,
        retry_backoff_seconds: Optional[Sequence[float]] = None,
    ):
        self.access_token = token
        self.group_id = group_id
        self.api_url = "https://api.vk.com/method/"
        self.v = "5.131"
        self.base_delay = delay
        self._user_cache = {}

        if retry_attempts is None:
            try:
                retry_attempts = int(os.getenv("VK_API_RETRY_ATTEMPTS", "3"))
            except (TypeError, ValueError):
                retry_attempts = 3
        self.retry_attempts = max(1, int(retry_attempts))

        if retry_backoff_seconds is None:
            raw_backoff = os.getenv("VK_API_RETRY_BACKOFF_SECONDS", "2,5")
            parsed_backoff: List[float] = []
            for value in raw_backoff.split(","):
                try:
                    parsed_backoff.append(max(0.0, float(value.strip())))
                except (TypeError, ValueError):
                    continue
            retry_backoff_seconds = parsed_backoff or [2.0, 5.0]
        self.retry_backoff_seconds = [
            max(0.0, float(value)) for value in retry_backoff_seconds
        ] or [0.0]

        self.state_file = state_file or PARSER_STATE_FILE
        if notify_existing is None:
            notify_existing = os.getenv("VK_NOTIFY_EXISTING", "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        self.notify_existing = notify_existing

        if merge_window_seconds is None:
            try:
                merge_window_seconds = int(
                    os.getenv("VK_COMMENT_MERGE_WINDOW_SECONDS", "180")
                )
            except (TypeError, ValueError):
                merge_window_seconds = 180
        self.merge_window_seconds = max(0, int(merge_window_seconds))
        self.fetch_thread_replies = os.getenv(
            "VK_FETCH_THREAD_REPLIES", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}

        state = self._load_state_data()
        self.last_comment_id = state.get("last_comment_id", 0)
        self.saved_comment_ids = set(state.get("comment_ids", []))
        # The key existing in state distinguishes a real empty baseline from a
        # never-initialized state file.
        self._comment_state_initialized = "comment_ids" in state
        # Older polling deployments could mark historical comments as seen
        # without persisting them to the application database. This flag is set only after the
        # database backfill succeeds, allowing an already deployed state file
        # to self-heal after upgrading.
        self._db_backfill_complete = bool(state.get("db_backfill_complete", False))

    def _load_state_data(self) -> Dict[str, Any]:
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _load_state(self, key: str, default):
        return self._load_state_data().get(key, default)

    def _save_state(self, key: str, value):
        try:
            data = self._load_state_data()
            data[key] = value
            data["updated"] = datetime.datetime.now().isoformat()
            state_path = Path(self.state_file)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            os.replace(temp_path, state_path)
        except Exception as error:
            print(f"⚠️ Ошибка сохранения состояния: {error}")

    def _save_comment_ids(self) -> None:
        self._save_state("comment_ids", sorted(self.saved_comment_ids, key=str))
        self._comment_state_initialized = True

    @staticmethod
    def _comment_key(post_id: str, comment_id: Any) -> str:
        return f"{post_id}:{comment_id}"

    def _is_comment_seen(self, post_id: str, comment_id: Any) -> bool:
        key = self._comment_key(post_id, comment_id)
        # Older versions stored only comment_id. Keep compatibility with an
        # already deployed state while moving new entries to compound keys.
        return (
            key in self.saved_comment_ids
            or comment_id in self.saved_comment_ids
            or str(comment_id) in self.saved_comment_ids
        )

    def _mark_comment_seen(self, post_id: str, comment_id: Any) -> None:
        self.saved_comment_ids.add(self._comment_key(post_id, comment_id))

    @property
    def needs_db_backfill(self) -> bool:
        """Whether historical comments still need to be persisted to the database."""

        return not self._db_backfill_complete

    def mark_db_backfill_complete(self, comments: List[Dict[str, Any]]) -> None:
        """Persist seen IDs and record that the historical DB import finished."""

        for comment in comments:
            post_id = comment.get("post_external_id")
            comment_id = comment.get("external_id")
            if post_id is None or comment_id is None:
                continue
            self._mark_comment_seen(str(post_id), comment_id)

        self._save_comment_ids()
        self._save_state("db_backfill_complete", True)
        self._db_backfill_complete = True

    def get_existing_comments_for_backfill(
        self, posts_count: int = 20
    ) -> Optional[List[Dict[str, Any]]]:
        """Return a complete snapshot of current comments for a database backfill.

        The snapshot deliberately ignores the seen-ID state. It is used only to
        repair/populate the web panel database and must be saved with Telegram
        notifications disabled. ``None`` means the snapshot was incomplete and
        must be retried later.
        """

        print(f"🗃️ Подготовка исторических комментариев для веб-панели ({posts_count} постов)...")
        posts = self.fetch_posts(posts_count)
        if not posts:
            print("⚠️ Backfill не выполнен: посты VK не получены")
            return None

        snapshot: List[Dict[str, Any]] = []
        for post in posts:
            post_id = post["external_id"]
            print(
                f"\n📄 Backfill поста #{post_id} "
                f"(комментариев: {post['comments_count']})"
            )

            if post["comments_count"] == 0:
                continue

            comments = self.fetch_comments(post_id)
            if comments is None:
                print(
                    "⚠️ Backfill прерван: не удалось получить полный список "
                    f"комментариев поста #{post_id}"
                )
                return None

            # Preserve the full VK post for intent-aware AI routing.  main.py
            # applies its own bounded prompt limit, so truncating to 100 chars
            # here only destroys useful context.
            post_text = post.get("text", "")
            for comment in comments:
                comment["post_text"] = post_text
                snapshot.append(comment)

        print(f"🗃️ Для backfill собрано комментариев: {len(snapshot)}")
        return snapshot

    def _retry_backoff_for_attempt(self, attempt: int) -> float:
        """Return the delay before the next retry after ``attempt`` failed."""

        index = min(max(attempt - 1, 0), len(self.retry_backoff_seconds) - 1)
        return self.retry_backoff_seconds[index]

    def _api_request(
        self, method: str, params: Dict, *, timeout: float = 30
    ) -> Dict:
        """Call VK API with bounded retries for transient transport failures.

        VK occasionally stalls long enough to hit the HTTP read timeout. A
        single timeout used to abort the current post/thread until the next
        polling cycle. Retry the same request up to three times by default,
        with configurable short backoff delays. VK logical/API errors are
        returned immediately and are not blindly retried.
        """

        request_params = dict(params)
        request_params["access_token"] = self.access_token
        request_params["v"] = self.v
        last_error: Optional[Exception] = None

        for attempt in range(1, self.retry_attempts + 1):
            time.sleep(self.base_delay + random.uniform(0, 0.5))
            try:
                response = requests.get(
                    f"{self.api_url}{method}",
                    params=request_params,
                    timeout=timeout,
                )
                return response.json()
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt >= self.retry_attempts:
                    break

                backoff = self._retry_backoff_for_attempt(attempt)
                print(
                    f"⚠️ VK API {method}: попытка {attempt}/{self.retry_attempts} "
                    f"не удалась: {error}; повтор через {backoff:g} с"
                )
                if backoff > 0:
                    time.sleep(backoff)

        return {
            "error": {
                "error_msg": str(last_error or "Неизвестная ошибка VK API"),
                "transport_error": True,
                "attempts": self.retry_attempts,
            }
        }

    def fetch_posts(self, count: int = 20) -> List[Dict[str, Any]]:
        print(f"📥 Запрос последних {count} постов...")
        params = {
            "owner_id": self.group_id,
            "count": count,
            "filter": "all",
        }
        data = self._api_request("wall.get", params)

        if "error" in data:
            error = data["error"]
            print(f"❌ Ошибка VK API: {error.get('error_msg', 'Неизвестная ошибка')}")
            return []

        items = data.get("response", {}).get("items", [])
        print(f"📊 Найдено постов: {len(items)}")

        posts = []
        for item in items:
            posts.append(
                {
                    "source": "vk",
                    "external_id": str(item.get("id")),
                    "group_id": self.group_id,
                    "date": datetime.datetime.fromtimestamp(item.get("date", 0)),
                    "text": item.get("text", ""),
                    "url": f"https://vk.com/wall{self.group_id}_{item.get('id')}",
                    "likes": item.get("likes", {}).get("count", 0),
                    "comments_count": item.get("comments", {}).get("count", 0),
                    "is_pinned": item.get("is_pinned", False),
                }
            )
        return posts

    def _fetch_comment_thread(
        self,
        post_id: str,
        parent_comment_id: Any,
        expected_count: Optional[int] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch every reply from one VK comment thread.

        ``wall.getComments`` returns only top-level comments by default while
        ``response.count`` may include replies.  Passing ``comment_id`` asks VK
        for the replies of that comment.  Paginate independently so threads
        longer than 100 replies are not truncated.
        """

        page_size = 100
        offset = 0
        replies: List[Dict[str, Any]] = []

        while True:
            params = {
                "owner_id": self.group_id,
                "post_id": post_id,
                "comment_id": parent_comment_id,
                "count": page_size,
                "offset": offset,
                "sort": "asc",
            }
            data = self._api_request("wall.getComments", params)
            if "error" in data:
                error = data["error"]
                print(
                    f"   ⚠️ Ошибка ответов к комментарию {parent_comment_id}: "
                    f"{error.get('error_msg', 'Неизвестная ошибка')}"
                )
                return None

            batch = data.get("response", {}).get("items", []) or []
            for item in batch:
                # Keep the branch root as a fallback. VK normally supplies
                # reply_to_comment for thread items, but older payloads may not.
                item = dict(item)
                item.setdefault("_thread_parent_id", parent_comment_id)
                replies.append(item)

            offset += len(batch)
            if not batch or len(batch) < page_size:
                break
            if expected_count is not None and offset >= expected_count:
                break

        return replies

    def fetch_comments(
        self, post_id: str, count: Optional[int] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch top-level VK comments and, by default, all thread replies.

        VK returns at most 100 top-level comments per ``wall.getComments``
        request.  Replies live in separate threads and are fetched with
        ``comment_id`` when ``VK_FETCH_THREAD_REPLIES=true`` (default). ``None``
        is returned on any API error so a partial snapshot is never persisted.
        """

        page_size = 100
        offset = 0
        top_level_items: List[Dict[str, Any]] = []

        while True:
            remaining = None if count is None else max(count - len(top_level_items), 0)
            if remaining == 0:
                break
            request_count = page_size if remaining is None else min(page_size, remaining)
            params = {
                "owner_id": self.group_id,
                "post_id": post_id,
                "count": request_count,
                "offset": offset,
                "sort": "asc",
                # Do not rely on the inline preview: VK caps it at 10 items.
                # Full threads are fetched explicitly below.
                "thread_items_count": 0,
            }
            data = self._api_request("wall.getComments", params)

            if "error" in data:
                error = data["error"]
                print(
                    f"   ⚠️ Ошибка комментариев к посту {post_id}: "
                    f"{error.get('error_msg', 'Неизвестная ошибка')}"
                )
                return None

            batch = data.get("response", {}).get("items", []) or []
            top_level_items.extend(batch)
            offset += len(batch)

            if not batch or len(batch) < request_count:
                break

        raw_items: List[Dict[str, Any]] = list(top_level_items)
        reply_count = 0
        if self.fetch_thread_replies:
            for parent in top_level_items:
                thread = parent.get("thread") or {}
                try:
                    expected_replies = int(thread.get("count", 0) or 0)
                except (TypeError, ValueError):
                    expected_replies = 0
                if expected_replies <= 0:
                    continue

                replies = self._fetch_comment_thread(
                    post_id,
                    parent.get("id"),
                    expected_count=expected_replies,
                )
                if replies is None:
                    return None
                raw_items.extend(replies)
                reply_count += len(replies)

        # A defensive dedupe protects against API variants that may include a
        # small thread preview even when thread_items_count=0.
        unique_items: Dict[Any, Dict[str, Any]] = {}
        for item in raw_items:
            item_id = item.get("id")
            key = item_id if item_id is not None else id(item)
            unique_items[key] = item
        raw_items = list(unique_items.values())
        raw_items.sort(
            key=lambda item: (
                int(item.get("date", 0) or 0),
                int(item.get("id", 0) or 0),
            )
        )

        if reply_count:
            print(
                f"   💬 Найдено комментариев: {len(raw_items)} "
                f"(верхний уровень: {len(top_level_items)}, ответы: {reply_count})"
            )
        else:
            print(f"   💬 Найдено комментариев: {len(raw_items)}")

        comments: List[Dict[str, Any]] = []
        own_comments_skipped = 0
        for sequence_index, item in enumerate(raw_items):
            author_id = item.get("from_id")

            # Filter comments posted by the community itself at the parser
            # boundary, before author-name lookup and before the comment can
            # reach the database/LLM pipeline. VK represents community
            # ``from_id`` as a negative number, while VK_GROUP_ID may be
            # configured with or without the leading minus sign.
            try:
                author_numeric = int(str(author_id).strip())
                group_numeric = int(str(self.group_id).strip())
            except (TypeError, ValueError):
                author_numeric = group_numeric = 0

            if author_numeric < 0 and abs(author_numeric) == abs(group_numeric):
                own_comments_skipped += 1
                continue

            author_name = self._get_user_name(author_id)

            raw_comment = {
                "source": "vk",
                "external_id": item.get("id"),
                "post_external_id": str(post_id),
                "group_id": self.group_id,
                "author_id": author_id,
                "author_name": author_name,
                "text": item.get("text", ""),
                "date": datetime.datetime.fromtimestamp(item.get("date", 0)),
                "likes": item.get("likes", {}).get("count", 0),
                "reply_to": item.get(
                    "reply_to_comment",
                    item.get("_thread_parent_id"),
                ),
                # Internal-only ordering marker. It lets the merge step verify
                # that two user comments were truly consecutive in VK, even
                # when a community-authored comment between them is filtered
                # out before the AI pipeline.
                "_vk_sequence_index": sequence_index,
            }
            comments.append(CommentNormalizer.normalize_comment(raw_comment))

        if own_comments_skipped:
            print(
                f"   ⏩ Пропущено комментариев от собственной группы: "
                f"{own_comments_skipped}"
            )

        return comments

    @staticmethod
    def _same_merge_identity(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return (
            left.get("source") == right.get("source") == "vk"
            and str(left.get("post_external_id")) == str(right.get("post_external_id"))
            and str(left.get("group_id")) == str(right.get("group_id"))
            and str(left.get("author_id")) == str(right.get("author_id"))
            and str(left.get("reply_to")) == str(right.get("reply_to"))
        )

    def _comments_close_enough(
        self, left: Dict[str, Any], right: Dict[str, Any]
    ) -> bool:
        left_date = left.get("date")
        right_date = right.get("date")
        if not isinstance(left_date, datetime.datetime) or not isinstance(
            right_date, datetime.datetime
        ):
            return False
        delta = (right_date - left_date).total_seconds()
        return 0 <= delta <= self.merge_window_seconds

    @staticmethod
    def _comments_truly_consecutive(
        left: Dict[str, Any], right: Dict[str, Any]
    ) -> bool:
        try:
            return int(right.get("_vk_sequence_index")) == int(
                left.get("_vk_sequence_index")
            ) + 1
        except (TypeError, ValueError):
            # Tests/custom adapters may not provide the internal index. In
            # that case list adjacency is the best available signal.
            return True

    def _partition_ready_comments(
        self,
        comments: List[Dict[str, Any]],
        *,
        now_timestamp: Optional[float] = None,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Delay fresh comments so fragments can merge across polling cycles.

        Previously a fragment seen near the end of one poll was marked as seen
        immediately, so a continuation arriving seconds later in the next poll
        could never be merged.  We now hold the whole potential fragment group
        until the newest item is at least ``merge_window_seconds`` old.
        Deferred comments remain unseen and are fetched again next cycle.
        """

        if self.merge_window_seconds <= 0 or not comments:
            return comments, []

        now_timestamp = time.time() if now_timestamp is None else now_timestamp
        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = [comments[0]]

        for item in comments[1:]:
            previous = current[-1]
            if (
                self._same_merge_identity(previous, item)
                and self._comments_close_enough(previous, item)
                and self._comments_truly_consecutive(previous, item)
            ):
                current.append(item)
            else:
                groups.append(current)
                current = [item]
        groups.append(current)

        ready: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        for group in groups:
            newest_date = group[-1].get("date")
            if not isinstance(newest_date, datetime.datetime):
                ready.extend(group)
                continue
            age_seconds = now_timestamp - newest_date.timestamp()
            # Clock skew into the future should not hold a comment forever.
            is_fresh = 0 <= age_seconds < self.merge_window_seconds
            if is_fresh:
                deferred.extend(group)
            else:
                ready.extend(group)

        return ready, deferred

    def _merge_consecutive_user_comments(
        self, comments: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Merge rapid consecutive VK comments from the same author.

        Users often split one thought into two or more comments. Generating a
        separate AI answer for every fragment produces poor context and noisy
        Telegram notifications. We merge only when all safety conditions hold:

        * same post, group, source and author;
        * comments were truly consecutive in the VK response;
        * same reply target (normally both top-level comments);
        * the gap is within ``VK_COMMENT_MERGE_WINDOW_SECONDS``.

        The last VK comment becomes the anchor (external_id/date/url target),
        while its text is replaced with the combined message. All original IDs
        are marked as seen only when the group leaves the hold window.
        """

        if self.merge_window_seconds <= 0 or len(comments) < 2:
            return comments

        merged: List[Dict[str, Any]] = []
        current_group: List[Dict[str, Any]] = [comments[0]]

        def flush_group() -> None:
            nonlocal current_group
            if len(current_group) == 1:
                item = dict(current_group[0])
                item.pop("_vk_sequence_index", None)
                merged.append(item)
                current_group = []
                return

            anchor = dict(current_group[-1])
            normalized_parts = [
                str(item.get("text") or "").strip() for item in current_group
            ]
            original_parts = [
                str(item.get("text_original") or item.get("text") or "").strip()
                for item in current_group
            ]
            normalized_parts = [part for part in normalized_parts if part]
            original_parts = [part for part in original_parts if part]

            combined_text = "\n".join(normalized_parts).strip()
            combined_original = "\n".join(original_parts).strip()
            anchor["text"] = combined_text
            anchor["text_normalized"] = combined_text
            anchor["text_original"] = combined_original or combined_text
            anchor["merged_external_ids"] = [
                item.get("external_id") for item in current_group
            ]
            anchor["merged_count"] = len(current_group)
            anchor.pop("_vk_sequence_index", None)
            merged.append(anchor)
            current_group = []

        for item in comments[1:]:
            previous = current_group[-1]
            if (
                self._same_merge_identity(previous, item)
                and self._comments_close_enough(previous, item)
                and self._comments_truly_consecutive(previous, item)
            ):
                current_group.append(item)
            else:
                flush_group()
                current_group = [item]

        flush_group()

        merged_fragments = sum(
            max(0, int(item.get("merged_count", 1)) - 1) for item in merged
        )
        if merged_fragments:
            print(
                f"🧩 Объединено последовательных комментариев: "
                f"{merged_fragments}; логических сообщений: {len(merged)}"
            )

        return merged

    def _get_user_name(self, user_id: int) -> str:
        if not user_id:
            return "Unknown"
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        data = self._api_request(
            "users.get",
            {"user_ids": user_id},
            timeout=10,
        )
        if "error" in data:
            error = data["error"]
            print(
                f"⚠️ Ошибка получения имени пользователя {user_id}: "
                f"{error.get('error_msg', 'Неизвестная ошибка')}"
            )
            # Do not cache a transport-error fallback forever. A later polling
            # cycle may successfully resolve the real VK display name.
            return f"User {user_id}"

        if "response" in data and data["response"]:
            user = data["response"][0]
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            self._user_cache[user_id] = name
            return name

        self._user_cache[user_id] = f"User {user_id}"
        return self._user_cache[user_id]

    def get_new_comments(self, posts_count: int = 20) -> List[Dict[str, Any]]:
        print(f"🔍 Проверка последних {posts_count} постов...")
        print("-" * 60)

        posts = self.fetch_posts(posts_count)
        if not posts:
            print("📭 Посты не найдены")
            return []

        current_post_ids = [post["external_id"] for post in posts]
        self._save_state("last_post_ids", current_post_ids)

        baseline_mode = not self._comment_state_initialized and not self.notify_existing
        baseline_count = 0
        fetch_failed = False
        all_new_comments: List[Dict[str, Any]] = []

        if baseline_mode:
            print(
                "🧭 Первый запуск: существующие VK-комментарии будут запомнены "
                "без Telegram-уведомлений."
            )

        for post in posts:
            post_id = post["external_id"]
            print(
                f"\n📄 Проверка поста #{post_id} "
                f"(комментариев: {post['comments_count']})"
            )

            if post["comments_count"] == 0:
                print("   📭 Нет комментариев")
                continue

            comments = self.fetch_comments(post_id)
            if comments is None:
                fetch_failed = True
                continue

            if baseline_mode:
                for comment in comments:
                    comment_id = comment.get("external_id")
                    if comment_id is None:
                        continue
                    if not self._is_comment_seen(post_id, comment_id):
                        self._mark_comment_seen(post_id, comment_id)
                        baseline_count += 1
                continue

            unseen_comments: List[Dict[str, Any]] = []
            for comment in comments:
                comment_id = comment.get("external_id")
                if comment_id is None or self._is_comment_seen(post_id, comment_id):
                    continue
                # Keep the complete parent post; generate_ai_reply() bounds
                # the context before it is sent to the LLM.
                comment["post_text"] = post.get("text", "")
                unseen_comments.append(comment)

            ready_comments, deferred_comments = self._partition_ready_comments(
                unseen_comments
            )
            if deferred_comments:
                print(
                    f"   ⏳ Ожидаем возможное продолжение: "
                    f"{len(deferred_comments)} комментариев"
                )

            for comment in ready_comments:
                comment_id = comment.get("external_id")
                all_new_comments.append(comment)
                self._mark_comment_seen(post_id, comment_id)

        if baseline_mode:
            if fetch_failed:
                print(
                    "⚠️ Начальная синхронизация не завершена из-за ошибки VK API. "
                    "Состояние не зафиксировано; повторим на следующем цикле."
                )
                return []
            self._save_comment_ids()
            print(
                f"✅ Начальная синхронизация завершена: {baseline_count} "
                "существующих комментариев отмечены как уже просмотренные."
            )
            return []

        if all_new_comments:
            self._save_comment_ids()
            raw_new_count = len(all_new_comments)
            all_new_comments = self._merge_consecutive_user_comments(all_new_comments)
            logical_count = len(all_new_comments)
            if logical_count == raw_new_count:
                print(f"✅ Найдено {logical_count} новых комментариев")
            else:
                print(
                    f"✅ Найдено {raw_new_count} новых комментариев → "
                    f"{logical_count} логических сообщений"
                )
        else:
            print("📭 Новых комментариев нет")

        return all_new_comments
