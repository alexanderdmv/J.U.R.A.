"""Команды, которые работают внутри разблокированной сессии: их выполняет и интерактивная оболочка (`shell`),
и одиночные вызовы (`python -m jura secret-get …`). Каждая команда — отдельная короткая функция."""

from __future__ import annotations

import getpass
import json
import shlex
from pathlib import Path

from jura.contracts.errors import JuraError
from jura.contracts.types import TaskState, Zone
from jura.model_registry import KEY_NAMES, PROVIDERS
from jura.session import Session, backup_dir_from, default_home, leak_scan

ZONES = {"O": Zone.O, "P": Zone.P, "S1": Zone.S1}

SHELL_HELP = """Команды:
  status                              сводка: события, память, секреты, копии
  secret set ИМЯ                      сохранить секрет (значение вводится скрыто)
  secret get ИМЯ                      показать секрет
  secret list | secret delete ИМЯ
  memory add [--zone O|P|S1] ТЕКСТ    запомнить факт (по умолчанию зона P)
  memory search СЛОВА                 найти в памяти
  backup [--to ПАПКА] [--keep N]      зашифрованная копия (по умолчанию JURA_BACKUP_DIR)
  backup-list [--to ПАПКА]
  model list|add|remove|discover|test  модели (облачные и локальные), подробности: model
  ask [--zone O|P|S1] [--model ИМЯ] ТЕКСТ   спросить модель; P и S1 — только локальные, облако — только для открытых (O)
  tools                               что умеет J.U.R.A. и что включено
  policy list|enable ВОЗМОЖНОСТЬ|disable ВОЗМОЖНОСТЬ   разрешить/запретить (спросит пароль ещё раз)
  run ИНСТРУМЕНТ ключ=значение …      выполнить инструмент (рискованное — после подтверждения;
                                      использующее пароли — после повторного ввода пароля хранилища)
  site add ИМЯ URL ПОЛЕ_ЛОГИНА ПОЛЕ_ПАРОЛЯ КНОПКА [--ok-selector СЕЛ] [--ok-url ТЕКСТ]
           [--allow-origin URL[,URL]] [--hold СЕК]
  site list | site show ИМЯ | site delete ИМЯ
  leak-check ТЕКСТ [--also ПАПКА]     искать этот текст в открытом виде во всех файлах
  verify-log                          проверить целостность журнала
  exit                                выйти (хранилище блокируется)"""


def _pop_option(tokens: list[str], name: str) -> tuple[str | None, list[str]]:
    if name in tokens:
        i = tokens.index(name)
        if i + 1 >= len(tokens):
            raise JuraError(f"после {name} нужно значение")
        return tokens[i + 1], tokens[:i] + tokens[i + 2:]
    return None, tokens


def _fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} КБ" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} МБ"


def _sub(rest: list[str]) -> tuple[str, list[str]]:
    return (rest[0] if rest else ""), rest[1:]


# ------------------------------------------------------------------ команды ---
def _help(session: Session, rest: list[str], out) -> None:
    out(SHELL_HELP)


def _status(session: Session, rest: list[str], out) -> None:
    st = session.status()
    out(f"Папка данных:  {st['home']}")
    out(f"Журнал:        {st['events']} событий, целостность {'OK' if st['log_ok'] else 'НАРУШЕНА'}")
    out(f"Якорь журнала: {st['anchor'][:30]}…  (сохраняй вне этого компьютера)")
    out(f"Память:        {st['memory'] or 'пусто'}")
    out(f"Секреты:       {st['secrets']}")
    out(f"Модели:        {st['models'][0]} настроено, из них локальных {st['models'][1]}")
    out(f"Разрешено:     {', '.join(session.policy_enabled()) or 'ничего (по умолчанию всё запрещено)'}")
    out(f"Размер баз:    {_fmt_size(st['db_bytes'])}")
    try:
        files = session.backups(backup_dir_from(None, session.home))
        out(f"Копии:         {len(files)}" + (f", последняя {files[-1].name}" if files else ""))
    except JuraError:
        out("Копии:         папка не настроена (JURA_BACKUP_DIR)")


