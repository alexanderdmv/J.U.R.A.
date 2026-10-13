"""Командная строка J.U.R.A.

Первый запуск на новом компьютере:
    pip install -e .                     — поставить проект (всё нужное — одной командой)
    python -m jura setup                 — скачать то, что pip не ставит: браузер Chromium и модель распознавания речи
    python -m jura init                  — создать хранилище, зашифрованные базы и БУМАЖНЫЙ КОД
Работа (пароль вводится один раз):
    python -m jura shell                 — интерактивная оболочка (рекомендуется); в ней: help
Те же команды по одной (каждая спрашивает пароль):
    secret-set / secret-get / secret-list / secret-delete ИМЯ     memory-add ТЕКСТ     memory-search СЛОВА
    backup [--to ПАПКА] [--keep N]   backup-list   status   tools   policy …   run …   site …   leak-check ТЕКСТ
    model list|add|remove|discover|test …    ask [--zone O|P|S1] [--model ИМЯ] ТЕКСТ
    talk [--typed] [--mute] — разговор голосом     chat [ТЕКСТ] — текстом     lexicon list|add|remove — личный словарь
Без пароля:
    verify-log · peek-raw · voice-check · listen [--file WAV] [--remember] · speak ТЕКСТ · echo
Ключи и копии:
    change-passphrase · new-recovery-code · vault-check-recovery
    backup-verify ФАЙЛ · restore ФАЙЛ --to ПАПКА

Где что лежит — настройки этого компьютера (jura.local.json рядом с проектом): python -m jura config
    config set home|models_dir|backup_dir ПУТЬ   (по умолчанию данные — ~/.jura, модели — ~/.jura-models; копии — другой диск!)
"""

from __future__ import annotations

import argparse
import sys

from jura import cli_setup, cli_standalone as cs
from jura.cli_shell import cmd_shell, run_session_command
from jura.contracts.errors import JuraError, VaultError

# одиночные команды консоли → токены команд оболочки (в т.ч. старые имена vault-*)
SESSION_COMMANDS = {
    "status": ["status"],
    "secret-set": ["secret", "set"], "vault-set": ["secret", "set"],
    "secret-get": ["secret", "get"],
    "secret-list": ["secret", "list"], "vault-list": ["secret", "list"],
    "secret-delete": ["secret", "delete"],
    "memory-add": ["memory", "add"], "memory-search": ["memory", "search"],
    "backup": ["backup"], "backup-list": ["backup-list"], "leak-check": ["leak-check"],
    "tools": ["tools"], "policy": ["policy"], "run": ["run"], "site": ["site"],
    "model": ["model"], "ask": ["ask"],
    "talk": ["talk"], "chat": ["chat"], "lexicon": ["lexicon"],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jura", description="J.U.R.A.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", aliases=["vault-init"]).set_defaults(fn=cs.cmd_init)
    sub.add_parser("shell").set_defaults(fn=cmd_shell)
    st = sub.add_parser("setup")
    st.add_argument("--skip-browser", action="store_true", help="не скачивать Chromium")
    st.add_argument("--skip-voice", action="store_true", help="не скачивать модель речи и не проверять голос")
    st.add_argument("--skip-llm", action="store_true", help="не готовить локальную языковую модель в Ollama")
    st.set_defaults(fn=cli_setup.cmd_setup)

    cf = sub.add_parser("config", help="настройки этого компьютера: где данные, модели, копии")
    cf.add_argument("action", nargs="?", choices=["show", "set", "unset"], default="show")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(fn=cli_setup.cmd_config)

    sub.add_parser("verify-log").set_defaults(fn=cs.cmd_verify_log)
    sub.add_parser("peek-raw").set_defaults(fn=cs.cmd_peek_raw)
    sub.add_parser("change-passphrase").set_defaults(fn=cs.cmd_change_passphrase)
    sub.add_parser("new-recovery-code").set_defaults(fn=cs.cmd_new_recovery_code)
    sub.add_parser("vault-check-recovery").set_defaults(fn=cs.cmd_vault_check_recovery)

    sub.add_parser("voice-check").set_defaults(fn=cs.cmd_voice_check)
    li = sub.add_parser("listen")
    li.add_argument("--file", help="распознать WAV-файл (16 кГц, моно, 16 бит) вместо микрофона")
    li.add_argument("--remember", action="store_true", help="сохранить услышанное в память")
    li.add_argument("--zone", default="P", choices=["O", "P", "S1"])
    li.set_defaults(fn=cs.cmd_listen)
    sp = sub.add_parser("speak")
    sp.add_argument("text", nargs="+")
    sp.set_defaults(fn=cs.cmd_speak)
    sub.add_parser("echo").set_defaults(fn=cs.cmd_echo)

    v = sub.add_parser("backup-verify")
    v.add_argument("file")
    v.set_defaults(fn=cs.cmd_backup_verify)
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--to", required=True, help="пустая папка для восстановления")
    r.set_defaults(fn=cs.cmd_restore)
    return parser


def _system_certificates() -> None:
    """Проверять сертификаты сайтов средствами Windows (truststore). На свежей Windows Python иначе падает на github.com,
    huggingface.co и др.: нужных корневых сертификатов ещё нет, а докачивать их умеет только сама Windows."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _system_certificates()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] in SESSION_COMMANDS:
            return run_session_command(SESSION_COMMANDS[argv[0]] + argv[1:])
        args = build_parser().parse_args(argv)
        return args.fn(args)
    except KeyboardInterrupt:
        print()
        return 130
    except VaultError as exc:
        print(f"Ошибка хранилища: {exc}")
        return 1
    except JuraError as exc:
        print(f"Ошибка: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
