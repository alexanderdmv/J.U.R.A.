"""Диалоговое ядро v0: реплика владельца → модель → ответ (ADR-015).

Что делает:
  * держит короткую историю разговора в оперативной памяти (на диск не пишется; журнал видит только факт вызова модели);
  * даёт модели инструкцию: отвечать коротко, разговорно и в той же смеси языков (ru/uk/cs/en), что и владелец;
  * подкладывает из личного словаря ТОЛЬКО слова, которые прозвучали в разговоре (иначе маленькое окно модели переполнится);
  * обрезает старые реплики, чтобы запрос влез в окно контекста модели;
  * чистит ответ для озвучки (без markdown, эмодзи, ссылок).
Чего пока НЕ делает: не вызывает инструменты — действия появятся через ToolPort и политики, как в остальном J.U.R.A.

Зона разговора — P (приватное): роутер отдаёт его только локальной модели. Это правило не настраивается здесь.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from jura.contracts.model import ModelRequest, ModelResponse, Turn
from jura.contracts.types import Zone

SYSTEM_PROMPT = """Ты — J.U.R.A. (Юра), личный голосовой ассистент и правая рука владельца. Говори как живой собеседник: \
коротко (обычно 1–3 предложения), по делу, дружелюбно, без канцелярита. Ответ будет озвучен: никакого markdown, списков, \
эмодзи, ссылок и кода — только обычная речь.
Языки: владелец говорит по-русски, по-украински, по-чешски и по-английски и часто смешивает их даже в одном предложении. \
Отвечай в том же стиле: на том же языке и с той же смесью — если он вставляет английские, чешские или украинские слова, \
вставляй такие же и ты. Не переводи его слова и не поправляй его.
Сленг понимай и используй естественно, если владелец сам так говорит.
Текст распознан из речи и может содержать ошибки распознавания — угадывай смысл по контексту. Если не понял — коротко переспроси.
Если просят что-то сделать (отправить, купить, открыть, перевести деньги), честно скажи, что пока ты умеешь только \
разговаривать, а действия появятся позже.
У тебя нет доступа к интернету, погоде, новостям, курсам, почте, календарю и часам. Никогда не выдумывай такие сведения: \
честно скажи, что пока этого не знаешь."""

MAX_LEXICON_HITS = 12


@dataclass(frozen=True)
class LexiconEntry:
    term: str
    meaning: str
    also: tuple[str, ...] = ()   # как ещё это может прозвучать или распознаться

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(_contains_word(low, w.lower()) for w in (self.term, *self.also) if w.strip())


@dataclass(frozen=True)
class Reply:
    text: str            # как ответила модель
    spoken: str          # то же, очищенное для озвучки
    model: str
    tokens_in: int
    tokens_out: int
    seconds: float


class Dialogue:
    def __init__(self, complete: Callable[[ModelRequest], ModelResponse], *, lexicon: Callable[[], Sequence[LexiconEntry]] = tuple,
                 zone: Zone = Zone.P, max_prompt_chars: int = 3200, max_turns: int = 16,
                 system_prompt: str = SYSTEM_PROMPT):
        self._complete, self._lexicon, self.zone = complete, lexicon, zone
        self._max_chars, self._max_turns, self._system = max_prompt_chars, max_turns, system_prompt
        self.history: list[Turn] = []

    def reset(self) -> None:
        self.history.clear()

    def _system_for(self, text: str) -> str:
        recent = " ".join([text, *(t.text for t in self.history[-4:])])
        hits = [e for e in self._lexicon() if e.matches(recent)][:MAX_LEXICON_HITS]
        if not hits:
            return self._system
        lines = [f"- «{e.term}»" + (f" (может прозвучать как: {', '.join(e.also)})" if e.also else "") + f" — {e.meaning}"
                 for e in hits]
        return self._system + "\nЛичный словарь владельца (эти слова значат именно это):\n" + "\n".join(lines)

    def _request(self, text: str) -> ModelRequest:
        system = self._system_for(text)
        history = self.history[-self._max_turns:]
        size = lambda h: len(system) + len(text) + sum(len(t.text) for t in h)  # noqa: E731
        while history and size(history) > self._max_chars:  # самые старые реплики уходят первыми
            history = history[2:]
        # ~2.5 символа на токен для кириллицы + место под ответ
        return ModelRequest(text, self.zone, system=system, history=tuple(history), prefer="local",
                            min_context=size(history) * 2 // 5 + 300)

    def reply(self, text: str) -> Reply:
        text = text.strip()
        started = time.perf_counter()
        response = self._complete(self._request(text))
        seconds = time.perf_counter() - started
        answer = response.text.strip()
        self.history += [Turn("user", text), Turn("assistant", answer)]
        return Reply(answer, for_speech(answer), response.model, response.tokens_in, response.tokens_out, seconds)


# ------------------------------------------------------------------ помощники ---
def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_URL = re.compile(r"https?://\S+")


def for_speech(text: str) -> str:
    """Убирает то, что нельзя или незачем произносить: разметку, эмодзи, ссылки, маркеры списков."""
    text = _URL.sub("ссылка", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"[*_#`>|~]+", "", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()