def _secret(session: Session, rest: list[str], out) -> None:
    sub, args = _sub(rest)
    if sub == "set" and len(args) == 1:
        session.secret_set(args[0], getpass.getpass(f"Значение для '{args[0]}' (ввод скрыт): "))
        out("✓ Сохранено в зашифрованном хранилище.")
    elif sub == "get" and len(args) == 1:
        value = session.secret_get(args[0])
        out("⚠ Значение показано на экране — не снимай скриншот и закрой окно после использования.")
        out(f"{args[0]}: {value}")
    elif sub == "list" and not args:
        out("\n".join(session.secret_list()) or "(секретов нет)")
    elif sub == "delete" and len(args) == 1:
        session.secret_delete(args[0])
        out("✓ Удалено.")
    else:
        raise JuraError("использование: secret set|get|delete ИМЯ  или  secret list")


def _memory(session: Session, rest: list[str], out) -> None:
    sub, args = _sub(rest)
    if sub == "add":
        zone_name, args = _pop_option(args, "--zone")
        zone = ZONES.get((zone_name or "P").upper())
        if zone is None:
            raise JuraError("зона S2 в память не пишется: пароли и ключи — только через «secret set»")
        if not args:
            raise JuraError("использование: memory add [--zone O|P|S1] ТЕКСТ")
        session.memory_add(" ".join(args), zone)
        out(f"✓ Запомнено (зона {zone.name}, хранится зашифрованным).")
    elif sub == "search" and args:
        items = session.memory_search(" ".join(args))
        for it in items:
            out(f"[{it.zone.name}] {it.content}")
        if not items:
            out("(ничего не найдено)")
    else:
        raise JuraError("использование: memory add [--zone O|P|S1] ТЕКСТ  или  memory search СЛОВА")


