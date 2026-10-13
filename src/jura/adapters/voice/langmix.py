"""Разметка языков внутри фразы: русский, украинский, чешский, английский (ADR-015).

Нужна синтезу речи: один голос читает всю фразу, но каждый кусок произносится по правилам своего языка.
Только стандартная библиотека и простые правила — без моделей, чтобы работало мгновенно и предсказуемо:
  1. буква выдаёт язык: і ї є ґ — украинский; ы э ъ ё — русский; á č ď é ě í ň ó ř š ť ú ů ý ž — чешский;
  2. частые служебные слова («що», «это», «jsem», «the»…) — тоже улики;
  3. слово без улик берёт язык своей группы (подряд идущие слова одного алфавита), потом предложения, потом всего текста;
  4. если улик нет совсем: кириллица — русский, латиница — английский.
Ошибка разметки не опасна: слово просто прозвучит с акцентом.
"""

from __future__ import annotations

import re

LANGS = ("ru", "uk", "cs", "en")

_UK_LETTERS = set("іїєґ")
_RU_LETTERS = set("ыэъё")
_CS_LETTERS = set("áčďéěíňóřšťúůýž")
# Только слова, которые есть в одном языке своего алфавита («так», «to», «a», «no» — в обоих, их нет).
_WORDS = {
    "uk": {"це", "що", "як", "але", "та", "й", "його", "її", "ми", "ви", "вони", "мене", "тебе", "дуже", "тут", "ні",
           "добре", "буде", "треба", "можна", "сьогодні", "зараз", "просто", "дякую", "привіт", "нема", "немає", "чи",
           "або", "коли", "чому", "де", "вже", "ще", "теж", "також", "будь", "ласка", "мені", "тобі", "хто"},
    "ru": {"что", "это", "как", "но", "только", "очень", "нужно", "можно", "сейчас", "его", "он", "она", "они", "мы",
           "вы", "меня", "тебя", "хорошо", "спасибо", "привет", "нет", "или", "когда", "почему", "где", "уже", "ещё",
           "тоже", "также", "пожалуйста", "мне", "тебе", "кто", "сегодня", "чтобы", "если", "вот", "был", "была"},
    "cs": {"je", "jsem", "jsi", "jsme", "se", "na", "ale", "jak", "prosím", "děkuji", "ahoj", "dneska", "taky", "já",
           "ty", "co", "kde", "kdy", "není", "bude", "mám", "máš", "jo", "ne", "teď", "proč", "nebo", "když", "už",
           "ještě", "tady", "tam", "dobře", "hele", "fakt", "moc", "potřebuju", "můžeš", "budeme"},
    "en": {"the", "is", "are", "you", "i", "it", "and", "of", "in", "what", "how", "ok", "okay", "yes", "please",
           "thanks", "hello", "hi", "let's", "lets", "go", "do", "can", "this", "that", "with", "for", "my", "your",
           "just", "really", "sorry", "cool", "nice", "any", "tips", "even", "can't", "don't", "it's", "i'm"},
}
_TOKEN = re.compile(r"[^\W\d_]+(?:['’ʼ-][^\W\d_]+)*|\s+|.", re.UNICODE)
_SENTENCE_END = set(".!?…\n")


def _script(word: str) -> str | None:
    for ch in word:
        if "а" <= ch.lower() <= "я" or ch.lower() in "ёіїєґ":
            return "cyr"
        if ch.isalpha() and ch.isascii() or ch.lower() in _CS_LETTERS:
            return "lat"
    return None


def word_clue(word: str) -> str | None:
    """Язык, который слово выдаёт само по себе, или None, если улик нет."""
    low = word.lower().replace("’", "'").replace("ʼ", "'")
    letters = set(low)
    if letters & _UK_LETTERS:
        return "uk"
    if letters & _RU_LETTERS:
        return "ru"
    if letters & _CS_LETTERS:
        return "cs"
    for lang, words in _WORDS.items():
        if low in words:
            return lang
    return None


def _vote(clues: list[str | None], script: str) -> str | None:
    own = ("uk", "ru") if script == "cyr" else ("cs", "en")
    counts = {lang: clues.count(lang) for lang in own}
    best = max(own, key=lambda lang: counts[lang])
    return best if counts[best] else None


def segments(text: str) -> list[tuple[str | None, str]]:
    """Делит текст на куски одного языка: [(язык, текст)]. Пробелы и знаки препинания прилипают к соседнему куску;
    у текста без букв язык None."""
    tokens = _TOKEN.findall(text)
    scripts = [_script(t) for t in tokens]
    clues = [word_clue(t) if s else None for t, s in zip(tokens, scripts)]

    # группы: подряд идущие слова одного алфавита (пробелы и знаки между ними группу не рвут)
    group_of: list[int] = []
    groups: list[list[int]] = []
    sentence_of: list[int] = []
    sentence, last_script = 0, None
    for i, (tok, s) in enumerate(zip(tokens, scripts)):
        if s and s != last_script:
            groups.append([])
            last_script = s
        if s:
            groups[-1].append(i)
        group_of.append(len(groups) - 1)
        sentence_of.append(sentence)
        if any(ch in _SENTENCE_END for ch in tok):
            sentence += 1
            last_script = None

    langs: list[str | None] = [None] * len(tokens)
    for members in groups:
        script = scripts[members[0]]
        group_lang = _vote([clues[i] for i in members], script)
        for i in members:
            sent = [clues[j] for j in range(len(tokens)) if sentence_of[j] == sentence_of[i] and scripts[j] == script]
            langs[i] = (clues[i] or group_lang or _vote(sent, script)
                        or _vote([c for c, s2 in zip(clues, scripts) if s2 == script], script)
                        or ("ru" if script == "cyr" else "en"))

    out: list[list] = []
    for tok, lang in zip(tokens, langs):
        if lang is None and out:      # пробел или знак — к предыдущему куску
            out[-1][1] += tok
        elif out and (out[-1][0] == lang or out[-1][0] is None):
            out[-1][0] = lang if out[-1][0] is None else out[-1][0]
            out[-1][1] += tok
        else:
            out.append([lang, tok])
    return [(lang, chunk) for lang, chunk in out]


def main_language(text: str) -> str | None:
    """Язык большей части текста (по числу букв) — например, чтобы выбрать подходящее слово для ответа."""
    weight: dict[str, int] = {}
    for lang, chunk in segments(text):
        if lang:
            weight[lang] = weight.get(lang, 0) + sum(ch.isalpha() for ch in chunk)
    return max(weight, key=weight.get) if weight else None
