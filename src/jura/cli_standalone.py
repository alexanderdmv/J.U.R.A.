"""Команды, которым не нужна разблокированная сессия (или которые сами её открывают и закрывают)."""

from __future__ import annotations

import getpass
from pathlib import Path

from jura import machine
from jura.cli_shell import ZONES, open_session
from jura.contracts.errors import JuraError
from jura.kernel.backup import restore_backup, verify_backup
from jura.kernel.db import Database
from jura.kernel.events import EventLog
from jura.kernel.vault import Vault
from jura.session import Session, default_home, peek_raw
from jura.voice import build_voice_stack, diagnose, echo_loop, listen_once


def home() -> Path:
    return default_home()


# ---------------------------------------------------------------- хранилище ---
def _show_recovery_code(code: str) -> None:
    print("\n" + "=" * 70)
    print("БУМАЖНЫЙ КОД ВОССТАНОВЛЕНИЯ — показывается ОДИН раз:\n")
    print(f"    {code}\n")
    print("Перепиши его ОТ РУКИ на бумагу (не фото, не облако, не файл).")
    print("Сделай две копии и храни в разных местах.")
    print("Если потеряешь и пароль, и код — данные не восстановить никому.")
    print("=" * 70)


def _confirm_recovery_code(session: Session) -> bool:
    """Просит ввести код с бумаги (ввод скрыт) и проверяет, что он подходит."""
    for _ in range(3):
        typed = getpass.getpass("\nВведи код с бумаги, чтобы проверить, что записал верно (ввод скрыт): ")
        if session.vault.check_recovery_code(typed):
            print("✓ Код записан верно.")
            return True
        print("✗ Не подходит. Проверь запись.")
    return False


def _connect_local_model(session: Session) -> None:
    """Подключает к новому хранилищу локальную модель, которую подготовил setup (имя — в настройках компьютера)."""
    name = machine.get("local_model")
    if not name or any(e.model == name for e in session.registry.entries()):
        return
    try:
        session.model_add("local-main", "local", name, context=2048, max_output=300)
        print(f"✓ Подключена локальная модель {name} (разговор: python -m jura shell → talk).")
    except JuraError as exc:
        print(f"⚠ Локальную модель подключить не удалось: {exc}")


def cmd_init(_args) -> int:
    h = home()
    if (h / "jura.vault").exists():
        print(f"Хранилище уже существует: {h / 'jura.vault'}")
        return 1
    p1 = getpass.getpass("Придумай пароль (не короче 12 символов, лучше фраза из 5+ слов): ")
    if p1 != getpass.getpass("Повтори пароль: "):
        print("Пароли не совпали.")
        return 1
    session, code = Session.create(h, p1)
    _show_recovery_code(code)
    confirmed = _confirm_recovery_code(session)
    _connect_local_model(session)
    session.close()
    if not confirmed:
        print("Код не подтверждён. Проверь запись: команда vault-check-recovery.")
    print(f"\nГотово. Данные: {h}\nДальше: python -m jura shell")
    return 0


def cmd_change_passphrase(_args) -> int:
    session = open_session()
    try:
        new = getpass.getpass("Новый пароль (не короче 12 символов, лучше фраза из 5+ слов): ")
        if new != getpass.getpass("Повтори новый пароль: "):
            print("Пароли не совпали — ничего не изменено.")
            return 1
        session.change_passphrase(new)
    finally:
        session.close()
    print("✓ Пароль изменён, стойкость подбора обновлена до текущей. Бумажный код продолжает работать.")
    print("Старые резервные копии содержат СТАРЫЙ пароль: сделай новую копию (backup).")
    return 0


def cmd_new_recovery_code(_args) -> int:
    session = open_session()
    try:
        code = session.new_recovery_code()
        _show_recovery_code(code)
        confirmed = _confirm_recovery_code(session)
    finally:
        session.close()
    print("Прежний бумажный код больше НЕ работает — уничтожь старую бумагу." if confirmed
          else "Код не подтверждён: проверь запись (vault-check-recovery) до уничтожения старой бумаги.")
    print("Старые резервные копии открываются СТАРЫМ кодом: сделай новую копию (backup).")
    return 0


def cmd_vault_check_recovery(_args) -> int:
    ok = Vault.open(home() / "jura.vault").check_recovery_code(getpass.getpass("Введи бумажный код (ввод скрыт): "))
    print("✓ Код подходит." if ok else "✗ Код НЕ подходит.")
    return 0 if ok else 2


# ----------------------------------------------- проверки и восстановление ---
def cmd_verify_log(_args) -> int:
    db_path = home() / "jura.db"
    if not db_path.exists():
        print(f"Журнал не найден: {db_path}")
        return 1
    db = Database(db_path)
    try:
        log = EventLog(db)  # без ключа: хэши считаются по шифротексту
        res = log.verify()
        anchor = log.anchor()
    finally:
        db.close()
    print(f"Событий проверено: {res.checked}")
    print("Целостность: OK" if res.ok else f"НАРУШЕНА на событии №{res.broken_at}: {res.reason}")
    print(f"Якорь (сохрани вне этой машины): {anchor}")
    return 0 if res.ok else 2


def cmd_peek_raw(_args) -> int:
    print("Так выглядят ваши данные для того, у кого украли файлы, но нет ключа.\n"
          "(Ключ и пароль здесь не использовались.)\n")
    lines = peek_raw(home())
    print("\n".join(lines) if lines else "Данных пока нет.")
    return 0


def _credentials() -> dict:
    pw = getpass.getpass("Пароль хранилища из копии (Enter — ввести бумажный код): ")
    return {"passphrase": pw} if pw else {"recovery": getpass.getpass("Бумажный код (ввод скрыт): ")}


def cmd_backup_verify(args) -> int:
    rep = verify_backup(Path(args.file), **_credentials())
    print(f"✓ Копия цела: файлов {len(rep.files)}, событий в журнале {rep.events}, якорь совпал.")
    return 0


def cmd_restore(args) -> int:
    rep = restore_backup(Path(args.file), Path(args.to), **_credentials())
    print(f"✓ Восстановлено в {rep.target}: файлов {len(rep.files)}, событий {rep.events}; журнал проверен по якорю.")
    print("Чтобы J.U.R.A. работал с этой папкой, запомни её (один раз) и запусти shell:")
    print(f'  python -m jura config set home "{rep.target}"')
    print("  python -m jura shell")
    return 0


# --------------------------------------------------------------------- голос ---
def cmd_voice_check(_args) -> int:
    return 0 if diagnose(build_voice_stack()) else 2


def cmd_listen(args) -> int:
    stack = build_voice_stack()
    result = listen_once(stack, wav_path=args.file)
    print(f"Услышал: «{result.text}»")
    print(f"  модель {result.model}; звук {result.audio_seconds:.1f} с, распознавание {result.compute_seconds:.2f} с "
          f"(RTF {result.rtf:.2f}; меньше 1 — быстрее реального времени)")
    if args.remember and result.text:
        session = open_session()
        try:
            session.memory_add(result.text, ZONES[args.zone])
            session.app.log.append("voice.transcribed", "user:owner",
                                   {"model": result.model, "audio_seconds": round(result.audio_seconds, 1)})
        finally:
            session.close()
        print(f"✓ Запомнено в память (зона {args.zone}, зашифровано). Аудио нигде не сохранялось.")
    return 0


def cmd_speak(args) -> int:
    build_voice_stack().tts.speak(" ".join(args.text))
    return 0


def cmd_echo(_args) -> int:
    n = echo_loop(build_voice_stack())
    print(f"Повторено фраз: {n}")
    return 0
