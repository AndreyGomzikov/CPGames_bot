from src.comment_policy import get_ignore_reason, is_uninformative_comment


def test_noise_variants_are_uninformative():
    for text in ("Во, во!", "во-во", "ву-во", "+1", "ага", "угу"):
        assert is_uninformative_comment(text) is True, text


def test_real_question_is_not_suppressed():
    assert is_uninformative_comment("Когда ждать регионы?") is False
    assert is_uninformative_comment("А сколько?") is False


def test_vk_comment_from_own_group_is_ignored():
    reason = get_ignore_reason(
        "Пенумбра активно рассылается по всем регионам",
        source="vk",
        author_id="-147452506",
        author_name="Фабрика игр",
        group_id="-147452506",
    )
    assert reason == "own_brand"


def test_brand_name_fallback_for_telegram():
    reason = get_ignore_reason(
        "Служебный комментарий",
        source="telegram",
        author_name="Фабрика игр",
    )
    assert reason == "own_brand"


def test_telegram_comment_from_own_channel_username_is_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL", "fabrica_igr")
    reason = get_ignore_reason(
        "Ответ от канала",
        source="telegram",
        author_id="-100777",
        author_name="Другое отображаемое имя",
        author_username="fabrica_igr",
        author_peer_type="channel",
        group_id="fabrica_igr",
    )
    assert reason == "own_brand"


def test_telegram_comment_from_own_channel_id_is_ignored():
    reason = get_ignore_reason(
        "Ответ от имени канала",
        source="telegram",
        author_id="-100777",
        author_entity_id="777",
        author_name="Совсем другое имя",
        author_peer_type="channel",
        group_id="fabrica_igr",
        own_author_ids=("777", "888"),
    )
    assert reason == "own_brand"


def test_telegram_comment_from_linked_discussion_group_id_is_ignored():
    reason = get_ignore_reason(
        "Ответ от имени группы обсуждения",
        source="telegram",
        author_id="-100888",
        author_entity_id="888",
        author_name="Обсуждение магазина",
        author_peer_type="channel",
        group_id="fabrica_igr",
        own_author_ids=("777", "888"),
    )
    assert reason == "own_brand"


def test_telegram_regular_user_with_same_raw_number_is_not_mistaken_for_group():
    reason = get_ignore_reason(
        "Когда будет доставка?",
        source="telegram",
        author_id="777",
        author_entity_id="777",
        author_name="Иван",
        author_username="ivan",
        author_peer_type="user",
        group_id="fabrica_igr",
        own_author_ids=("777",),
    )
    assert reason is None


def test_vk_comment_from_own_group_is_ignored_when_config_id_is_positive():
    reason = get_ignore_reason(
        "Служебный ответ от сообщества",
        source="vk",
        author_id="-147452506",
        author_name="User -147452506",
        group_id="147452506",
    )
    assert reason == "own_brand"