def _int(value: str | None, label: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise JuraError(f"{label} должно быть целым числом") from None


def _model(session: Session, rest: list[str], out) -> None:
    sub, args = _sub(rest)
    if sub == "list" and not args:
        rows = session.model_list()
        for r in rows:
            where = "локально (данные до S1)" if r["local"] else "облако (только данные O)"
            key = "" if r["local"] else ("  ключ ✓" if r["key_ok"] else f"  ключ ✗ → secret set {KEY_NAMES[r['provider']]}")
            out(f"{r['name']}  [{r['provider']}: {r['model']}]  {where}{key}")
            if r["error"]:
                out(f"    ⚠ не подключена: {r['error']}")
        if not rows:
            out("(моделей нет) Добавить: model add ИМЯ ПРОВАЙДЕР МОДЕЛЬ   Провайдеры: " + ", ".join(PROVIDERS))
    elif sub == "add":
        base, args = _pop_option(args, "--base-url")
        context, args = _pop_option(args, "--context")
        max_out, args = _pop_option(args, "--max-output")
        quality, args = _pop_option(args, "--quality")
        if len(args) != 3:
            raise JuraError("использование: model add ИМЯ ПРОВАЙДЕР МОДЕЛЬ [--base-url URL] [--context N] "
                            f"[--max-output N] [--quality N]   провайдеры: {', '.join(PROVIDERS)}")
        name, provider, model = args
        session.model_add(name, provider, model, base_url=base, context=_int(context, "--context"),
                          max_output=_int(max_out, "--max-output"), quality=_int(quality, "--quality"))
        out(f"✓ Модель '{name}' добавлена.")
        if provider in KEY_NAMES:
            out(f"  Ключ API: secret set {KEY_NAMES[provider]}")
        out(f"  Проверка: model test {name}")
    elif sub == "remove" and len(args) == 1:
        session.model_remove(args[0])
        out("✓ Удалено.")
    elif sub == "discover" and args:
        base, args = _pop_option(args, "--base-url")
        if len(args) != 1:
            raise JuraError("использование: model discover ПРОВАЙДЕР [--base-url URL]")
        names = session.registry.discover(args[0], base)
        out("\n".join(names[:60]) if names else "(список пуст)")
        if len(names) > 60:
            out(f"… и ещё {len(names) - 60}")
    elif sub == "test" and len(args) == 1:
        res = session.model_test(args[0])
        out(f"✓ {res.model} ответила: «{res.text.strip()[:200]}»")
        out(f"  {'локально' if res.local else 'облако'}, {res.seconds:.1f} с, токены {res.tokens_in}→{res.tokens_out}")
    else:
        raise JuraError("использование: model list | add | remove ИМЯ | discover ПРОВАЙДЕР | test ИМЯ")


def _ask(session: Session, rest: list[str], out) -> None:
    zone_name, rest = _pop_option(rest, "--zone")
    only, rest = _pop_option(rest, "--model")
    if not rest:
        raise JuraError("использование: ask [--zone O|P|S1] [--model ИМЯ] ТЕКСТ")
    zone = ZONES.get((zone_name or "P").upper())
    if zone is None:
        raise JuraError("зона S2 (пароли и ключи) моделям не передаётся никогда")
    res = session.ask(" ".join(rest), zone, only=only)
    out(res.text.strip())
    out(f"— {res.model} ({'локально' if res.local else 'облако'}), зона {res.zone.name}, "
        f"токены {res.tokens_in}→{res.tokens_out}, {res.seconds:.1f} с")


def _backup(session: Session, rest: list[str], out) -> None:
    to, rest = _pop_option(rest, "--to")
    keep, rest = _pop_option(rest, "--keep")
    dest = backup_dir_from(to, session.home)
    info, removed = session.backup(dest, int(keep) if keep else None)
    out(f"✓ Копия создана: {info.path}")
    out(f"  размер {_fmt_size(info.size)}, событий в журнале: {info.events}")
    out(f"  якорь: {info.anchor[:30]}…")
    if removed:
        out(f"  старых копий удалено: {len(removed)}")


def _backup_list(session: Session, rest: list[str], out) -> None:
    to, _ = _pop_option(rest, "--to")
    files = session.backups(backup_dir_from(to, session.home))
    for f in files:
        out(f"{f.name}  {_fmt_size(f.stat().st_size)}")
    if not files:
        out("(копий нет)")


def _leak_check(session: Session, rest: list[str], out) -> None:
    also, rest = _pop_option(rest, "--also")
    if not rest:
        raise JuraError("использование: leak-check ТЕКСТ [--also ПАПКА]")
    roots = [session.home]
    try:
        roots.append(backup_dir_from(None, session.home))
    except JuraError:
        pass
    if also:
        roots.append(Path(also))
    found, files, total = leak_scan(" ".join(rest), [r for r in roots if r.exists()])
    if found:
        out(f"✗ НАЙДЕНО в открытом виде в {len(found)} файле(ах):")
        for f in found:
            out(f"    {f}")
    else:
        out(f"✓ Не найдено: проверено файлов — {files}, {_fmt_size(total)}. Без ключа этот текст нигде не читается.")


def _tools(session: Session, rest: list[str], out) -> None:
    for spec, on in session.tools():
        state = "включён" if on else f"выключен → policy enable {spec.capability}"
        out(f"{spec.name}  [риск {spec.risk.name}, {spec.capability}]  {state}")
        out(f"    {spec.description}")


def _policy(session: Session, rest: list[str], out) -> None:
    sub, args = _sub(rest)
    if sub == "list" and not args:
        out("Разрешено: " + (", ".join(session.policy_enabled()) or "ничего — всё запрещено по умолчанию"))
    elif sub in ("enable", "disable") and len(args) == 1:
        pw = getpass.getpass("Для изменения политики введи пароль хранилища ещё раз: ")
        (session.policy_enable if sub == "enable" else session.policy_disable)(args[0], pw)
        out(f"✓ {'Разрешено' if sub == 'enable' else 'Запрещено'}: {args[0]} (записано в журнал)")
    else:
        raise JuraError("использование: policy list | policy enable ВОЗМОЖНОСТЬ | policy disable ВОЗМОЖНОСТЬ")


def _site(session: Session, rest: list[str], out) -> None:
    sub, args = _sub(rest)
    if sub == "add":
        ok_sel, args = _pop_option(args, "--ok-selector")
        ok_url, args = _pop_option(args, "--ok-url")
        origins, args = _pop_option(args, "--allow-origin")
        hold, args = _pop_option(args, "--hold")
        if len(args) != 5:
            raise JuraError("использование: site add ИМЯ URL ПОЛЕ_ЛОГИНА ПОЛЕ_ПАРОЛЯ КНОПКА "
                            "[--ok-selector СЕЛ] [--ok-url ТЕКСТ] [--allow-origin URL[,URL]] [--hold СЕК]")
        name, url, user_sel, pass_sel, submit_sel = args
        profile = {"url": url, "user_selector": user_sel, "password_selector": pass_sel, "submit_selector": submit_sel}
        if ok_sel:
            profile["success_selector"] = ok_sel
        if ok_url:
            profile["success_url_contains"] = ok_url
        if origins:
            profile["allowed_origins"] = [o.strip() for o in origins.split(",") if o.strip()]
        if hold:
            profile["hold_seconds"] = int(hold)
        session.site_set(name, profile)
        out(f"✓ Профиль сайта '{name}' сохранён (зашифрован).")
        out("  Пароль будет вводиться ТОЛЬКО на адресе профиля (и перечисленных в --allow-origin): "
            "при переадресации на другой адрес вход отменяется.")
        out(f"  Теперь сохрани учётные данные: secret set {name}.username  и  secret set {name}.password")
        out(f"  и разреши вход: policy enable web.login   Запуск: run web.login site={name}")
    elif sub == "list" and not args:
        out("\n".join(session.sites()) or "(профилей нет)")
    elif sub == "show" and len(args) == 1:
        out(json.dumps(session.site_get(args[0]), ensure_ascii=False, indent=2))
    elif sub == "delete" and len(args) == 1:
        session.site_delete(args[0])
        out("✓ Удалено.")
    else:
        raise JuraError("использование: site add|list|show|delete …  (help — подробности)")


def _run(session: Session, rest: list[str], out) -> None:
    if not rest:
        raise JuraError("использование: run ИНСТРУМЕНТ ключ=значение …   (список: tools)")
    name, args = rest[0], {}
    for token in rest[1:]:
        if "=" not in token:
            raise JuraError(f"аргумент '{token}' должен быть вида ключ=значение")
        key, value = token.split("=", 1)
        args[key] = value
    res = session.run_tool(
        name, args,
        confirm=lambda text: input(text + "\nРазрешить? (да/нет): ").strip().lower() in ("да", "д", "y", "yes"),
        reauth=lambda text: getpass.getpass(text + "\nВведи пароль хранилища для подтверждения (Enter — отмена): "))
    if res.state is TaskState.DONE:
        out(f"✓ {res.output}")
        if res.result is not None:  # метки результата: именно по ним решается, куда эти данные можно отправлять дальше
            out(f"  метки результата: зона {res.result.zone.name}, доверие {res.result.trust.value}, источник {res.result.source}")
    elif res.state is TaskState.CANCELLED:
        out("Отменено — ничего не выполнено.")
    else:
        out(f"✗ {res.reason}")
        try:
            cap = session.app.registry.spec(name).capability
            if "не включена" in res.reason:
                out(f"  Разрешить: policy enable {cap}")
        except JuraError:
            pass


def _verify_log(session: Session, rest: list[str], out) -> None:
    res = session.app.log.verify()
    out(f"Событий проверено: {res.checked}. " + ("Целостность: OK" if res.ok else f"НАРУШЕНА: {res.reason}"))


COMMANDS = {
    "help": _help, "status": _status, "secret": _secret, "memory": _memory, "backup": _backup,
    "backup-list": _backup_list, "leak-check": _leak_check, "tools": _tools, "policy": _policy,
    "site": _site, "run": _run, "verify-log": _verify_log, "model": _model, "ask": _ask,
}


def execute(session: Session, tokens: list[str], out=print) -> None:
    """Выполняет одну команду. Ошибки пользователя — JuraError (печатаются без падения)."""
    if not tokens:
        return
    handler = COMMANDS.get(tokens[0])
    if handler is None:
        raise JuraError(f"неизвестная команда '{tokens[0]}'. Список: help")
    handler(session, list(tokens[1:]), out)


def open_session() -> Session:
    home = default_home()
    if not (home / "jura.vault").exists():
        raise JuraError(f"хранилище не найдено в {home}. Сначала выполни: python -m jura init")
    return Session.open(home, passphrase=getpass.getpass("Пароль хранилища: "))


def run_session_command(tokens: list[str]) -> int:
    session = open_session()
    try:
        execute(session, tokens)
    finally:
        session.close()
    return 0


def cmd_shell(_args, input_fn=None) -> int:
    session = open_session()
    print("J.U.R.A. разблокирован. «help» — команды, «exit» — выход.")
    try:
        while True:
            try:
                line = (input_fn or input)("jura> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line in ("exit", "quit"):
                break
            try:
                execute(session, shlex.split(line))
            except JuraError as exc:
                print(f"Ошибка: {exc}")
            except ValueError as exc:  # неверные кавычки и т.п.
                print(f"Не понял команду: {exc}")
    finally:
        session.close()
    return 0
