import asyncio
import datetime
import html
import os
import re
import threading
import time
from typing import Optional

import gspread
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google.oauth2.service_account import Credentials
from sqlalchemy import or_
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from .comment_policy import get_ignore_reason, is_uninformative_comment
from .config import TEMPLATES_DIR
from .models import Base, Comment, Post, Prompt, SessionLocal, engine
from .vk_parser import VKParser

load_dotenv()

app = FastAPI(title="CPGames Агрегатор VK")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Настройки из .env
VK_ACCESS_TOKEN = os.getenv("VK_ACCESS_TOKEN")
VK_GROUP_ID = os.getenv("VK_GROUP_ID")
POSTS_COUNT = int(os.getenv("POSTS_COUNT", 20))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
TG_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    or os.getenv("TG_BOT_TOKEN", "").strip()
)
ADMIN_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID", "").strip()
    or os.getenv("ADMIN_CHAT_ID", "").strip()
)
BACKGROUND_WORKERS_ENABLED = os.getenv(
    "BACKGROUND_WORKERS_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# Telegram reminder safety controls. CHECK_INTERVAL controls how often parsers
# poll for new comments; it must not also become the reminder frequency.
TELEGRAM_REMINDER_INTERVAL = max(
    0, int(os.getenv("TELEGRAM_REMINDER_INTERVAL", "3600"))
)
TELEGRAM_NOTIFICATION_BATCH_LIMIT = max(
    1, int(os.getenv("TELEGRAM_NOTIFICATION_BATCH_LIMIT", "20"))
)
APP_STARTED_AT = datetime.datetime.now()
LAST_REMINDER_BATCH_AT = APP_STARTED_AT

GOOGLE_SHEETS_CREDENTIALS = os.getenv(
    "GOOGLE_SHEETS_CREDENTIALS", "credentials.json")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_SHEETS_RANGE = os.getenv("GOOGLE_SHEETS_RANGE", "A:G")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Таблицы базы данных проверены/созданы")
    except Exception as e:
        print(f"❌ Критическая ошибка при создании таблиц БД: {e}")
        raise

# ================= RAG ПОИСК ПО БАЗЕ ЗНАНИЙ =================


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[^\w\s]", " ", (value or "").lower()).strip()


# Informal Russian shorthand frequently used in VK comments for add-ons.
# Keep it narrow enough not to match unrelated words beginning with "доп".
_ADDON_SHORTHAND_RE = r"\bдоп(?:ы|а|у|ом|е|ов|ам|ами|ах)?\b"
_DELIVERY_MAILING_RE = r"\bрассыл\w*\b"


def _is_addon_term(value: str) -> bool:
    term = _normalize_search_text(value)
    return bool(
        re.fullmatch(r"доп(?:ы|а|у|ом|е|ов|ам|ами|ах)?", term)
        or term.startswith("дополн")
    )


def _mentions_promo_code(text: str) -> bool:
    """Recognize common promo-code slang without confusing "я промок"."""
    return (
        _matches_any(text, r"\bпромокод\w*\b", r"\bпромо\b")
        or _contains_phrase(
            text,
            "нет промок",
            "есть промок",
            "ввести промок",
            "применить промок",
            "промок не работает",
            "промок на скидку",
            "промок для",
        )
    )


def _mentions_delivery_mailing(text: str) -> bool:
    """Recognize shipment/distribution forms like ``рассылка``/``рассылать``.

    Keep obvious newsletter/subscription wording out of the delivery route so
    ``подписаться на рассылку новостей`` is not treated as order shipping.
    """
    if not _matches_any(text, _DELIVERY_MAILING_RE):
        return False
    return not _contains_phrase(
        text,
        "рассылка новостей",
        "рассылку новостей",
        "подписаться на рассылку",
        "подписка на рассылку",
        "email рассыл",
        "e mail рассыл",
        "почтовая рассылка",
        "почтовую рассылку",
    )


def _cell_text(value) -> str:
    """Normalize spreadsheet cells from gspread/XLSX-like adapters."""
    return "" if value is None else str(value).strip()


_FAQ_FIELDS = {
    "question",
    "keywords",
    "content",
    "project_status",
    "sent",
    "not_sent",
    "next_step",
    "updated_at",
    "internal_note",
    "content_type",
    "is_active",
}


def _parse_boolish(value: str, default=True) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"0", "false", "no", "нет", "off", "inactive"}:
        return False
    if text in {"1", "true", "yes", "да", "on", "active"}:
        return True
    return default


def _split_aliases(value: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[;,\n]+", str(value or ""))
        if part.strip()
    ]


def _parse_base_game_data(all_values) -> dict[str, str]:
    """Read only the base key/value block before updates/add-ons/FAQ sections."""
    data: dict[str, str] = {}
    for row in all_values or []:
        field = _cell_text(row[0] if len(row) > 0 else "")
        if not field or field in {"Поле", "id"}:
            continue
        lowered = field.lower()
        if (
            field.startswith(("📰", "📦", "📚"))
            or "project_updates" in lowered
            or lowered == "add-ons"
            or "knowledge_base" in lowered
            or "оперативная база" in lowered
        ):
            break
        value = _cell_text(
            row[2] if len(row) >= 3 else (row[1] if len(row) >= 2 else "")
        )
        if field and value:
            data[field] = value
    return data


