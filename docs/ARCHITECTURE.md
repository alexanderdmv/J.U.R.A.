# Архитектура: соответствие диаграммам и коду

Источник: `docs/diagrams/Jarvis_Architecture_v4_1.drawio` (13 страниц). Здесь — где каждая идея живёт в коде.

## Слои и правило зависимостей

```
app / __main__  ─►  всё
runtime         ─►  kernel, contracts
adapters        ─►  contracts
kernel          ─►  contracts
contracts       ─►  (ничего)
```
Правило проверяет `tests/test_architecture.py`: нарушение границы ломает тесты. Ядро использует только
стандартную библиотеку; единственное исключение — `cryptography` в `kernel/vault.py`.

## Диаграмма → код

| Стр. | Идея | Где в коде | Статус |
|---|---|---|---|
| 01 | Envelope (принципал, устройство, auth, доверие) | `contracts/types.py` | ✅ |
| 01 | Event Log, Audit (tamper-evident) | `kernel/events.py` | ✅ (внешний якорь — вручную) |
| 01/03 | Task State Machine | `kernel/tasks.py` | ✅ |
| 01 | Policy Engine | `kernel/policy.py` | ✅ (политика задана в коде) |
| 01/04 | Tool Port (PEP) | `kernel/tools.py` | ✅ |
| 01 | Model Port + Router | `contracts/model.py`, `runtime/router.py` | ✅ (реальных моделей пока нет) |
| 01/05 | Memory Port | `contracts/memory.py`, `adapters/memory/` | ✅ базово (поиск по словам) |
| 03 | Fast path | `app.py: execute_fast` | ✅ |
| 03 | Full path (Intent → Plan → Verify) | правила в `tasks.py` | ⏳ логики агентов нет |
| 03 | Transactional outbox | `kernel/outbox.py` | ✅ минимально |
| 04 | Принципалы, taint, зоны, step-up | `policy.py`, `contracts/types.py` | ✅ |
| 04 | Secret Vault | `kernel/vault.py` | ✅ |
| 07 | Трейсинг, evals, replay | — | ⏳ Phase 1 |
| 08 | Initiative Engine, Standing Orders | — | ⏳ Phase 5 |
| 09 | Redaction Gateway | — | ⏳ Phase 1 (v0) |
| 09 | Шифрование данных на диске (журнал, задачи, память) | `kernel/crypto.py`, `kernel/keyring.py`, `contracts/crypto.py` | ✅ (ADR-008) |
| 06 | Резервные копии + восстановление | `kernel/backup.py` | ✅ локально; облако ⏳ |
| — | Сессия (разблокировка, команды) | `session.py`, `__main__.py` | ✅ |
| 10 | Голос: запись, распознавание (GigaAM), синтез (SAPI) | `voice.py`, `adapters/voice/` | ✅ вход/выход; ⏳ ответы по смыслу, слово-активатор |
| 11 | Finance Enclave | — | ⏳ Phase 7 |
| 06 | Узлы, протокол, mTLS, Face ID | — | ⏳ Phase 2–3 |

## Как проходит запрос (то, что уже работает)

```
Envelope ──► Jura.execute_fast
              │  создаёт задачу (состояние + событие — одной транзакцией)
              ▼
         ToolPort.call
              │  1. находит инструмент
              │  2. PolicyEngine.decide  → разрешить / спросить / запретить
              │  3. пишет решение в журнал (без значений аргументов)
              │  4. защита от повторного выполнения необратимых действий (outbox)
              │  5. выполняет обработчик
              ▼
         результат → задача DONE/FAILED/AWAITING_APPROVAL → всё в журнале
```
Модель в этой цепочке может только *предложить* вызов; решает политика.

## Что сознательно упрощено в Phase 0

- Ожидающие подтверждения вызовы хранятся в памяти процесса (после перезапуска теряются) — `app.py`.
- Хранилище — SQLite за интерфейсами; PostgreSQL появится адаптером (см. ADR-001).
- Контракты — обычные dataclass'ы. Экспорт в JSON Schema — до появления компонента на втором языке (ADR-002).
- Политика — Python-конфиг. Декларативный формат с тестами — позже.

## Добавлено в v0.5
| Что | Где |
|---|---|
| Метки результатов инструментов (`ToolResult`) | `contracts/types.py`, `kernel/tools.py` |
| Брокер учётных данных (чтение секретов коннекторами по шаблонам имён + аудит) | `kernel/credentials.py` |
| P — только локальные модели, облако только для O | `runtime/router.py` |
| Проверка origin при входе через браузер | `adapters/connectors/web_login.py` |
| Консоль разнесена на три файла | `__main__.py` (разбор), `cli_shell.py` (команды сессии), `cli_standalone.py` (прочее) |
| Подставные модели вынесены из продукта | `tests/fakes.py` |
| Адаптеры моделей (Claude, OpenAI, Gemini, локальная) + общий безопасный HTTP | `adapters/models/` |
| Реестр моделей (зашифрованная настройка, ключи через брокер) | `model_registry.py` |
| Автоматическая проверка на GitHub | `.github/workflows/tests.yml` |
