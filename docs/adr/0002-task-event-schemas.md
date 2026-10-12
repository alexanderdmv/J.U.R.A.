# ADR-002 — Схемы задач и событий (v0)
**Статус:** Draft — заморозка после walking skeleton

## Решение (черновик)
- Всё входящее — `Envelope` (`contracts/types.py`): принципал, устройство, уровень аутентификации, доверие, зона, `schema_version`.
- Событие журнала: `seq, ts, kind, actor, task_id, payload(JSON), schema_version, prev_hash, hash`.
- Имена событий: `task.created`, `task.transition`, `tool.decision`, `tool.executed`, `tool.failed`, `tool.unknown`,
  `model.call`, `model.failed`, `policy.changed`.
- **В payload — только имена, хэши, метки зон. Значений аргументов и текстов запросов там нет.**
- Контракты — dataclass'ы; экспорт в JSON Schema будет добавлен до появления компонента на втором языке.

## Последствия
`schema_version` есть с первого дня — это позволит позже читать старые события (ADR-009).