def _parse_faq_records(all_values) -> list[dict[str, str]]:
    """Parse repeated vertical FAQ records from a project worksheet.

    Supported record fields are intentionally compatible with the old
    ``question/content/content_type/is_active`` layout while adding the new
    operational fields used for problematic/new projects.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_faq = False

    def finish_current():
        nonlocal current
        if current and current.get("question"):
            records.append(current)
        current = None

    for row in all_values or []:
        field_raw = _cell_text(row[0] if len(row) > 0 else "")
        lowered = field_raw.lower()
        if (
            field_raw.startswith("📚")
            or "knowledge_base" in lowered
            or "faq / оперативная база" in lowered
        ):
            finish_current()
            in_faq = True
            continue
        if not in_faq:
            continue

        field = lowered
        if field == "question":
            finish_current()
            value = _cell_text(row[2] if len(row) >= 3 else "")
            current = {"question": value} if value else None
            continue

        if current is None or field not in _FAQ_FIELDS:
            continue
        value = _cell_text(row[2] if len(row) >= 3 else "")
        current[field] = value

    finish_current()
    return records


def _meaningful_terms(value: str) -> list[str]:
    stop_words = {
        "как", "что", "где", "когда", "почему", "зачем", "можно", "нужно",
        "будет", "есть", "это", "этот", "эта", "для", "работает", "работать",
        "делать", "если", "там", "тут", "про", "или", "уже", "еще", "ещё",
    }
    return [
        word for word in _normalize_search_text(value).split()
        if len(word) > 3 and word not in stop_words
    ]


def _terms_related(left: str, right: str) -> bool:
    if left == right:
        return True
    # Treat informal forms ("допы", "допах", "допов") as the same
    # concept as the formal "дополнение" used in the knowledge base.
    if _is_addon_term(left) and _is_addon_term(right):
        return True
    return len(left) >= 5 and len(right) >= 5 and left[:5] == right[:5]


def _game_identifiers(worksheet_title: str, game_data: dict[str, str]) -> list[str]:
    values = [worksheet_title, game_data.get("name", "")]
    values.extend(_split_aliases(game_data.get("aliases", "")))
    result = []
    seen = set()
    for value in values:
        cleaned = _normalize_search_text(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _game_token_related(left: str, right: str) -> bool:
    """Loose token match for common Russian case endings in game titles."""
    if left == right:
        return True
    shortest = min(len(left), len(right))
    if shortest >= 5:
        return left[:4] == right[:4]
    if shortest >= 4:
        return left[:3] == right[:3]
    return False


def _game_matches_context(identifiers: list[str], combined_clean: str) -> bool:
    context_tokens = [
        token for token in combined_clean.split()
        if len(token) > 2
    ]
    for identifier in identifiers:
        if identifier and identifier in combined_clean:
            return True
        tokens = [token for token in identifier.split() if len(token) > 2]
        if tokens:
            matched = sum(
                any(_game_token_related(token, ctx) for ctx in context_tokens)
                for token in tokens
            )
            if matched >= max(1, min(2, len(tokens))):
                return True
    return False


def _looks_operational_query(comment_clean: str) -> bool:
    return _matches_any(
        comment_clean,
        r"\bстатус\w*\b",
        r"\bсрок\w*\b",
        r"\bдостав\w*\b",
        r"\bотправ\w*\b",
        r"\bрегион\w*\b",
        r"\bпредзаказ\w*\b",
        r"\bзадерж\w*\b",
        r"\bтираж\w*\b",
        r"\bпарт(?:ия|ии|ий)\b",
        r"\bобещал\w*\b",
        r"\bинформац\w*\b",
        r"\bновост\w*\b",
        r"\bждат\w*\b",
        r"\bпланир\w*\b",
        r"\bволн\w*\b",
        r"\bкороб\w*\b",
        r"\bподдержк\w*\b",
        _ADDON_SHORTHAND_RE,
    ) or _mentions_delivery_mailing(comment_clean) or _contains_phrase(
        comment_clean,
        "когда ждать",
        "сколько прошло",
        "что отправлено",
        "что уже отправили",
        "что не отправлено",
        "когда появится информация",
    )


def _operational_topics(value: str) -> set[str]:
    """Return coarse topics used to keep project-status FAQ matches relevant.

    A project may have several operational records (for example shipping and
    future add-ons).  Matching every operational record merely because the game
    title is known can feed the LLM unrelated facts.  These topic buckets keep
    delivery questions on delivery records and add-on questions on add-on
    records while still allowing broad project-status records to answer status
    and pre-order delay questions.
    """
    text = _normalize_search_text(value)
    topics: set[str] = set()

    if _matches_any(
        text,
        r"\bдостав\w*\b",
        r"\bотправ\w*\b",
        r"\bрегион\w*\b",
        r"\bтрек\w*\b",
        r"\bпосыл\w*\b",
    ) or _mentions_delivery_mailing(text) or _contains_phrase(text, "когда ждать", "что отправлено", "что не отправлено"):
        topics.add("delivery")

    if _matches_any(
        text,
        r"\bдополнени\w*\b",
        r"\bаддон\w*\b",
        r"\bкороб\w*\b",
        r"\bволн\w*\b",
        r"\bподдержк\w*\b",
        r"\bexpansion\w*\b",
        r"\baddon\w*\b",
        _ADDON_SHORTHAND_RE,
    ):
        topics.add("addons")

    if _matches_any(
        text,
        r"\bстатус\w*\b",
        r"\bсрок\w*\b",
        r"\bпредзаказ\w*\b",
        r"\bзадерж\w*\b",
        r"\bтираж\w*\b",
        r"\bпроизвод\w*\b",
        r"\bобещал\w*\b",
        r"\bинформац\w*\b",
        r"\bновост\w*\b",
    ) or _contains_phrase(text, "сколько прошло", "что по проекту"):
        topics.add("status")

    if _matches_any(text, r"\bрелиз\w*\b", r"\bвыход\w*\b", r"\bвыйд\w*\b"):
        topics.add("release")

    if _matches_any(text, r"\bналичи\w*\b", r"\bпродаж\w*\b") or _contains_phrase(
        text, "где купить", "как купить", "есть в наличии"
    ):
        topics.add("availability")

    return topics


def _is_operational_record(record: dict[str, str]) -> bool:
    content_type = record.get("content_type", "").strip().lower()
    return content_type in {
        "operational_faq", "operational", "project_status", "status"
    } or any(
        record.get(key, "").strip()
        for key in ("project_status", "sent", "not_sent", "next_step", "updated_at")
    )


def _operational_record_has_facts(record: dict[str, str]) -> bool:
    """An enabled operational record must contain something usable for an answer."""
    return any(
        record.get(key, "").strip()
        for key in ("content", "project_status", "sent", "not_sent", "next_step")
    )


def _operational_record_is_verified(record: dict[str, str]) -> bool:
    """Reject obvious draft/manual-review placeholders even if TRUE was set by mistake."""
    updated = record.get("updated_at", "").strip().lower()
    if any(
        marker in updated
        for marker in (
            "требует ручной проверки",
            "требуется ручная проверка",
            "проверить smm",
            "проверки smm",
            "черновик",
            "draft",
        )
    ):
        return False
    return True


def _operational_record_relevant(
    record: dict[str, str],
    comment_clean: str,
    *,
    game_match: bool,
) -> bool:
    # Operational rows live on a project worksheet.  Without a project match,
    # using one would leak another game's status into a generic comment.
    if not game_match or not _looks_operational_query(comment_clean):
        return False

    query_topics = _operational_topics(comment_clean)
    record_topics = _operational_topics(
        f"{record.get('question', '')} {record.get('keywords', '')}"
    )
    if query_topics and record_topics:
        return bool(query_topics & record_topics)

    # Backward-compatible fallback for older operational rows whose wording does
    # not map cleanly to a topic bucket.
    record_terms = _meaningful_terms(
        f"{record.get('question', '')} {record.get('keywords', '')}"
    )
    comment_terms = _meaningful_terms(comment_clean)
    matched = {
        q_term
        for q_term in record_terms
        if any(_terms_related(q_term, c_term) for c_term in comment_terms)
    }
    required = 1 if len(record_terms) <= 1 else 2
    return bool(record_terms and len(matched) >= required)


def _faq_match_score(
    record: dict[str, str],
    comment_clean: str,
    *,
    game_match: bool,
) -> int:
    if not _parse_boolish(record.get("is_active", ""), default=True):
        return 0

    is_operational = _is_operational_record(record)

    record_terms = _meaningful_terms(
        f"{record.get('question', '')} {record.get('keywords', '')}"
    )
    comment_terms = _meaningful_terms(comment_clean)
    matched = {
        q_term
        for q_term in record_terms
        if any(_terms_related(q_term, c_term) for c_term in comment_terms)
    }
    required = 1 if len(record_terms) <= 1 else 2
    lexical_match = bool(record_terms and len(matched) >= required)

    if is_operational:
        if not _operational_record_has_facts(record):
            return 0
        if not _operational_record_is_verified(record):
            return 0
        if not _operational_record_relevant(
            record,
            comment_clean,
            game_match=game_match,
        ):
            return 0
        return 100 + len(matched)

    if lexical_match:
        return 40 + len(matched) + (5 if game_match else 0)
    return 0


def _format_faq_context(record: dict[str, str], game_label: str) -> str:
    is_operational = _is_operational_record(record)

    if not is_operational:
        return (
            "ОТВЕТ ИЗ БАЗЫ ЗНАНИЙ:\n"
            f"{record.get('question', '').strip()}: {record.get('content', '').strip()}"
        )

    lines = [f"ОПЕРАТИВНАЯ БАЗА ПО ПРОЕКТУ: {game_label}"]
    mapping = [
        ("question", "Сценарий/вопрос"),
        ("project_status", "Актуальный статус"),
        ("sent", "Уже отправлено/выполнено"),
        ("not_sent", "Ещё не отправлено/не выполнено"),
        ("next_step", "Следующий шаг/ориентир"),
        ("updated_at", "Актуально на"),
        ("content", "Публичная формулировка ответа"),
        ("internal_note", "Внутренняя инструкция SMM — не цитировать дословно"),
    ]
    for key, label in mapping:
        value = record.get(key, "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _build_structured_game_context(game_data: dict[str, str], comment_clean: str) -> str | None:
    game_name = game_data.get("name", "")
    fields_to_include = ["name", "aliases"]

    asks_price = (
        _matches_any(comment_clean, r"\bцен\w*\b", r"\bстоимост\w*\b", r"\bсто(?:ит|ят)\b")
        or _contains_phrase(comment_clean, "сколько стоит")
    )
    asks_delivery = (
        _matches_any(
            comment_clean,
            r"\bдостав\w*\b", r"\bотправ\w*\b", r"\bтрек(?:-?номер)?\w*\b",
            r"\bпосыл\w*\b", r"\bкурьер\w*\b", r"\bрегион\w*\b",
        )
        or _mentions_delivery_mailing(comment_clean)
        or _contains_phrase(
            comment_clean,
            "когда получу", "когда получим", "получить заказ", "получу заказ",
            "не получил заказ", "получить посылку", "срок доставки", "срок отправки",
            "когда ждать",
        )
    )
    asks_release = (
        _matches_any(comment_clean, r"\bрелиз\w*\b", r"\bвыйд(?:ет|ут|я)\w*\b", r"\bвыход\w*\b")
        or _contains_phrase(comment_clean, "дата выхода", "когда выйдет", "когда релиз")
    )
    asks_availability = (
        _matches_any(comment_clean, r"\bналичи\w*\b", r"\bпродаж\w*\b")
        or _contains_phrase(comment_clean, "где купить", "как купить", "можно купить", "как заказать", "есть в наличии")
    )
    asks_addons = (
        _matches_any(comment_clean, r"\bдополнени\w*\b", r"\bаддон\w*\b", _ADDON_SHORTHAND_RE, r"\bкороб\w*\b", r"\bволн\w*\b", r"\bподдержк\w*\b")
        or _contains_phrase(comment_clean, "новые коробки", "новых коробок", "следующая волна")
    )
    asks_status = _matches_any(
        comment_clean,
        r"\bстатус\w*\b", r"\bобещал\w*\b", r"\bинформац\w*\b", r"\bновост\w*\b",
        r"\bпредзаказ\w*\b", r"\bзадерж\w*\b",
    ) or _contains_phrase(comment_clean, "сколько прошло", "что по проекту")

    if asks_price:
        fields_to_include += ["base_price", "preorder_price", "price", "Late_backer_price"]
        fields_to_include += [
            key for key in game_data
            if ("цена" in key.lower() or "price" in key.lower())
        ]
    if asks_delivery:
        fields_to_include += ["delivery_estimate", "delivery", "доставка", "project_status"]
    if any(word in comment_clean for word in ["игрок", "сколько человек", "вдвоем", "вдвоём", "двоих", "компания"]):
        fields_to_include += ["min_players", "max_players"]
    if any(word in comment_clean for word in ["врем", "минут", "длится", "час"]):
        fields_to_include += ["play_time_min", "play_time_max"]
    if any(word in comment_clean for word in ["язык", "англ", "русск", "локализация"]):
        fields_to_include += ["language", "localization_status"]
    if any(word in comment_clean for word in ["что за", "описание", "сюжет", "механик", "суть", "правил"]):
        fields_to_include += ["genre", "description"]

    if asks_release or asks_availability or asks_addons or asks_status:
        fragments = []
        if asks_release:
            fragments += ["релиз", "выход", "release", "sale", "продаж"]
        if asks_availability:
            fragments += ["налич", "availability", "stock", "продаж", "sale", "status", "статус"]
        if asks_addons:
            fragments += ["addon", "дополн", "expansion", "короб", "волн", "support", "поддерж"]
        if asks_status:
            fragments += ["status", "статус", "update", "новост", "срок", "delivery", "достав", "предзаказ"]
        for key in game_data:
            if any(fragment in key.lower() for fragment in fragments):
                fields_to_include.append(key)

    specific_question = any((asks_price, asks_delivery, asks_release, asks_availability, asks_addons, asks_status))
    if not specific_question and len(fields_to_include) <= 2:
        fields_to_include += [
            "genre", "min_players", "max_players", "base_price", "preorder_price",
            "Late_backer_price", "delivery_estimate",
        ]

    unique_fields = []
    for field in fields_to_include:
        if field not in unique_fields:
            unique_fields.append(field)

    context_lines = []
    for key in unique_fields:
        value = game_data.get(key, "")
        if not value:
            continue
        if key in {"description", "contents"} and len(value) > 300:
            value = value[:300] + "..."
        context_lines.append(f"{key}: {value}")

    # Explicit absence markers are important: they prevent the model from
    # inventing "new waves", add-ons or vague "soon" deadlines when neither the
    # post nor the knowledge base confirms them.
    relevant_non_identity = [line for line in context_lines if not line.startswith(("name:", "aliases:"))]
    if specific_question and not relevant_non_identity:
        if asks_addons:
            context_lines.append("confirmed_addons_or_new_boxes: НЕТ ПОДТВЕРЖДЁННОЙ ИНФОРМАЦИИ В БАЗЕ")
        elif asks_delivery:
            context_lines.append("confirmed_delivery_timing: НЕТ ПОДТВЕРЖДЁННОГО СРОКА В БАЗЕ")
        elif asks_release:
            context_lines.append("confirmed_release_timing: НЕТ ПОДТВЕРЖДЁННОГО СРОКА В БАЗЕ")
        elif asks_status:
            context_lines.append("confirmed_project_update: НЕТ АКТУАЛЬНОЙ ПОДТВЕРЖДЁННОЙ ЗАПИСИ В БАЗЕ")
        elif asks_availability:
            context_lines.append("confirmed_availability: НЕТ ПОДТВЕРЖДЁННОЙ ИНФОРМАЦИИ В БАЗЕ")

    if not context_lines and game_name:
        context_lines = [f"name: {game_name}"]
    return "ДАННЫЕ ПО ИГРЕ:\n" + "\n".join(context_lines) if context_lines else None


def get_knowledge_base_context(
    comment_text,
    post_text="",
    max_results=3,
    *,
    allow_game_context=True,
):
    """Return the best matching FAQ/operational/game context from Google Sheets.

    The new operational FAQ block is treated as the source of truth for
    problematic/new projects: it can contain current status, what has already
    been sent, what has not been sent, next step, freshness date and an internal
    SMM note.  Old FAQ rows remain fully compatible.
    """
    if not GOOGLE_SHEETS_ID or not os.path.exists(GOOGLE_SHEETS_CREDENTIALS):
        print(f"❌ Не найден файл {GOOGLE_SHEETS_CREDENTIALS} или ID таблицы.")
        return None

    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(
            GOOGLE_SHEETS_CREDENTIALS, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEETS_ID)

        comment_clean = _normalize_search_text(comment_text)
        post_clean = _normalize_search_text(post_text)
        combined_clean = f"{comment_clean} {post_clean}".strip()

        print(f"🔍 Ищем релевантные данные для: {comment_clean[:120] or '<пусто>'}")

        game_candidates: list[tuple[int, str]] = []
        generic_candidates: list[tuple[int, str]] = []
        structured_candidates: list[tuple[int, str]] = []
        inactive_operational_markers: list[tuple[int, str]] = []

        for worksheet in spreadsheet.worksheets():
            all_values = worksheet.get_all_values()
            if not all_values:
                continue

            game_data = _parse_base_game_data(all_values)
            worksheet_title = str(
                getattr(worksheet, "title", "") or game_data.get("name", "")
            ).strip()
            identifiers = _game_identifiers(worksheet_title, game_data)
            game_match = bool(
                allow_game_context
                and _game_matches_context(identifiers, combined_clean)
            )
            game_label = game_data.get("name") or worksheet_title or "Проект"

            for record in _parse_faq_records(all_values):
                if (
                    game_match
                    and _is_operational_record(record)
                    and (
                        not _parse_boolish(record.get("is_active", ""), default=True)
                        or not _operational_record_is_verified(record)
                    )
                    and _operational_record_relevant(
                        record,
                        comment_clean,
                        game_match=True,
                    )
                ):
                    marker = (
                        f"ОПЕРАТИВНАЯ БАЗА ПО ПРОЕКТУ: {game_label}\n"
                        "Статус оперативной записи: НЕ АКТИВНА / НЕ ПРОВЕРЕНА\n"
                        "Подтверждённой актуальной информации для ответа о текущем "
                        "статусе, сроках или отправке сейчас нет. "
                        "Не использовать старые сроки/статусы из общих полей как текущие."
                    )
                    inactive_operational_markers.append((90, marker))

                score = _faq_match_score(
                    record,
                    comment_clean,
                    game_match=game_match,
                )
                if score:
                    item = (score, _format_faq_context(record, game_label))
                    if game_match:
                        game_candidates.append(item)
                    else:
                        generic_candidates.append(item)

            if not game_match:
                continue

            context_text = _build_structured_game_context(game_data, comment_clean)
            if context_text:
                # Structured data is useful but verified operational FAQ has priority.
                structured_candidates.append((30, context_text))

        # For a game-related question, context from the matched project must win
        # over lexically similar FAQ rows from unrelated project sheets.
        if game_candidates:
            game_candidates.sort(key=lambda item: item[0], reverse=True)
            selected = game_candidates[: max(1, min(max_results, 2))]
            context = "\n\n".join(item_text for _, item_text in selected)
            print(f"📄 Найден FAQ/оперативный контекст:\n{context}")
            return context

        # An explicitly disabled operational row means SMM has not yet verified
        # the current facts.  Do not fall back to potentially stale dynamic
        # fields such as old delivery estimates.
        if inactive_operational_markers:
            inactive_operational_markers.sort(
                key=lambda item: item[0],
                reverse=True,
            )
            context = inactive_operational_markers[0][1]
            print(f"📄 Оперативная запись требует проверки:\n{context}")
            return context

        if structured_candidates:
            structured_candidates.sort(key=lambda item: item[0], reverse=True)
            context = structured_candidates[0][1]
            print(f"📄 Контекст для AI:\n{context}")
            return context

        # Generic FAQ rows are allowed for support/general routes.  For a
        # game-related route without a project match, using an arbitrary FAQ
        # from another game's worksheet is more dangerous than returning no
        # project context.
        if generic_candidates and not allow_game_context:
            generic_candidates.sort(key=lambda item: item[0], reverse=True)
            selected = generic_candidates[: max(1, min(max_results, 2))]
            context = "\n\n".join(item_text for _, item_text in selected)
            print(f"📄 Найден общий FAQ-контекст:\n{context}")
            return context

        if allow_game_context and _looks_operational_query(comment_clean):
            context = (
                "СЛУЖЕБНЫЙ РЕЗУЛЬТАТ ПОИСКА ОПЕРАТИВНОЙ БАЗЫ:\n"
                "Для этого вопроса не найден проект с активной подтверждённой "
                "оперативной записью. Не придумывать текущий статус, сроки, "
                "факт отправки, дополнения, новые коробки или следующую волну. "
                "SMM должен проверить/добавить актуальную запись в KNOWLEDGE_BASE."
            )
            print(f"📄 Нет подтверждённой оперативной записи:\n{context}")
            return context

        print("❌ Релевантные данные в базе знаний не найдены.")
        return None

    except gspread.exceptions.APIError as e:
        print(f"❌ Ошибка доступа к Google Sheets: {e}")
        return None
    except Exception as e:
        print(f"❌ Неизвестная ошибка: {e}")
        return None


# ================= РАБОТА С БАЗОЙ ПРОМПТОВ =================
def db_set_prompt(name: str, text: str):
    db = SessionLocal()
    try:
        prompt = db.query(Prompt).filter(Prompt.name == name).first()
        if prompt:
            prompt.text = text
            prompt.updated_at = datetime.datetime.now()
        else:
            prompt = Prompt(name=name, text=text)
            db.add(prompt)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"❌ Ошибка сохранения промпта {name}: {e}")
        return False
    finally:
        db.close()


def db_get_prompt(name: str):
    db = SessionLocal()
    try:
        prompt = db.query(Prompt).filter(Prompt.name == name).first()
        return prompt.text if prompt else None
    finally:
        db.close()


def db_list_prompts():
    db = SessionLocal()
    try:
        prompts = db.query(Prompt.name).all()
        return [p[0] for p in prompts]
    finally:
        db.close()


def db_delete_prompt(name: str):
    db = SessionLocal()
    try:
        prompt = db.query(Prompt).filter(Prompt.name == name).first()
        if prompt:
            db.delete(prompt)
            db.commit()
            return True
        return False
    finally:
        db.close()


# ================= ФУНКЦИЯ ГЕНЕРАЦИИ AI-ОТВЕТА =================


def _contains_phrase(text: str, *phrases: str) -> bool:
    return any(phrase in text for phrase in phrases)


def _matches_any(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _is_short_contextual_question(text: str) -> bool:
    """Recognize elliptical questions whose subject lives in the parent post.

    Comments such as ``Когда будет?`` or ``А сколько?`` are common under a
    product/game post.  The same question may be prefixed with praise, for
    example ``Спасибо! Когда будет?`` or ``Круто! А сколько?``.  Strip only a
    narrow leading feedback preamble before matching so a real question keeps
    priority over gratitude without turning unrelated questions into game
    questions.
    """
    normalized = _normalize_search_text(text)
    tokens = normalized.split()

    feedback_leads = {
        "спасибо", "благодарю", "класс", "классно", "супер",
        "отлично", "круто", "здорово", "огонь", "кайф",
    }
    feedback_fillers = {
        "большое", "огромное", "вам", "очень", "прям", "реально",
    }

    # Remove only a leading gratitude/praise fragment.  Stop as soon as the
    # actual question begins.  Keeping ``а``/``и`` is intentional because the
    # contextual patterns below accept those conjunctions.
    idx = 0
    saw_feedback = False
    while idx < len(tokens):
        token = tokens[idx]
        if token in feedback_leads:
            saw_feedback = True
            idx += 1
            continue
        if saw_feedback and token in feedback_fillers:
            idx += 1
            continue
        break

    candidate = " ".join(tokens[idx:]) if saw_feedback else normalized

    return _matches_any(
        candidate,
        r"^(?:а\s+|и\s+)?когда(?:\s+(?:будет|появится|появятся|стартует|начн[её]тся))?$",
        r"^(?:а\s+|и\s+)?сколько$",
        r"^(?:а\s+|и\s+)?поч[её]м$",
    )


def _has_explicit_question_signal(raw_text: str, text: str) -> bool:
    """Return True when praise is accompanied by an actual question.

    A question must outrank a gratitude marker: ``Спасибо! Когда выйдет?``
    should be routed to the question logic, not to ``positive_feedback``.
    The question mark is the strongest signal; the narrow contextual forms are
    also accepted for messages written without punctuation.
    """
    return "?" in (raw_text or "") or _is_short_contextual_question(text)


def detect_comment_intent(comment_text: str) -> str:
    """Route the comment before the LLM performs semantic reasoning.

    The markers are deliberately specific.  Broad substrings such as ``карта``
    and ``получ`` caused false positives (``карта мира`` -> payment and
    ``получилось круто`` -> delivery), so operational routes now require a
    payment/delivery phrase or an unambiguous word stem.
    """
    raw_text = comment_text or ""
    text = _normalize_search_text(raw_text)

    if is_uninformative_comment(raw_text):
        return "ignore_comment"

    # Payment/support.  Never treat a bare word "карта" as payment: it may mean
    # a map/card in the game.  Require a banking/payment context around it.
    if (
        _matches_any(
            text,
            r"\bоплат\w*\b",
            r"\bплат[её]ж\w*\b",
            r"\bсписан\w*\b",
            r"\bбанк\w*\b",
            r"\bкасс\w*\b",
            r"\bcheckout\b",
            r"\bкорзин\w*\b",
            r"\bвозврат\w*\b",
            r"\bденьг\w*\b",
        )
        or _mentions_promo_code(text)
        or _contains_phrase(
            text,
            "банковская карта",
            "банковской картой",
            "оплата картой",
            "оплатить картой",
            "картой не проходит",
            "карта не проходит",
            "не проходит карта",
            "не могу оплатить",
        )
    ):
        return "payment_support"

    # Delivery.  Avoid the old broad marker "получ", which matched
    # "получилось".  The receive/get wording must mention an order/parcel or a
    # concrete future delivery form.
    if (
        _matches_any(
            text,
            r"\bдостав\w*\b",
            r"\bотправ\w*\b",
            r"\bтрек(?:-?номер)?\w*\b",
            r"\bпосыл\w*\b",
            r"\bкурьер\w*\b",
            r"\bрегион\w*\b",
        )
        or _mentions_delivery_mailing(text)
        or _contains_phrase(
            text,
            "когда придет",
            "когда придёт",
            "когда получу",
            "когда получим",
            "получить заказ",
            "получу заказ",
            "получил заказ",
            "не получил заказ",
            "получить посылку",
            "получу посылку",
            "адрес доставки",
            "срок доставки",
            "срок отправки",
            "когда ждать",
        )
    ):
        return "delivery_question"

    # Technical/support complaints should win over praise when a sentence mixes
    # both (e.g. "Спасибо, но сайт не работает").
    if (
        _matches_any(
            text,
            r"\bошибк\w*\b",
            r"\bпроблем\w*\b",
            r"\bслом\w*\b",
            r"\bбрак\w*\b",
            r"\bповреж\w*\b",
            r"\bжалоб\w*\b",
            r"\bнедовол\w*\b",
        )
        or _contains_phrase(
            text,
            "не работает",
            "не открывается",
            "не загружается",
            "не запускается",
            "не пришло",
            "не пришёл",
            "не пришел",
            "не получилось оформить",
            "не получается оформить",
            "не могу оформить заказ",
            "не могу заказать",
            "не дает оформить заказ",
            "не даёт оформить заказ",
            "не получается заказать",
            "нет ответа",
            "ответа нет",
            "пока ответа нет",
            "так и нет ответа",
            "так и ответа нет",
            "не ответили",
            "не отвечают",
            "жду ответа",
            "жду ответ",
        )
    ):
        return "complaint_or_issue"

    # A real question must outrank a gratitude/praise marker.  Without this
    # guard, messages like "Спасибо! Когда выйдет?" were reduced to a thank-
    # you reply and the actual question was ignored.
    if (
        not _has_explicit_question_signal(raw_text, text)
        and _matches_any(
            text,
            r"\bспасиб\w*\b",
            r"\bблагодар\w*\b",
            r"\bкласс\w*\b",
            r"\bсупер\b",
            r"\bотличн\w*\b",
            r"\bкрут\w*\b",
            r"\bздоров\w*\b",
            r"\bогонь\b",
            r"\bпонрав\w*\b",
            r"\bлюблю\b",
            r"\bмолодц\w*\b",
            r"\bкайф\w*\b",
        )
    ):
        return "positive_feedback"

    # Suggestions to the SMM team may contain words like "статус", but they
    # are not requests for a specific project's status.  Keep them out of the
    # project KB so an unrelated game's FAQ cannot leak into a simple idea.
    if _contains_phrase(
        text,
        "сделать пост",
        "можно сделать пост",
        "пост по статусам",
        "пост со статусами",
        "идея для поста",
    ):
        return "neutral_comment"

    # Project status/update questions are common under campaign posts and may
    # be phrased without a question mark ("в августе обещали информацию...").
    # Route them through the game context so the operational FAQ can answer.
    if (
        _matches_any(
            text,
            r"\bстатус\w*\b",
            r"\bновост\w*\b",
            r"\bобещал\w*\b",
            r"\bинформац\w*\b",
            r"\bпредзаказ\w*\b",
            r"\bзадерж\w*\b",
        )
        or _contains_phrase(text, "что по проекту", "как там проект", "сколько прошло")
    ):
        return "game_question"

    # Product/game questions often omit the title because the comment is made
    # directly under a post.  Recognize common elliptical questions here so
    # the parent post can identify the game instead of forcing the SMM reply to
    # ask "о какой игре речь?".
    if (
        _is_short_contextual_question(text)
        or _matches_any(
            text,
            r"\bигр(?:а|ы|е|у|ой|ою|ах|ами)?\b",
            r"\bигрок\w*\b",
            r"\bцен\w*\b",
            r"\bстоимост\w*\b",
            r"\bсто(?:ит|ят)\b",
            r"\bправил\w*\b",
            r"\bмеханик\w*\b",
            r"\bсюжет\w*\b",
            r"\bлокализац\w*\b",
            r"\bязык\w*\b",
            r"\bрусск\w*\b",
            r"\bвдво[её]м\b",
            r"\bдвоих\b",
            r"\bрелиз\w*\b",
            r"\bвыйд(?:ет|ут|я)\w*\b",
            r"\bвыход\w*\b",
            r"\bналичи\w*\b",
            r"\bпродаж\w*\b",
            r"\bкомплект\w*\b",
            r"\bпредзаказ\w*\b",
            r"\bдополнени\w*\b",
            r"\bаддон\w*\b",
            r"\baddon\w*\b",
            r"\bexpansion\w*\b",
            _ADDON_SHORTHAND_RE,
        )
        or _contains_phrase(
            text,
            "время партии",
            "сколько человек",
            "сколько игроков",
            "сколько стоит",
            "есть на русском",
            "будет на русском",
            "на русском будет",
            "можно вдвоем",
            "можно вдвоём",
            "играть вдвоем",
            "играть вдвоём",
            "где купить",
            "как купить",
            "можно купить",
            "как заказать",
            "можно заказать",
            "есть в наличии",
            "когда в продаже",
            "дата выхода",
            "когда выйдет",
            "когда релиз",
        )
    ):
        return "game_question"

    if "?" in raw_text or any(text.startswith(word) for word in [
        "как ", "почему ", "зачем ", "где ", "когда ", "можно ", "есть ли ",
    ]):
        return "general_question"

    return "neutral_comment"

def _intent_instruction(intent: str) -> str:
    instructions = {
        "ignore_comment": (
            "Комментарий не содержит понятного запроса и не требует ответа. Не генерируй ответ."
        ),
        "positive_feedback": (
            "Это похоже на благодарность или положительную обратную связь. "
            "Коротко и тепло поблагодари. Не задавай уточняющий вопрос и не спрашивай название игры."
        ),
        "payment_support": (
            "Это вопрос/проблема с оплатой или промокодом. Отвечай по оплате/промокоду: предложи безопасный следующий шаг "
            "или запроси только действительно нужные данные о платеже/заказе. Не спрашивай, о какой игре речь, "
            "если название игры не нужно для решения проблемы."
        ),
        "delivery_question": (
            "Это вопрос о доставке/отправке. Используй данные поста или базы знаний только если они реально дают срок/условия. "
            "Если точного срока нет, честно скажи об этом и запроси минимально необходимую информацию."
        ),
        "complaint_or_issue": (
            "Это жалоба или проблема. Признай проблему, предложи конкретный следующий шаг и задай максимум один "
            "необходимый уточняющий вопрос. Не своди ответ автоматически к уточнению названия игры."
        ),
        "game_question": (
            "Это вопрос о конкретной игре, статусе проекта или её характеристиках. Используй релевантный контекст поста и базы знаний. "
            "Оперативная FAQ-запись имеет приоритет над старым постом и общими полями. Если спрашивают о дополнениях, новых коробках, "
            "волнах, сроках или статусе, а в контексте нет явного подтверждения, прямо скажи, что подтверждённой информации сейчас нет; "
            "не придумывай планы, состав волны и формулировки вроде 'скоро'. Спрашивай название игры только если без него действительно "
            "невозможно ответить и оно не определяется из поста."
        ),
        "general_question": (
            "Ответь на смысл вопроса. Контекст поста используй только если он непосредственно помогает. "
            "Не предполагай автоматически, что пользователю нужно уточнить название игры."
        ),
        "neutral_comment": (
            "Это может быть нейтральный комментарий без запроса информации. Дай естественную короткую реакцию по смыслу; "
            "не выдумывай проблему и не спрашивай название игры без необходимости."
        ),
    }
    return instructions[intent]


def _should_include_post_context(intent: str, comment_text: str) -> bool:
    """Return whether the parent post is likely to help this specific route.

    Game/delivery routes always need the post because short comments commonly
    omit the title.  Generic support questions stay self-contained unless they
    explicitly point back to the post.
    """
    if intent in {"game_question", "delivery_question"}:
        return True

    text = _normalize_search_text(comment_text)
    # Promo-code questions are often meaningful only in the context of the
    # campaign post (for example, a post announcing a sale or a code).  Include
    # the post for promo-specific payment_support, but keep ordinary card/payment
    # failures isolated from unrelated product copy.
    if intent == "payment_support":
        return _mentions_promo_code(text) or _contains_phrase(
            f" {text} ",
            " этот ", " эта ", " это ", " здесь ", " в посте ",
        )

    if intent not in {"complaint_or_issue", "general_question"}:
        return False

    # Thus "У вас есть вакансии?" and "Сайт не работает" stay self-contained,
    # while "Когда это выйдет?" can use the parent post.
    return _contains_phrase(
        f" {text} ",
        " это ", " этот ", " эта ", " этой ", " этого ", " эти ",
        " здесь ", " в посте ", " из поста ", " данная ", " данный ",
        " у неё ", " у нее ", " у него ", " про неё ", " про нее ",
    )


def generate_ai_reply(comment_text, post_text, source="unknown"):
    LLM_API_URL = os.getenv("LLM_API_URL")
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_MODEL = os.getenv("LLM_MODEL")
    if not LLM_API_KEY:
        return None

    intent = detect_comment_intent(comment_text)
    if intent == "ignore_comment":
        print("⏩ AI-ответ не нужен: бессодержательный комментарий")
        return None

    # Decide what context is allowed *after* routing.  Pure praise/neutral
    # chatter needs no KB lookup.  Payment/complaint/general questions may use a
    # matching FAQ, but structured game data is allowed only when the intent is
    # actually game- or delivery-related.
    knowledge_context = None
    allow_game_context = intent in {"game_question", "delivery_question"}
    if intent not in {"positive_feedback", "neutral_comment"}:
        knowledge_context = get_knowledge_base_context(
            comment_text,
            post_text if allow_game_context else "",
            allow_game_context=allow_game_context,
        )

    # Do not send the parent post merely because one exists.  It is always
    # useful for game/delivery routes and only conditionally useful for generic
    # questions/issues that explicitly refer back to the post.
    include_post = _should_include_post_context(intent, comment_text)
    post_context = (post_text or "").strip()[:1800] if include_post else ""

    source_label = {"vk": "VK", "telegram": "Telegram"}.get(source, source or "неизвестно")
    system_prompt = (
        "Ты AI-помощник SMM-специалиста магазина настольных игр. "
        "Сначала ВНУТРЕННЕ определи тему и намерение комментария, но не выводи классификацию пользователю. "
        "Затем сформируй естественный ответ именно на это намерение. "
        "Критическое правило: НЕ считай каждый комментарий вопросом об игре и НЕ спрашивай 'о какой игре речь' по умолчанию. "
        "Такой вопрос допустим только когда конкретная игра действительно нужна для ответа и её нельзя определить из комментария или поста. "
        "Не выдумывай факты, цены, сроки, статусы платежей или заказов. "
        "Если оперативная FAQ-запись присутствует, считай её главным источником текущего статуса; внутреннюю инструкцию SMM выполняй, но не цитируй дословно. "
        "Если в доступном контексте НЕТ явного подтверждения дополнений, новых коробок, следующей волны, срока или даты, не делай выводов из общих слов поста: "
        "скажи, что подтверждённой информации в доступной базе сейчас нет. Не заменяй неизвестный срок словами 'скоро' или 'в ближайшее время'. "
        "Отвечай кратко, обычно 1-3 предложениями."
    )

    prompt_parts = [
        f"Площадка: {source_label}",
        f"Комментарий пользователя: {comment_text}",
        f"Маршрут предварительной классификации: {intent}",
        f"Правило для этого случая: {_intent_instruction(intent)}",
    ]
    if post_context:
        prompt_parts.append(
            "Контекст поста (используй только если он релевантен ответу):\n" + post_context
        )
    if knowledge_context:
        prompt_parts.append(
            "Релевантные данные/FAQ (используй только если они прямо отвечают на комментарий):\n"
            + knowledge_context
        )
    prompt_parts.append(
        "Сформируй только готовый ответ SMM-специалиста без меток intent, анализа и служебных пояснений."
    )
    user_prompt = "\n\n".join(prompt_parts)

    try:
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        response = requests.post(
            LLM_API_URL, headers=headers, json=payload, timeout=45)
        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

        print(f"❌ Ошибка API: {response.status_code}")
        return None
    except Exception as e:
        print(f"❌ Ошибка AI: {e}")
        return None

# ================= АВТОМАТИЧЕСКАЯ ОТПРАВКА В ТЕЛЕГРАМ =================


def send_tg_notification(comment):
    if not TG_BOT_TOKEN or not ADMIN_CHAT_ID:
        return False
    ignore_reason = get_ignore_reason(
        getattr(comment, "text", ""),
        source=getattr(comment, "source", ""),
        author_id=getattr(comment, "author_id", None),
        author_name=getattr(comment, "author_name", None),
        group_id=getattr(comment, "group_id", None),
    )
    if ignore_reason:
        print(f"⏩ Telegram-уведомление пропущено ({ignore_reason})")
        return True
    try:
        raw_text = comment.text or "Без текста"
        if len(raw_text) > 3500:
            raw_text = raw_text[:3500] + "\n\n(Текст слишком длинный и был сокращен)"

        author = html.escape(str(comment.author_name or "Unknown"))
        display_text = html.escape(raw_text)

        comment_url = str(comment.url or "").strip()
        if comment_url:

            safe_url = html.escape(comment_url, quote=True)

            link_html = f'<a href="{safe_url}">Перейти к комментарию</a>'
        else:

            link_html = "Перейти к комментарию"


        source = getattr(comment, "source", "vk")

        if source == "telegram":
            notification_title = (
                "📜 <b>Исторический комментарий Telegram</b>"
                if getattr(comment, "status", None) == "baseline"
                else "🆕 <b>Новый комментарий Telegram!</b>"
            )
        else:
            notification_title = (
                "📜 <b>Исторический комментарий VK</b>"
                if getattr(comment, "status", None) == "baseline"
                else "🆕 <b>Новый комментарий VK!</b>"
            )

        platform_label = "Telegram" if source == "telegram" else "VK"
        message = (
            f"{notification_title}\n"
            f"🌐 Площадка: <b>{platform_label}</b>\n"
            f"👤 Автор: {author}\n"
            f"💬 Текст: {display_text}\n"
            f"🔗 {link_html}\n"  # 🔥
            f"ℹ️ ID: <code>{comment.id}</code>\n\n"
        )
        if comment.ai_suggested_reply:
            ai_reply = html.escape(str(comment.ai_suggested_reply))
            message += f"🤖 <b>AI-ответ:</b>\n<pre>{ai_reply}</pre>\n\n"

        keyboard_dict = {
            "inline_keyboard": [
                [{"text": "✅ Ответил", "callback_data": f"reply_{comment.id}"}]
            ]
        }

        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard_dict,
        }
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(
                f"⚠️ Telegram API вернул {response.status_code}: "
                f"{response.text[:500]}"
            )
            return False
        return True
    except Exception as e:
        print(f"⚠️ Не удалось отправить уведомление в Telegram: {e}")
        return False

# ================= СОХРАНЕНИЕ КОММЕНТАРИЕВ =================


def send_pending_tg_notifications():
    """Deliver unsent comments and rate-limited reminders safely.

    New comments are normally sent immediately by ``save_comments_to_db``.
    This function retries rows that were never delivered and, optionally,
    reminds about still-unprocessed ``status='new'`` rows after
    ``TELEGRAM_REMINDER_INTERVAL`` seconds.

    Safety rules:
    * reminders are NOT tied to CHECK_INTERVAL;
    * a freshly restarted process waits one full reminder interval before it
      can emit reminders, preventing an old backlog from flooding Telegram;
    * historical ``baseline`` rows are never replayed automatically;
    * each call sends at most TELEGRAM_NOTIFICATION_BATCH_LIMIT messages.
    """
    if not TG_BOT_TOKEN or not ADMIN_CHAT_ID:
        return 0

    global LAST_REMINDER_BATCH_AT

    db = SessionLocal()
    sent_count = 0
    try:
        now = datetime.datetime.now()
        batch_limit = TELEGRAM_NOTIFICATION_BATCH_LIMIT

        # 1) First priority: comments that have never been successfully sent.
        # Historical baseline rows are intentionally excluded.
        unsent = (
            db.query(Comment)
            .filter(Comment.is_processed.is_(False))
            .filter(Comment.tg_notified.is_(False))
            .filter(Comment.status != "baseline")
            .order_by(Comment.date.asc(), Comment.id.asc())
            .limit(batch_limit)
            .all()
        )

        pending = list(unsent)
        remaining = batch_limit - len(pending)

        # 2) Reminders are optional and use their own cooldown.  Do not remind
        # during the first interval after a restart: old rows may have stale
        # updated_at values from pre-fix versions and must not cause a burst.
        process_uptime = (now - APP_STARTED_AT).total_seconds()
        since_last_reminder_batch = (
            now - LAST_REMINDER_BATCH_AT
        ).total_seconds()
        reminder_batch_due = (
            TELEGRAM_REMINDER_INTERVAL > 0
            and process_uptime >= TELEGRAM_REMINDER_INTERVAL
            and since_last_reminder_batch >= TELEGRAM_REMINDER_INTERVAL
        )
        if remaining > 0 and reminder_batch_due:
            reminder_cutoff = now - datetime.timedelta(
                seconds=TELEGRAM_REMINDER_INTERVAL
            )
            reminders = (
                db.query(Comment)
                .filter(Comment.is_processed.is_(False))
                .filter(Comment.tg_notified.is_(True))
                .filter(Comment.status == "new")
                .filter(Comment.updated_at <= reminder_cutoff)
                .order_by(Comment.updated_at.asc(), Comment.id.asc())
                .limit(remaining)
                .all()
            )
            pending.extend(reminders)
            # Run at most one reminder batch per reminder interval, regardless
            # of how often background_sync itself polls.
            LAST_REMINDER_BATCH_AT = now

        if not pending:
            return 0

        print(
            f"📨 В Telegram ожидают отправки {len(pending)} комментариев "
            f"(лимит за цикл: {batch_limit})."
        )

        for comment in pending:
            ignore_reason = get_ignore_reason(
                comment.text,
                source=comment.source,
                author_id=comment.author_id,
                author_name=comment.author_name,
                group_id=comment.group_id,
            )
            if ignore_reason:
                comment.status = "ignored"
                comment.is_processed = True
                comment.tg_notified = True
                comment.updated_at = datetime.datetime.now()
                db.commit()
                print(f"⏩ Комментарий {comment.id} автоматически проигнорирован ({ignore_reason})")
                continue

            # Telegram rows are inserted by the isolated Telethon worker.
            # Generate the same AI suggestion here, inside the app container,
            # where credentials.json is mounted.
            if not comment.ai_suggested_reply and comment.text:
                try:
                    post = (
                        db.query(Post)
                        .filter(
                            Post.source == comment.source,
                            Post.external_id == comment.post_external_id,
                            Post.group_id == comment.group_id,
                        )
                        .first()
                    )
                    if post:
                        ai_reply = generate_ai_reply(
                            comment.text, post.text or "", comment.source
                        )
                        if ai_reply:
                            comment.ai_suggested_reply = ai_reply
                            db.commit()
                except Exception as ai_error:
                    db.rollback()
                    print(
                        f"⚠️ AI для комментария {comment.id} не сгенерирован: "
                        f"{ai_error}"
                    )

            is_reminder = bool(comment.tg_notified)
            if send_tg_notification(comment):
                comment.tg_notified = True
                # Explicitly store the last successful notification time.
                # Merely assigning True to an already-True boolean does not
                # necessarily trigger SQLAlchemy's onupdate timestamp.
                comment.updated_at = datetime.datetime.now()
                db.commit()
                sent_count += 1
                delivery_kind = (
                    "повторное напоминание" if is_reminder else "уведомление"
                )
                print(
                    f"✅ Telegram-{delivery_kind} отправлено для "
                    f"{comment.source} комментария {comment.id}"
                )
                time.sleep(1.0)
            else:
                db.rollback()
                print(
                    f"⚠️ Telegram-уведомление для комментария {comment.id} "
                    "не отправлено; повторим на следующем цикле."
                )

        return sent_count
    finally:
        db.close()


def save_comments_to_db(comments_data, *, notify_telegram=True, initial_status="new"):
    db = SessionLocal()
    saved_count = 0
    for comment_data in comments_data:
        try:
            text_content = comment_data.get("text", "")
            if not text_content or text_content.strip() == "":
                print("⏩ Пропущен пустой комментарий (стикер или только пробелы)")
                continue

            source = comment_data.get("source", "vk")
            ignore_reason = get_ignore_reason(
                text_content,
                source=source,
                author_id=comment_data.get("author_id"),
                author_name=comment_data.get("author_name"),
                group_id=comment_data.get("group_id"),
            )
            if ignore_reason:
                print(f"⏩ Комментарий пропущен ({ignore_reason}): {text_content[:120]}")
                continue

            existing = db.query(Comment).filter(
                Comment.source == source,
                Comment.external_id == str(comment_data["external_id"]),
                Comment.post_external_id == str(comment_data["post_external_id"]),
                Comment.group_id == str(comment_data.get("group_id") or ""),
            ).first()
            if existing:
                continue

            # ===== Пост (Post) =====
            post = db.query(Post).filter(
                Post.source == source,
                Post.external_id == str(comment_data["post_external_id"]),
                Post.group_id == str(comment_data.get("group_id") or ""),
            ).first()
            if not post:
                # Формируем URL поста в зависимости от источника
                if source == "telegram":
                    # Для Telegram используем переданный post_url или создаём из channel/post_id
                    post_url = comment_data.get("post_url", "")
                    if not post_url and "channel" in comment_data:
                        post_url = f"https://t.me/{comment_data['channel']}/{comment_data['post_external_id']}"
                else:
                    # VK
                    post_url = f"https://vk.com/wall{comment_data['group_id']}_{comment_data['post_external_id']}"

                post = Post(
                    source=source,
                    external_id=str(comment_data["post_external_id"]),
                    group_id=str(comment_data.get("group_id") or ""),
                    date=datetime.datetime.now(),
                    text=comment_data.get("post_text", ""),
                    url=post_url,
                    comments_count=0
                )
                db.add(post)
                db.flush()

            # ===== Комментарий =====
            # Формируем URL комментария в зависимости от источника
            if source == "telegram":
                comment_url = comment_data.get("comment_url") or comment_data.get("url", "")
                if not comment_url and "channel" in comment_data:
                    comment_url = f"https://t.me/{comment_data['channel']}/{comment_data['post_external_id']}?comment={comment_data['external_id']}"
            else:
                # VK
                comment_url = f"https://vk.com/wall{comment_data['group_id']}_{comment_data['post_external_id']}?reply={comment_data['external_id']}"

            comment = Comment(
                source=source,
                external_id=str(comment_data["external_id"]),
                post_external_id=str(comment_data["post_external_id"]),
                group_id=str(comment_data.get("group_id") or ""),
                author_id=comment_data.get("author_id"),
                author_name=comment_data.get("author_name", "Unknown"),
                text=comment_data.get("text", ""),
                date=comment_data.get("date", datetime.datetime.now()),
                likes=comment_data.get("likes", 0),
                reply_to=comment_data.get("reply_to"),
                is_processed=False,
                tg_notified=False,
                url=comment_url,
                status=initial_status
            )
            db.add(comment)
            db.flush()

            # Генерация AI
            try:
                if post and comment.text:
                    print(f"🔄 Генерация AI для комментария {comment.id}...")
                    ai_reply = generate_ai_reply(comment.text, post.text, comment.source)
                    if ai_reply:
                        comment.ai_suggested_reply = ai_reply
                        db.flush()
            except Exception as ai_error:
                print(f"⚠️ Ошибка в блоке AI: {ai_error}")

            if notify_telegram and ADMIN_CHAT_ID and TG_BOT_TOKEN:
                comment.tg_notified = send_tg_notification(comment)
                db.flush()

            db.commit()
            saved_count += 1
            print(f"✅ Комментарий {comment.id} успешно сохранен в БД")

        except Exception as e:
            db.rollback()
            print(f"❌ Ошибка сохранения конкретного комментария: {e}")

    db.close()
    return saved_count


def ensure_web_panel_backfill(parser: VKParser):
    """Populate the database with historical VK comments without Telegram spam.

    Returns ``None`` when the VK snapshot could not be fetched completely,
    otherwise returns the number of rows inserted during this run.
    """

    if not parser.needs_db_backfill:
        return 0

    print(
        "🗃️ Веб-панель ещё не получила исторические комментарии. "
        "Запускаем безопасный backfill без Telegram-уведомлений..."
    )
    existing_comments = parser.get_existing_comments_for_backfill(POSTS_COUNT)
    if existing_comments is None:
        print("⚠️ Backfill веб-панели отложен до следующей синхронизации")
        return None

    saved_count = save_comments_to_db(
        existing_comments,
        notify_telegram=False,
        initial_status="baseline",
    )
    parser.mark_db_backfill_complete(existing_comments)
    print(
        f"✅ Backfill веб-панели завершён: сохранено {saved_count}, "
        f"проверено {len(existing_comments)} исторических комментариев. "
        "Telegram-уведомления не отправлялись."
    )
    return saved_count

# ================= ФОНОВЫЙ ПРОЦЕСС ПАРСЕРА VK =================


def background_sync():
    print("🔄 Запуск фоновой синхронизации...")
    time.sleep(3)
    if not VK_ACCESS_TOKEN or not VK_GROUP_ID:
        print("❌ VK_ACCESS_TOKEN или VK_GROUP_ID не заданы в .env")
        return
    parser = VKParser(VK_ACCESS_TOKEN, VK_GROUP_ID, delay=0.8)
    while True:
        try:
            print(
                f"\n⏰ Синхронизация в {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            if parser.needs_db_backfill:
                backfill_result = ensure_web_panel_backfill(parser)
                if backfill_result is None:
                    time.sleep(CHECK_INTERVAL)
                    continue

            # Deliver unsent rows and repeat active "new" comments until the
            # SMM marks them processed with the ✅ Ответил button.
            replayed_count = send_pending_tg_notifications()
            if replayed_count:
                print(
                    f"📨 Отправлено {replayed_count} уведомлений/напоминаний "
                    "по комментариям в Telegram"
                )

            new_comments = parser.get_new_comments(POSTS_COUNT)
            if new_comments:
                saved_count = save_comments_to_db(new_comments)
                print(f"✅ Сохранено {saved_count} новых комментариев")
            else:
                print("📭 Новых комментариев нет")
        except Exception as e:
            print(f"❌ Ошибка синхронизации: {e}")
        time.sleep(CHECK_INTERVAL)


# ================= ТЕЛЕГРАМ БОТ (С ОДНОЙ КНОПКОЙ) =================


def db_get_new_comments():
    db = SessionLocal()
    try:
        return db.query(Comment).filter(Comment.status == 'new').order_by(Comment.date.desc()).limit(5).all()
    finally:
        db.close()


def db_update_comment_status(comment_id: int, status: str):

    from sqlalchemy.exc import OperationalError

    retries = 3
    for attempt in range(retries):
        db = SessionLocal()
        try:
            comment = db.query(Comment).filter(
                Comment.id == comment_id).first()
            if comment:
                comment.is_processed = True
                comment.status = status
                comment.updated_at = datetime.datetime.now()
                db.commit()
                return True

            print(
                f"⚠️ ВНИМАНИЕ: Комментарий с ID {comment_id} не найден в базе данных!")
            return False

        except OperationalError as e:
            if attempt < retries - 1:
                print(
                    f"⚠️ Временная ошибка БД. Повторная попытка через 1 сек... "
                    f"({attempt + 1}/{retries}): {e}"
                )
                time.sleep(1)
                continue
            print(
                f"❌ Критическая ошибка при обновлении статуса {comment_id}: {e}")
            return False
        except Exception as e:
            print(
                f"❌ Неизвестная ошибка при обновлении статуса {comment_id}: {e}")
            return False
        finally:
            db.close()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Бот управления комментариями.\n"
        "Новые комментарии приходят автоматически с умным поиском по базе знаний."
    )


async def new_comments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments = await asyncio.to_thread(db_get_new_comments)

    if not comments:
        await update.message.reply_text("✅ Сейчас нет новых необработанных комментариев!")
        return

    for c in comments:
        full_text = html.escape(c.text if c.text else "Без текста")
        author = html.escape(str(c.author_name or "Unknown"))
        url = html.escape(str(c.url or ""), quote=True)
        platform_label = "Telegram" if c.source == "telegram" else "VK"
        msg_text = (
            "📝 <b>Новый комментарий</b>\n"
            f"🌐 <b>Площадка:</b> {platform_label}\n"
            f"<b>ID:</b> {c.id}\n"
            f"<b>Автор:</b> {author}\n"
            f"<b>Текст:</b> {full_text}\n"
            f'<a href="{url}">Ссылка</a>'
        )
        if c.ai_suggested_reply:
            ai_reply = html.escape(str(c.ai_suggested_reply))
            msg_text += f"\n\n🤖 <b>Предлагаемый AI-ответ:</b>\n<pre>{ai_reply}</pre>"

        keyboard = [[InlineKeyboardButton(
            "✅ Ответил", callback_data=f"reply_{c.id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # 1. Сначала отвечаем Telegram, чтобы запрос не протух
    try:
        await query.answer()
    except Exception as e:
        print(f"⚠️ Ошибка ответа на callback (запрос мог протухнуть): {e}")

    # 2. Только теперь выполняем операцию с БД (она может быть долгой)
    comment_id = int(query.data.replace('reply_', ''))
    success = await asyncio.to_thread(db_update_comment_status, comment_id, "replied")

    new_text = query.message.text + \
        f"\n\n{'✅ Статус: Ответил' if success else '❌ Ошибка'}"
    try:
        await query.edit_message_text(text=new_text)
    except Exception as e:
        print(f"⚠️ Ошибка при редактировании сообщения: {e}")


# ================= КОМАНДЫ УПРАВЛЕНИЯ ПРОМПТАМИ =================

async def add_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/add_prompt <name> <text> — добавить или обновить промпт"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/add_prompt имя текст`\n"
            "Пример: `/add_prompt system Ты – вежливый помощник.`",
            parse_mode="Markdown"
        )
        return

    name = args[0]
    text = ' '.join(args[1:])
    success = await asyncio.to_thread(db_set_prompt, name, text)
    if success:
        await update.message.reply_text(f"✅ Промпт `{name}` успешно сохранён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Ошибка при сохранении промпта `{name}`.", parse_mode="Markdown")


async def list_prompts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list_prompts — показать все имена промптов"""
    names = await asyncio.to_thread(db_list_prompts)
    if not names:
        await update.message.reply_text("📭 Нет сохранённых промптов.")
        return

    lines = [f"{i+1}. `{name}`" for i, name in enumerate(names)]
    msg = "📋 *Список промптов:*\n" + "\n".join(lines)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def get_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/get_prompt <name> — показать текст промпта"""
    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите имя промпта: `/get_prompt system`", parse_mode="Markdown")
        return

    name = args[0]
    text = await asyncio.to_thread(db_get_prompt, name)
    if text is None:
        await update.message.reply_text(f"❌ Промпт с именем `{name}` не найден.", parse_mode="Markdown")
    else:
        if len(text) > 4000:
            text = text[:4000] + "\n…(обрезано)"
        await update.message.reply_text(
            f"📄 *Промпт `{name}`:*\n```\n{text}\n```",
            parse_mode="Markdown"
        )


async def delete_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delete_prompt <name> — удалить промпт"""
    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите имя промпта: `/delete_prompt system`", parse_mode="Markdown")
        return

    name = args[0]
    success = await asyncio.to_thread(db_delete_prompt, name)
    if success:
        await update.message.reply_text(f"✅ Промпт `{name}` удалён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Промпт `{name}` не найден или не удалён.", parse_mode="Markdown")


