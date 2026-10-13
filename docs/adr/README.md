# ADR — журнал архитектурных решений

Формат: **Контекст → Решение → Альтернативы → Последствия**. Статусы: `Draft` (проверяем на практике),
`Accepted` (принято и реализовано), `Planned` (ещё не написан).

| № | Тема | Статус |
|---|---|---|
| [000](0000-stack.md) | Язык и стек | Accepted |
| [001](0001-storage-ownership.md) | Роли хранилищ: состояние и история | Accepted |
| [002](0002-task-event-schemas.md) | Схемы задач и событий | Draft |
| [003](0003-authorization.md) | Модель авторизации и политик | Accepted |
| [004](0004-transaction-outbox.md) | Транзакции и outbox | Draft |
| [005](0005-data-classification.md) | Классификация данных и зоны | Accepted (частично реализовано) |
| [006](0006-model-tool-contracts.md) | Контракты моделей и инструментов | Draft |
| 007 | Изоляция плагинов | Planned (Phase 1) |
| [008](0008-key-hierarchy-and-encryption.md) | Иерархия ключей, шифрование данных, копии | Accepted |
| 009 | Версионирование событий (upcasting) | Planned |
| 010 | Трейсы и опыт: что логируем с учётом зон | Planned (Phase 1) |
| [011](0011-voice-stack.md) | Голосовой стек | Accepted; распознавание и синтез заменены в ADR-015 |
| [012](0012-tool-results-and-credential-broker.md) | Метки результатов инструментов, брокер учётных данных | Accepted |
| [013](0013-model-adapters.md) | Адаптеры моделей, локальная среда | Accepted (вживую не проверен) |
| [014](0014-installer.md) | Установщик для нового компьютера | Accepted; дополнен ADR-016 (без winget, VC++, настройки) |
| [015](0015-multilingual-voice-dialogue.md) | Многоязычный голос (ru/uk/cs/en) и диалоговое ядро v0 | Accepted (проверен вживую, с микрофоном) |
| [016](0016-machine-settings-and-bare-windows-installer.md) | Настройки компьютера (jura.local.json) и установка на голый Windows | Accepted (проверен в Windows Sandbox) |

Контракты v1 замораживаются **после** walking skeleton. Черновики (Draft) можно менять; Accepted — только новым ADR.
