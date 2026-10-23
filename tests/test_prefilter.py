from scheduler import keyword_matches, passes_prefilter


def test_short_keywords_use_word_boundaries() -> None:
    assert keyword_matches("нужен js разработчик", "js") is True
    assert keyword_matches("нужен json парсер", "js") is False
    assert keyword_matches("работа с сайтом", "бот") is False
    assert keyword_matches("телеграм бот для записи", "бот") is True
    assert keyword_matches("бот на python", "python") is True
    assert keyword_matches("нужен node.js", "js") is True


def test_prefilter_exclude_wins() -> None:
    assert passes_prefilter(
        "Логотип для бота",
        "python",
        include=["python", "бот"],
        exclude=["логотип"],
    ) is False


def test_prefilter_requires_an_include_hit() -> None:
    assert passes_prefilter(
        "Нарисовать баннер",
        "для кафе",
        include=["python", "fastapi"],
        exclude=[],
    ) is False
    assert passes_prefilter(
        "API на fastapi",
        "",
        include=["fastapi"],
        exclude=[],
    ) is True
