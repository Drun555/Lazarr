"""Language normalization shared by forms, filenames and provider descriptions."""

import re

LABELS = {
    "ru": "Русский",
    "ja": "Японский",
    "en": "Английский",
    "uk": "Украинский",
    "de": "Немецкий",
    "fr": "Французский",
    "es": "Испанский",
    "it": "Итальянский",
    "zh": "Китайский",
    "ko": "Корейский",
    "pt": "Португальский",
    "pl": "Польский",
    "ar": "Арабский",
    "hi": "Хинди",
    "tr": "Турецкий",
    "nl": "Нидерландский",
}
CODES = {
    "ru": "rus russian рус",
    "ja": "jpn jap japanese",
    "en": "eng english англ",
    "uk": "ukr ukrainian",
    "de": "deu ger german",
    "fr": "fra fre french",
    "es": "spa spanish",
    "it": "ita italian",
    "zh": "zho chi chinese",
    "ko": "kor korean",
    "pt": "por portuguese",
    "pl": "pol polish",
    "ar": "ara arabic",
    "hi": "hin hindi",
    "tr": "tur turkish",
    "nl": "nld dut dutch",
}
ALIASES = {}
for code, label in LABELS.items():
    for value in [code, label.lower(), *CODES[code].split()]:
        ALIASES[value] = code
    if label.endswith("ий"):
        stem = label[:-2].lower()
        for ending in ("ая", "ое", "ие", "ого", "ой", "их", "ом", "ую"):
            ALIASES[stem + ending] = code
ALIASES["яп"] = "ja"


def language(value):
    value = (value or "").strip().casefold().replace("_", "-")
    return ALIASES.get(value, ALIASES.get(value.split("-")[0], "und"))


def extract_languages(text):
    return list(
        dict.fromkeys(
            code for word in re.findall(r"[a-zа-яё]+", text.casefold()) if (code := language(word)) != "und"
        )
    )


def language_name(code):
    return LABELS.get(code, "Не определён")