def run_telegram_bot():
    if not TG_BOT_TOKEN:
        print("⚠️ TG_BOT_TOKEN не указан, Telegram Bot с ✅ не будет запущен🤖.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(TG_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("new", new_comments_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(CommandHandler("add_prompt", add_prompt_command))
    application.add_handler(CommandHandler("list_prompts", list_prompts_command))
    application.add_handler(CommandHandler("get_prompt", get_prompt_command))
    application.add_handler(CommandHandler("delete_prompt", delete_prompt_command))
    print("🤖 Telegram Bot (✅) запущен и слушает команду🤖")
    application.run_polling(stop_signals=None)

# ================= API ЭНДПОИНТЫ =================


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cpgames-bot"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"games": [], "last_update": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    )


@app.get("/api/comments")
async def get_comments(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None
):
    query = db.query(Comment)
    if status == "unprocessed":
        query = query.filter(Comment.is_processed == False)
    elif status == "processed":
        query = query.filter(Comment.is_processed == True)
    total = query.count()
    comments = query.order_by(Comment.date.desc()).offset(
        offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "comments": [
            {
                "id": c.id,
                "external_id": c.external_id,
                "author_name": c.author_name,
                "text": c.text,
                "date": c.date.isoformat() if c.date else None,
                "likes": c.likes,
                "is_processed": c.is_processed,
                "status": getattr(c, 'status', None),
                "source": c.source,
                "url": c.url,
                "ai_suggested_reply": c.ai_suggested_reply
            }
            for c in comments
        ]
    }


@app.post("/api/comments/{comment_id}/process")
async def process_comment(comment_id: int, db: Session = Depends(get_db)):
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        return JSONResponse({"error": "Комментарий не найден"}, status_code=404)
    comment.is_processed = True
    comment.status = "replied"
    comment.updated_at = datetime.datetime.now()
    db.commit()
    return {"success": True, "message": "Комментарий отмечен как обработанный"}


@app.get("/api/sync")
async def sync_now(db: Session = Depends(get_db)):
    if not VK_ACCESS_TOKEN or not VK_GROUP_ID:
        return {"error": "VK_ACCESS_TOKEN или VK_GROUP_ID не заданы"}
    try:
        parser = VKParser(VK_ACCESS_TOKEN, VK_GROUP_ID)
        backfilled = ensure_web_panel_backfill(parser)
        if backfilled is None:
            return {
                "success": False,
                "message": "Не удалось полностью получить исторические комментарии VK; повторите синхронизацию",
                "saved": 0,
            }

        new_comments = parser.get_new_comments(POSTS_COUNT)
        if new_comments:
            saved_count = save_comments_to_db(new_comments)
            return {
                "success": True,
                "message": f"Сохранено {saved_count} новых комментариев",
                "saved": saved_count,
                "backfilled": backfilled,
            }
        else:
            message = "Новых комментариев нет"
            if backfilled:
                message = f"Исторические комментарии добавлены в веб-панель: {backfilled}; новых комментариев нет"
            return {
                "success": True,
                "message": message,
                "saved": 0,
                "backfilled": backfilled,
            }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    total_posts = db.query(Post).count()
    total_comments = db.query(Comment).count()
    unprocessed = db.query(Comment).filter(
        Comment.is_processed == False).count()
    return {
        "total_posts": total_posts,
        "total_comments": total_comments,
        "unprocessed": unprocessed,
        "processed": total_comments - unprocessed
    }

# ================= ЗАПУСК ПРИЛОЖЕНИЯ =================


@app.on_event("startup")
def startup_event():
    init_db()
    if BACKGROUND_WORKERS_ENABLED:
        threading.Thread(target=background_sync, daemon=True).start()
        if TG_BOT_TOKEN:
            threading.Thread(target=run_telegram_bot, daemon=True).start()
        print("🚀 FastAPI + VK polling + SMM Bot запущены; Telethon работает отдельным worker-ом.")
    else:
        print("ℹ️ FastAPI запущен без фоновых worker-ов (BACKGROUND_WORKERS_ENABLED=false)")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
