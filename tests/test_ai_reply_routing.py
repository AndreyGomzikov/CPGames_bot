from types import SimpleNamespace

from src import main


def test_intent_routes_operational_problem_before_thanks():
    assert (
        main.detect_comment_intent("Спасибо, но оплата картой не проходит")
        == "payment_support"
    )
    assert (
        main.detect_comment_intent("Спасибо! Когда будет доставка?")
        == "delivery_question"
    )


def test_intent_recognizes_positive_feedback_without_game_clarification():
    assert main.detect_comment_intent("Спасибо, всё супер! Очень понравилось") == "positive_feedback"


def test_game_question_is_detected():
    assert main.detect_comment_intent("Сколько игроков поддерживает эта игра?") == "game_question"


def _fake_llm_response(text="Готовый ответ"):
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": text}}]},
    )


def test_positive_feedback_does_not_load_game_kb_or_force_post(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def kb_must_not_run(*args, **kwargs):
        raise AssertionError("KB lookup must not run for simple praise")

    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Спасибо! Очень приятно 😊")

    monkeypatch.setattr(main, "get_knowledge_base_context", kb_must_not_run)
    monkeypatch.setattr(main.requests, "post", fake_post)

    answer = main.generate_ai_reply(
        "Спасибо, всё было супер!",
        "Пост про игру, который здесь не нужен",
        "telegram",
    )

    assert answer == "Спасибо! Очень приятно 😊"
    user_prompt = captured["payload"]["messages"][1]["content"]
    system_prompt = captured["payload"]["messages"][0]["content"]
    assert "positive_feedback" in user_prompt
    assert "Контекст поста" not in user_prompt
    assert "не спрашивай название игры" in user_prompt.lower()
    assert "НЕ спрашивай 'о какой игре речь' по умолчанию" in system_prompt


def test_payment_problem_focuses_on_payment_not_game(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        assert "оплата" in comment_text.lower()
        assert post_text == ""
        assert allow_game_context is False
        return None

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Проверьте статус платежа и попробуйте другой способ оплаты.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Не проходит оплата картой, что делать?",
        "Пост про конкретную настольную игру",
        "vk",
    )

    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "payment_support" in user_prompt
    assert "Контекст поста" not in user_prompt
    assert "Не спрашивай, о какой игре речь" in user_prompt


def test_game_question_uses_parent_post_and_knowledge(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        captured["kb_comment"] = comment_text
        captured["kb_post"] = post_text
        captured["allow_game_context"] = allow_game_context
        return "ДАННЫЕ ПО ИГРЕ:\nname: Грабёж!\nmax_players: 4"

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("В «Грабёж!» можно играть вчетвером.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    answer = main.generate_ai_reply(
        "Сколько игроков поддерживает эта игра?",
        "Новый пост про настольную игру «Грабёж!»",
        "telegram",
    )

    assert answer == "В «Грабёж!» можно играть вчетвером."
    assert "Грабёж" in captured["kb_post"]
    assert captured["allow_game_context"] is True
    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "Контекст поста" in user_prompt
    assert "ДАННЫЕ ПО ИГРЕ" in user_prompt
    assert "Площадка: Telegram" in user_prompt


def test_notification_contains_platform_and_answered_button(monkeypatch):
    monkeypatch.setattr(main, "TG_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "ADMIN_CHAT_ID", "123")
    captured = {}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(main.requests, "post", fake_post)
    comment = SimpleNamespace(
        id=29,
        source="telegram",
        status="new",
        text="Комментарий",
        author_name="Тест",
        url="https://t.me/fabrica_igr/1?comment=2",
        ai_suggested_reply="Ответ",
    )

    assert main.send_tg_notification(comment) is True
    assert "Площадка: <b>Telegram</b>" in captured["payload"]["text"]
    assert captured["payload"]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Ответил"


def test_intent_does_not_confuse_game_card_with_payment():
    assert main.detect_comment_intent("Карта мира в этой игре отличная") == "positive_feedback"


def test_intent_does_not_confuse_poluchilos_with_delivery():
    assert main.detect_comment_intent("Получилось круто, спасибо!") == "positive_feedback"


def test_intent_recognizes_real_delivery_receive_phrase():
    assert main.detect_comment_intent("Когда получу заказ?") == "delivery_question"


def test_intent_recognizes_technical_issue():
    assert main.detect_comment_intent("Сайт не работает, не могу открыть страницу") == "complaint_or_issue"


def test_payment_route_allows_faq_but_blocks_game_context(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        captured["kb_comment"] = comment_text
        captured["kb_post"] = post_text
        captured["allow_game_context"] = allow_game_context
        return "ОТВЕТ ИЗ БАЗЫ ЗНАНИЙ:\nОплата: попробуйте повторить позже"

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Попробуйте повторить оплату позже.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Не проходит оплата картой",
        "Большой пост про игру «Грабёж!» с характеристиками и ценой",
        "vk",
    )

    assert captured["kb_post"] == ""
    assert captured["allow_game_context"] is False
    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "Контекст поста" not in user_prompt
    assert "ОТВЕТ ИЗ БАЗЫ ЗНАНИЙ" in user_prompt


def test_game_route_explicitly_allows_game_context(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        captured["kb_post"] = post_text
        captured["allow_game_context"] = allow_game_context
        return "ДАННЫЕ ПО ИГРЕ:\nname: Грабёж!\nmax_players: 4"

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("В игре до четырёх игроков.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Сколько игроков в этой игре?",
        "Подробный пост про игру «Грабёж!»",
        "telegram",
    )

    assert "Грабёж" in captured["kb_post"]
    assert captured["allow_game_context"] is True


def test_unrelated_general_question_does_not_receive_parent_post(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        assert allow_game_context is False
        assert post_text == ""
        return None

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Актуальные вакансии лучше уточнить у команды.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "У вас есть вакансии?",
        "Пост про настольную игру «Грабёж!»",
        "telegram",
    )

    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "Контекст поста (используй только если он релевантен ответу):" not in user_prompt
    assert "Грабёж" not in user_prompt


def test_referential_release_question_receives_parent_post(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        assert allow_game_context is True
        assert "Анонс проекта" in post_text
        return None

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Уточню по анонсу.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Когда это выйдет?",
        "Анонс проекта с датой релиза",
        "vk",
    )

    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "Контекст поста (используй только если он релевантен ответу):" in user_prompt
    assert "Анонс проекта" in user_prompt


def test_unprocessed_new_comment_respects_reminder_cooldown(monkeypatch):
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    main.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)

    now = datetime(2026, 9, 3, 1, 0)
    db = TestSession()
    db.add_all([
        main.Comment(
            source="vk",
            external_id="1",
            post_external_id="10",
            group_id="-1",
            author_name="Reminder",
            text="Новый комментарий",
            date=now - timedelta(hours=2),
            is_processed=False,
            tg_notified=True,
            ai_suggested_reply="Ответ",
            status="new",
            updated_at=now,
            url="https://example.invalid/1",
        ),
        main.Comment(
            source="vk",
            external_id="2",
            post_external_id="10",
            group_id="-1",
            author_name="Unsent",
            text="Ещё не отправлен",
            date=now - timedelta(minutes=5),
            is_processed=False,
            tg_notified=False,
            ai_suggested_reply="Ответ",
            status="new",
            updated_at=now - timedelta(minutes=5),
            url="https://example.invalid/2",
        ),
        main.Comment(
            source="vk",
            external_id="3",
            post_external_id="10",
            group_id="-1",
            author_name="Baseline",
            text="Исторический",
            date=now - timedelta(days=1),
            is_processed=False,
            tg_notified=False,
            ai_suggested_reply="Ответ",
            status="baseline",
            updated_at=now - timedelta(days=1),
            url="https://example.invalid/3",
        ),
    ])
    db.commit()
    db.close()

    monkeypatch.setattr(main, "SessionLocal", TestSession)
    monkeypatch.setattr(main, "TG_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "ADMIN_CHAT_ID", "123")
    monkeypatch.setattr(main, "TELEGRAM_REMINDER_INTERVAL", 3600)
    monkeypatch.setattr(main, "TELEGRAM_NOTIFICATION_BATCH_LIMIT", 20)
    monkeypatch.setattr(main, "APP_STARTED_AT", now)
    monkeypatch.setattr(main, "LAST_REMINDER_BATCH_AT", now)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(main.datetime, "datetime", FakeDateTime)

    sent_ids = []

    def fake_send(comment):
        sent_ids.append(comment.external_id)
        return True

    monkeypatch.setattr(main, "send_tg_notification", fake_send)

    # On a fresh process only a genuinely unsent new row is delivered.
    assert main.send_pending_tg_notifications() == 1
    assert sent_ids == ["2"]

    # Baseline is never replayed and the already-notified comment does not
    # repeat immediately.
    sent_ids.clear()
    assert main.send_pending_tg_notifications() == 0
    assert sent_ids == []

    # After one full reminder interval, the still-unprocessed row may remind.
    later = now + timedelta(hours=1, seconds=1)

    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(main.datetime, "datetime", LaterDateTime)
    assert main.send_pending_tg_notifications() == 2
    assert sent_ids == ["1", "2"]

    # The successful reminder refreshes updated_at and the global reminder
    # batch gate prevents another immediate burst.
    # cannot send the same comment again.
    sent_ids.clear()
    assert main.send_pending_tg_notifications() == 0
    assert sent_ids == []


def test_notification_batch_limit_prevents_bursts(monkeypatch):
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    main.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    now = datetime(2026, 9, 3, 1, 0)

    db = TestSession()
    for idx in range(10):
        db.add(
            main.Comment(
                source="vk",
                external_id=str(idx),
                post_external_id="10",
                group_id="-1",
                author_name="User",
                text=f"Комментарий {idx}",
                date=now + timedelta(seconds=idx),
                is_processed=False,
                tg_notified=False,
                ai_suggested_reply="Ответ",
                status="new",
                updated_at=now,
                url=f"https://example.invalid/{idx}",
            )
        )
    db.commit()
    db.close()

    monkeypatch.setattr(main, "SessionLocal", TestSession)
    monkeypatch.setattr(main, "TG_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "ADMIN_CHAT_ID", "123")
    monkeypatch.setattr(main, "TELEGRAM_NOTIFICATION_BATCH_LIMIT", 3)
    monkeypatch.setattr(main, "APP_STARTED_AT", now)
    monkeypatch.setattr(main, "LAST_REMINDER_BATCH_AT", now)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(main.datetime, "datetime", FakeDateTime)

    sent_ids = []
    monkeypatch.setattr(
        main, "send_tg_notification",
        lambda comment: sent_ids.append(comment.external_id) or True,
    )

    assert main.send_pending_tg_notifications() == 3
    assert sent_ids == ["0", "1", "2"]


def test_short_contextual_game_questions_are_routed_to_game_context():
    cases = [
        "Сколько стоит?",
        "Когда выйдет?",
        "Когда релиз?",
        "Есть на русском?",
        "Можно играть вдвоём?",
        "Где купить?",
        "Есть в наличии?",
        "Как заказать?",
    ]
    for text in cases:
        assert main.detect_comment_intent(text) == "game_question", text
        assert main._should_include_post_context("game_question", text) is True


def test_order_checkout_problem_is_support_issue():
    assert main.detect_comment_intent("Не могу оформить заказ") == "complaint_or_issue"
    assert main.detect_comment_intent("Не получается заказать") == "complaint_or_issue"


def test_short_price_question_uses_parent_post_in_llm_prompt(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        captured["kb_post"] = post_text
        captured["allow_game_context"] = allow_game_context
        return "ДАННЫЕ ПО ИГРЕ:\nname: Грабёж!\nprice: 1990"

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Цена — 1990 ₽.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Сколько стоит?",
        "Пост про настольную игру «Грабёж!»",
        "vk",
    )

    assert captured["allow_game_context"] is True
    assert "Грабёж" in captured["kb_post"]
    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "Контекст поста" in user_prompt
    assert "Грабёж" in user_prompt


def test_release_question_does_not_treat_generic_when_as_delivery(monkeypatch):
    class Worksheet:
        def get_all_values(self):
            return [
                ["field", "unused", "value"],
                ["name", "", "Грабёж!"],
                ["delivery_estimate", "", "декабрь 2026"],
                ["release_date", "", "октябрь 2026"],
            ]

    class Spreadsheet:
        def worksheets(self):
            return [Worksheet()]

    class Client:
        def open_by_key(self, _key):
            return Spreadsheet()

    monkeypatch.setattr(main, "GOOGLE_SHEETS_ID", "sheet")
    monkeypatch.setattr(main.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(main.Credentials, "from_service_account_file", lambda *a, **k: object())
    monkeypatch.setattr(main.gspread, "authorize", lambda _creds: Client())

    context = main.get_knowledge_base_context(
        "Когда выйдет?",
        "Пост про игру Грабёж!",
        allow_game_context=True,
    )

    assert "release_date: октябрь 2026" in context
    assert "delivery_estimate: декабрь 2026" not in context


def test_question_has_priority_over_positive_feedback():
    cases = [
        ("Спасибо! Когда выйдет?", "game_question"),
        ("Круто! Сколько стоит?", "game_question"),
        ("Отлично, а есть на русском?", "game_question"),
        ("Спасибо! Можно играть вдвоём?", "game_question"),
        ("Спасибо! У вас есть вакансии?", "general_question"),
    ]
    for text, expected in cases:
        assert main.detect_comment_intent(text) == expected, text


def test_short_elliptical_questions_use_parent_post_context():
    cases = [
        "Когда будет?",
        "Когда появится?",
        "А когда будет?",
        "А сколько?",
        "Почём?",
    ]
    for text in cases:
        assert main.detect_comment_intent(text) == "game_question", text
        assert main._should_include_post_context("game_question", text) is True


def test_short_contextual_question_without_question_mark_is_still_detected():
    assert main.detect_comment_intent("Когда будет") == "game_question"
    assert main.detect_comment_intent("А сколько") == "game_question"


def test_feedback_prefixed_short_contextual_questions_use_parent_post():
    cases = [
        "Спасибо! Когда будет?",
        "Спасибо огромное! Когда появится?",
        "Круто! А сколько?",
        "Отлично! Почём?",
        "Спасибо, а когда?",
    ]
    for text in cases:
        assert main.detect_comment_intent(text) == "game_question", text
        assert main._should_include_post_context("game_question", text) is True


def test_feedback_prefixed_short_question_reaches_llm_with_parent_post(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        captured["kb_post"] = post_text
        captured["allow_game_context"] = allow_game_context
        return None

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Дата указана в посте.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Спасибо! Когда будет?",
        "Пост про настольную игру с датой выхода 15 октября",
        "vk",
    )

    assert captured["allow_game_context"] is True
    assert "датой выхода" in captured["kb_post"]
    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "Маршрут предварительной классификации: game_question" in user_prompt
    assert "Контекст поста" in user_prompt
    assert "15 октября" in user_prompt


def test_feedback_prefixed_unrelated_question_stays_general():
    # Praise must not force *every* following question into game context.
    assert main.detect_comment_intent("Спасибо! У вас есть вакансии?") == "general_question"


def test_uninformative_vo_vo_is_ignored():
    assert main.detect_comment_intent("Во, во!") == "ignore_comment"


def test_region_waiting_question_is_delivery_route():
    assert (
        main.detect_comment_intent("Аналогичный вопрос, на почте нет информации. Когда ждать регионы?")
        == "delivery_question"
    )


def test_promised_project_information_routes_to_game_context():
    assert (
        main.detect_comment_intent("Там в начале августа обещали информацию по Власти тьмы")
        == "game_question"
    )


def test_operational_faq_parser_and_priority():
    rows = [
        ["Поле", "Пояснение", "Значение"],
        ["name", "", "Тестовая игра"],
        ["📚 FAQ / ОПЕРАТИВНАЯ БАЗА ДЛЯ БОТА", "", ""],
        ["question", "", "Когда отправка по регионам?"],
        ["keywords", "", "сроки, регионы, отправка"],
        ["content", "", "Региональная отправка начнётся после комплектации партии."],
        ["project_status", "", "Комплектация партии"],
        ["sent", "", "Москва отправлена"],
        ["not_sent", "", "Регионы ещё не отправлены"],
        ["next_step", "", "Передача региональной партии в ТК"],
        ["updated_at", "", "05.09.2026"],
        ["internal_note", "", "Не обещать точную дату"],
        ["content_type", "", "operational_faq"],
        ["is_active", "", "TRUE"],
    ]
    records = main._parse_faq_records(rows)
    assert len(records) == 1
    score = main._faq_match_score(
        records[0],
        main._normalize_search_text("Когда ждать регионы?"),
        game_match=True,
    )
    assert score >= 100
    formatted = main._format_faq_context(records[0], "Тестовая игра")
    assert "Уже отправлено/выполнено: Москва отправлена" in formatted
    assert "Ещё не отправлено/не выполнено: Регионы ещё не отправлены" in formatted
    assert "Не цитировать" not in formatted  # label is descriptive, not fabricated text
    assert "Внутренняя инструкция SMM" in formatted


def test_inactive_operational_faq_is_not_selected():
    record = {
        "question": "Когда отправка?",
        "keywords": "отправка, сроки",
        "content": "Старый ответ",
        "content_type": "operational_faq",
        "is_active": "FALSE",
    }
    assert main._faq_match_score(
        record,
        main._normalize_search_text("Когда отправка?"),
        game_match=True,
    ) == 0


def test_addon_question_without_confirmed_data_gets_explicit_absence_marker():
    context = main._build_structured_game_context(
        {"name": "Пример"},
        main._normalize_search_text("А поддержка и продажа новых коробок планируется?"),
    )
    assert "НЕТ ПОДТВЕРЖДЁННОЙ ИНФОРМАЦИИ В БАЗЕ" in context


def test_uninformative_vu_vo_variant_is_ignored():
    # The review text mentions the typo/variant «ву-во» as content-free noise.
    assert main.detect_comment_intent("ву-во") == "ignore_comment"


def test_operational_faq_requires_matching_game_context():
    record = {
        "question": "Статус проекта и сроки",
        "keywords": "статус, сроки, предзаказ",
        "content": "Актуальный статус проекта",
        "content_type": "operational_faq",
        "is_active": "TRUE",
    }
    assert main._faq_match_score(
        record,
        main._normalize_search_text("В целом можно сделать пост по статусам проектов"),
        game_match=False,
    ) == 0


def test_shipping_operational_record_does_not_answer_addon_question():
    record = {
        "question": "Статус отправки по регионам",
        "keywords": "доставка, регионы, отправка, сроки",
        "content": "Региональная партия комплектуется",
        "project_status": "Комплектация",
        "content_type": "operational_faq",
        "is_active": "TRUE",
    }
    assert main._faq_match_score(
        record,
        main._normalize_search_text(
            "А поддержка и продажа новых коробок планируется? Или на первой волне всё остановится?"
        ),
        game_match=True,
    ) == 0


def test_active_but_empty_operational_record_is_not_selected():
    record = {
        "question": "Статус проекта / сроки / что отправлено?",
        "keywords": "статус, сроки, отправка",
        "content_type": "operational_faq",
        "is_active": "TRUE",
    }
    assert main._faq_match_score(
        record,
        main._normalize_search_text("Какой сейчас статус проекта?"),
        game_match=True,
    ) == 0


def test_inactive_operational_record_blocks_stale_dynamic_fallback(monkeypatch):
    class Worksheet:
        title = "Тестовая игра"

        def get_all_values(self):
            return [
                ["Поле", "Пояснение", "Значение"],
                ["name", "", "Тестовая игра"],
                ["delivery_estimate", "", "старый срок: май 2025"],
                ["📚 KNOWLEDGE_BASE (База знаний для бота)", "", ""],
                ["question", "", "Статус проекта и сроки отправки"],
                ["keywords", "", "статус, сроки, доставка, отправка"],
                ["content", "", "Непроверенная новая информация"],
                ["project_status", "", "Нужно проверить у SMM"],
                ["content_type", "", "operational_faq"],
                ["is_active", "", "FALSE"],
            ]

    class Spreadsheet:
        def worksheets(self):
            return [Worksheet()]

    class Client:
        def open_by_key(self, _key):
            return Spreadsheet()

    monkeypatch.setattr(main, "GOOGLE_SHEETS_ID", "sheet")
    monkeypatch.setattr(main.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        main.Credentials,
        "from_service_account_file",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(main.gspread, "authorize", lambda _creds: Client())

    context = main.get_knowledge_base_context(
        "Когда отправка Тестовой игры?",
        "",
        allow_game_context=True,
    )

    assert "НЕ АКТИВНА / НЕ ПРОВЕРЕНА" in context
    assert "старый срок: май 2025" not in context


def test_operational_record_with_manual_review_placeholder_is_not_selected():
    record = {
        "question": "Актуальный статус проекта",
        "keywords": "статус, сроки, производство",
        "content": "Непроверенный ответ",
        "project_status": "Непроверенный статус",
        "updated_at": "ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ SMM",
        "content_type": "operational_faq",
        "is_active": "TRUE",
    }
    assert main._faq_match_score(
        record,
        main._normalize_search_text("Какой сейчас статус проекта?"),
        game_match=True,
    ) == 0


def test_promo_shorthand_routes_to_payment_support_without_false_wet_match():
    assert main.detect_comment_intent("Почему нет промок?") == "payment_support"
    assert main.detect_comment_intent("Есть промо?") == "payment_support"
    # Exact slang support must not turn the normal Russian verb into payment.
    assert main.detect_comment_intent("Я промок под дождём") != "payment_support"


def test_addon_shorthand_routes_to_game_context_and_matches_faq():
    comment = "Хотим разобраться в допах к хай фронтиру"
    assert main.detect_comment_intent(comment) == "game_question"
    assert "addons" in main._operational_topics(main._normalize_search_text(comment))

    record = {
        "question": "Какие дополнения или модули есть для Высокого рубежа?",
        "keywords": "допы, дополнения, модули, аддоны, addon, expansion",
        "content": "Модули планируется выпускать на русском языке.",
        "content_type": "faq",
        "is_active": "TRUE",
    }
    score = main._faq_match_score(
        record,
        main._normalize_search_text(comment),
        game_match=True,
    )
    assert score > 0


def test_addon_shorthand_builds_addon_specific_context():
    context = main._build_structured_game_context(
        {
            "name": "Высокий рубеж. Для всех",
            "aliases": "High Frontier; Высокий рубеж; Хай Фронтир",
            "addons_note": "Модули будут выпускаться на русском языке.",
        },
        main._normalize_search_text("Что по допам к хай фронтиру?"),
    )
    assert "addons_note" in context
    assert "Модули будут выпускаться" in context


def test_distribution_mailing_forms_route_to_delivery_question():
    assert main.detect_comment_intent("А когда Keyflower рассылать будете?") == "delivery_question"
    assert main.detect_comment_intent("Когда начнётся рассылка Keyflower?") == "delivery_question"
    assert main.detect_comment_intent("Заказы по регионам уже рассылаете?") == "delivery_question"
    assert "delivery" in main._operational_topics(
        main._normalize_search_text("Когда начнётся рассылка Keyflower?")
    )


def test_newsletter_mailing_is_not_treated_as_order_delivery():
    assert main.detect_comment_intent("Как подписаться на рассылку новостей?") != "delivery_question"


def test_distribution_mailing_builds_delivery_specific_game_context():
    context = main._build_structured_game_context(
        {
            "name": "Keyflower",
            "delivery_estimate": "После подтверждения SMM",
        },
        main._normalize_search_text("Когда Keyflower рассылать будете?"),
    )
    assert "delivery_estimate" in context


def test_promo_payment_route_includes_parent_post_context(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/chat")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    captured = {}

    def fake_kb(comment_text, post_text="", max_results=3, *, allow_game_context=True):
        # Payment route still blocks structured game data; parent-post context
        # is passed directly to the LLM only for promo-specific questions.
        assert post_text == ""
        assert allow_game_context is False
        return None

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _fake_llm_response("Проверьте условия промокода в посте.")

    monkeypatch.setattr(main, "get_knowledge_base_context", fake_kb)
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.generate_ai_reply(
        "Почему нет промок?",
        "В этом посте действует промокод STAR10 только на товары из подборки.",
        "vk",
    )

    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "payment_support" in user_prompt
    assert "Контекст поста" in user_prompt
    assert "STAR10" in user_prompt


def test_plain_card_payment_still_does_not_include_unrelated_post():
    assert (
        main._should_include_post_context(
            "payment_support", "Не проходит оплата картой, что делать?"
        )
        is False
    )
