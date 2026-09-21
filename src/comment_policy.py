"""Rules for comments that must not reach the SMM reply generator.

The project intentionally keeps these checks deterministic. They are applied
before the LLM so obvious service/brand comments and content-free reactions do
not create noisy Telegram tasks or fabricated replies.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any


def _normalize(value: Any) -> str:
    return re.sub(r"[^\w\s+]+", " ", str(value or "").lower(), flags=re.UNICODE).strip()


def _brand_aliases() -> set[str]:
    configured = os.getenv("OWN_BRAND_NAMES", "")
    values = {
        "фабрика игр",
        "фабрика игры",
        "cpgames",
        "cp games",
    }
    values.update(part.strip().lower() for part in configured.split(",") if part.strip())
    return {_normalize(value) for value in values if _normalize(value)}


def _normalize_username(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^https?://(?:www\.)?t\.me/", "", text)
    return text.lstrip("@").split("/", 1)[0].strip()


def _env_values(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _iter_values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    return (value,)


def _telegram_numeric_id(value: Any) -> str:
    """Return an unmarked Telegram peer id for comparison.

    Telethon can expose the same channel as raw ``123456`` or marked
    ``-100123456`` (and legacy chats as ``-123456``). Comparing the canonical
    numeric part lets us match a comment sender against channel/discussion ids
    without depending on which representation a particular API object uses.
    """

    text = str(value or "").strip()
    if not re.fullmatch(r"-?\d+", text):
        return ""
    if text.startswith("-100") and len(text) > 4:
        return text[4:]
    return text[1:] if text.startswith("-") else text


def _own_brand_usernames(extra: Any = None) -> set[str]:
    values = _env_values("OWN_BRAND_USERNAMES")
    telegram_channel = os.getenv("TELEGRAM_CHANNEL", "")
    if telegram_channel:
        values.append(telegram_channel)
    values.extend(_iter_values(extra))
    return {username for value in values if (username := _normalize_username(value))}


def _own_telegram_ids(extra: Any = None) -> set[str]:
    values: list[Any] = list(_env_values("OWN_TELEGRAM_IDS"))
    values.extend(_iter_values(extra))
    return {peer_id for value in values if (peer_id := _telegram_numeric_id(value))}


def is_own_brand_comment(
    *,
    source: str = "",
    author_id: Any = None,
    author_name: Any = None,
    author_username: Any = None,
    author_entity_id: Any = None,
    author_peer_type: Any = None,
    group_id: Any = None,
    own_author_ids: Any = None,
    own_usernames: Any = None,
) -> bool:
    """Return True for comments posted by the project/company itself.

    VK primarily uses ``from_id == owner/group_id``. Telegram is checked by
    channel/discussion peer ids first and by username second; display-name
    aliases remain only as a fallback. This is important for comments posted
    "as channel/group", whose visible title may differ from the configured
    brand name.
    """

    src = str(source or "").strip().lower()
    author_id_text = str(author_id or "").strip()
    group_id_text = str(group_id or "").strip()

    if src == "vk" and author_id_text and group_id_text:
        # VK group comments use a negative ``from_id`` (for example
        # ``-147452506``), while deployments sometimes configure
        # ``VK_GROUP_ID`` as either ``-147452506`` or ``147452506``.  Compare
        # the canonical community id instead of raw strings so an own-brand
        # comment can never slip through merely because the sign differs.
        try:
            author_numeric = int(author_id_text)
            group_numeric = int(group_id_text)
        except (TypeError, ValueError):
            author_numeric = group_numeric = 0

        if author_numeric < 0 and abs(author_numeric) == abs(group_numeric):
            return True
        if author_id_text == group_id_text:
            return True

    if src == "telegram":
        username = _normalize_username(author_username)
        if username and username in _own_brand_usernames(own_usernames):
            return True

        own_ids = _own_telegram_ids(own_author_ids)
        if group_id_text and _telegram_numeric_id(group_id_text):
            own_ids.add(_telegram_numeric_id(group_id_text))

        sender_ids = {
            peer_id
            for value in (author_id, author_entity_id)
            if (peer_id := _telegram_numeric_id(value))
        }
        peer_type = str(author_peer_type or "").strip().lower()
        sender_is_group_peer = (
            peer_type in {"channel", "chat", "group", "supergroup"}
            or author_id_text.startswith("-")
        )
        if sender_is_group_peer and own_ids and sender_ids.intersection(own_ids):
            return True

    name = _normalize(author_name)
    if not name:
        return False

    aliases = _brand_aliases()
    return name in aliases or any(
        len(alias) >= 6 and (name.startswith(alias + " ") or name.endswith(" " + alias))
        for alias in aliases
    )


# Short acknowledgement/noise phrases that do not contain a request and do not
# need an SMM reply. Positive feedback such as "спасибо" / "супер" is NOT in
# this list because a short friendly answer is useful there.
_NOISE_PHRASES = {
    "во",
    "во во",
    "вово",
    "ву",
    "ву во",
    "ву ву",
    "ага",
    "угу",
    "мгм",
    "да",
    "да да",
    "точно",
    "именно",
    "согласен",
    "согласна",
    "плюс один",
    "+1",
    "ок",
    "окей",
    "понятно",
    "ясно",
    "хм",
    "мм",
    "лол",
}

_NOISE_TOKENS = {
    "во", "ву", "ага", "угу", "мгм", "да", "точно", "именно", "ок", "окей",
    "понятно", "ясно", "хм", "мм", "лол", "+1",
}


def is_uninformative_comment(text: Any) -> bool:
    """Detect comments where there is no actionable or reply-worthy intent.

    The rule is deliberately conservative: questions, complaints, product
    terms, gratitude and praise are left for normal routing. We only suppress
    empty/punctuation-only messages and a narrow set of acknowledgement/noise
    phrases such as the test case ``"Во, во!"``.
    """

    raw = str(text or "").strip()
    normalized = _normalize(raw)
    if not normalized:
        return True

    # A question mark is an explicit request signal even for very short text.
    if "?" in raw:
        return False

    if normalized in _NOISE_PHRASES:
        return True

    tokens = normalized.split()
    if 1 <= len(tokens) <= 3 and all(token in _NOISE_TOKENS for token in tokens):
        return True

    # Suppress symbols/numbers with no actual word content (e.g. "++" / "123").
    if not re.search(r"[A-Za-zА-Яа-яЁё]", raw):
        return True

    return False


def get_ignore_reason(
    text: Any,
    *,
    source: str = "",
    author_id: Any = None,
    author_name: Any = None,
    author_username: Any = None,
    author_entity_id: Any = None,
    author_peer_type: Any = None,
    group_id: Any = None,
    own_author_ids: Any = None,
    own_usernames: Any = None,
) -> str | None:
    """Return a stable reason code when a comment should be ignored."""

    if is_own_brand_comment(
        source=source,
        author_id=author_id,
        author_name=author_name,
        author_username=author_username,
        author_entity_id=author_entity_id,
        author_peer_type=author_peer_type,
        group_id=group_id,
        own_author_ids=own_author_ids,
        own_usernames=own_usernames,
    ):
        return "own_brand"
    if is_uninformative_comment(text):
        return "uninformative"
    return None
