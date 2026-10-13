"""Личный словарь владельца: сленг, свои словечки, имена — на любом из языков (ADR-015).

Хранится в зашифрованном хранилище (`__lexicon__`), как настройки моделей: на диске его не прочитать без ключа.
Диалог подкладывает модели только те слова, что прозвучали в разговоре (`runtime/dialogue.py`).
"""

from __future__ import annotations

import json

from jura.contracts.errors import JuraError, VaultError
from jura.kernel.vault import Vault
from jura.runtime.dialogue import LexiconEntry

LEXICON_KEY = "__lexicon__"
MAX_TERM, MAX_MEANING = 60, 300


class Lexicon:
    def __init__(self, vault: Vault):
        self._vault = vault

    def entries(self) -> list[LexiconEntry]:
        try:
            raw = json.loads(self._vault.get(LEXICON_KEY))
        except VaultError:
            return []
        return [LexiconEntry(e["term"], e["meaning"], tuple(e.get("also", ()))) for e in raw]

    def _save(self, entries: list[LexiconEntry]) -> None:
        data = [{"term": e.term, "meaning": e.meaning, "also": list(e.also)} for e in entries]
        self._vault.set(LEXICON_KEY, json.dumps(data, ensure_ascii=False))

    def add(self, term: str, meaning: str, also: tuple[str, ...] = ()) -> LexiconEntry:
        """Добавляет слово или заменяет значение уже известного."""
        term, meaning = term.strip(), meaning.strip()
        also = tuple(a.strip() for a in also if a.strip())
        if not term or not meaning:
            raise JuraError("нужно и слово, и значение: lexicon add СЛОВО = ЗНАЧЕНИЕ")
        if len(term) > MAX_TERM or len(meaning) > MAX_MEANING or any(len(a) > MAX_TERM for a in also):
            raise JuraError(f"слишком длинно: слово до {MAX_TERM} символов, значение до {MAX_MEANING}")
        entry = LexiconEntry(term, meaning, also)
        self._save([e for e in self.entries() if e.term.lower() != term.lower()] + [entry])
        return entry

    def remove(self, term: str) -> None:
        entries = self.entries()
        kept = [e for e in entries if e.term.lower() != term.strip().lower()]
        if len(kept) == len(entries):
            raise JuraError(f"слова «{term}» в словаре нет")
        self._save(kept)
