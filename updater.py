"""
updater.py — система обновлений JARVIS
=======================================

Качает version.json с GitHub, сравнивает SHA-256 каждого .py-файла и
докачивает только изменённые файлы поштучно — так же, как раньше в
dev-режиме. Это единственный режим: JARVIS больше не собирается через
PyInstaller (см. README_BUILD.md), поэтому sys.frozen никогда не
выставляется, а поштучное обновление подходит всегда — исходники лежат
рядом с Jarvis.exe как обычные .py-файлы, а не запечены внутрь бандла.

Для разработчика: после правок в .py-файлах запусти:
    python updater.py --generate 1.0.9
Это создаст version.json с SHA-256 всех файлов. Дальше просто загрузи
его вместе с изменёнными .py-файлами на GitHub (main-ветка).

Папки python_env/ venv/ и пользовательские JSON — никогда не трогаются.
"""

from __future__ import annotations
import hashlib, json, os, sys, threading, time, shutil, py_compile, subprocess
from pathlib import Path
from typing   import Callable

# ── Настройки репозитория ─────────────────────────────────────────────────────
GITHUB_USER   = "MWxAram"
GITHUB_REPO   = "Jarvis-AI"
GITHUB_BRANCH = "main"

_VERSION_URL = (f"https://raw.githubusercontent.com/{GITHUB_USER}/"
                f"{GITHUB_REPO}/{GITHUB_BRANCH}/version.json")
_FILE_URL    = (f"https://raw.githubusercontent.com/{GITHUB_USER}/"
                f"{GITHUB_REPO}/{GITHUB_BRANCH}/{{path}}")


# Файлы и папки, которые НИКОГДА не трогаем.
_PROTECTED = {
    "jarvis_config.json", "jarvis_commands.json",
    "jarvis_notes.json",  "jarvis_chat_log.json",
    "jarvis_usage.json",           # счётчики токенов/запросов — меняются на каждый ИИ-запрос
    "jarvis_analytics_queue.json", # локальная очередь аналитики — меняется на каждый track()
    "updater.py",    # нельзя обновить себя пока запущен — заменяется лаунчером
    "version.json",  # манифест — перезаписывается локально через _save_version()
}
_PROTECTED_DIRS = ("python", "python_env", "venv", ".git", "__pycache__")

# Модули проекта, которые импортируются друг другом во время работы
# (jarvis_ui импортирует jarvis_vip; main_app импортирует updater,
# jarvis_features и jarvis_ui). Для них перед принятием скачанного файла
# делаем py_compile-проверку синтаксиса — битый файл от плохого релиза
# не должен молча подменить рабочий модуль и заставить, например,
# jarvis_ui.py откатиться в режим "_vip = None" после перезапуска
# без внятного объяснения пользователю.
_CRITICAL_MODULES = {
    "jarvis_vip.py", "jarvis_features.py", "jarvis_ui.py", "main_app.py",
}

# ── Папка установки ─────────────────────────────────────────────────────
_BASE = Path(sys.argv[0]).parent.resolve()

# ── Callbacks — устанавливаются из main_app через init_updater() ──────────────
_voice_fn  : Callable | None = None
_log_fn    : Callable | None = None
_status_fn : Callable | None = None
_voice_ru  : str             = "ru-RU-DmitryNeural"
# (ui_ask_update / ui_ask_restart removed — Jarvis asks via voice now)


def init_updater(voice_fn, log_fn, status_fn, voice_ru: str,
                 ui_ask_update=None, ui_ask_restart=None):
    """Вызывать один раз из main_app после start_ui().
    ui_ask_update / ui_ask_restart оставлены для совместимости, не используются.
    """
    global _voice_fn, _log_fn, _status_fn, _voice_ru
    _voice_fn = voice_fn
    _log_fn   = log_fn
    _status_fn = status_fn
    _voice_ru  = voice_ru
    print(f"[UPDATE] Модуль обновлений инициализирован. Папка: {_BASE}")


# ── Вспомогательные функции ───────────────────────────────────────────────────
def _say(text: str):
    """Голосовое + лог сообщение."""
    print(f"[UPDATE] {text}")
    if _log_fn:    _log_fn("jarvis", text)
    if _voice_fn:  _voice_fn(text, _voice_ru)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str, timeout: int = 15) -> bytes | None:
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        print(f"[UPDATE] fetch error: {e}")
        return None


def _skip(rel: str) -> bool:
    if rel in _PROTECTED:
        return True
    for d in _PROTECTED_DIRS:
        if rel == d or rel.startswith(d + "/") or rel.startswith(d + os.sep):
            return True
    return False


def _get_local_version() -> str:
    try:
        d = json.loads((_BASE / "version.json").read_text(encoding="utf-8"))
        return d.get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def _find_changed(remote_files: dict) -> list[tuple[str, str]]:
    """Возвращает список (rel_path, remote_sha) только для изменённых файлов."""
    changed = []
    for rel, remote_sha in remote_files.items():
        if _skip(rel):
            continue
        local = _BASE / rel
        if _sha256(local) != remote_sha:
            changed.append((rel, remote_sha))
            print(f"[UPDATE] изменён: {rel}")
    return changed


def _is_valid_python(path: Path) -> bool:
    """Проверяет, что .py-файл синтаксически корректен (py_compile).
    Используется только для критичных модулей перед тем, как принять
    скачанную версию — чтобы битый релиз с GitHub не тихо подменил
    рабочий jarvis_vip.py / jarvis_features.py / jarvis_ui.py / main_app.py.
    """
    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except Exception as e:
        print(f"[UPDATE] синтаксическая ошибка в {path.name}: {e}")
        return False


def _download_file(rel: str, expected_sha: str) -> bool:
    """Скачивает и подменяет ОДИН .py-файл."""
    data = _fetch(_FILE_URL.format(path=rel.replace(os.sep, "/")))
    if data is None:
        return False
    if _sha256_bytes(data) != expected_sha:
        print(f"[UPDATE] SHA mismatch: {rel}")
        return False
    dest = _BASE / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp  = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        if rel in _CRITICAL_MODULES and not _is_valid_python(tmp):
            try: tmp.unlink()
            except Exception: pass
            return False
        shutil.move(str(tmp), str(dest))
        print(f"[UPDATE] ✓ {rel}")
        return True
    except Exception as e:
        print(f"[UPDATE] write error {rel}: {e}")
        try: tmp.unlink()
        except Exception: pass
        return False


def _save_version(ver: str, files: dict):
    """Сохраняет версию локально."""
    payload = {"version": ver, "files": files,
               "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        (_BASE / "version.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[UPDATE] version save error: {e}")


def _get_local_files_record() -> dict:
    """Текущая секция 'files' из локального version.json (если есть)."""
    try:
        d = json.loads((_BASE / "version.json").read_text(encoding="utf-8"))
        return dict(d.get("files", {}))
    except Exception:
        return {}


# ── Основная логика: скачивание изменённых файлов ─────────────────────────
def _do_download_and_restart(changed: list, remote_ver: str, remote_files: dict):
    """Скачивает изменённые файлы, затем голосом предлагает перезапуск."""
    ok, fail, fail_critical = 0, [], []
    for rel, sha in changed:
        if _download_file(rel, sha):
            ok += 1
        else:
            fail.append(rel)
            if rel in _CRITICAL_MODULES:
                fail_critical.append(rel)

    if ok:
        merged = _get_local_files_record()
        for rel, _ in changed:
            if rel not in fail:
                merged[rel] = remote_files.get(rel, "")
        new_ver = remote_ver if not fail else _get_local_version()
        _save_version(new_ver, merged)

    if fail_critical:
        msg = (f"Обновлено {ok} из {len(changed)} файлов. "
               f"Критичные модули не прошли проверку и оставлены прежними: "
               f"{', '.join(fail_critical)}.")
    elif fail:
        msg = f"Обновлено {ok} из {len(changed)} файлов. Не удалось: {', '.join(fail)}."
    else:
        msg = f"Обновление завершено, {ok} файлов. Версия {remote_ver}."
    _say(msg)

    if ok > 0:
        time.sleep(1.5)
        _say("Для применения обновлений требуется перезапуск. Перезапустить сейчас?")
        _set_pending(
            on_yes=_do_restart,
            on_no=lambda: _say("Перезапуск отложен. Перезапустите вручную для применения обновлений."),
            on_ignore=lambda: print("[UPDATE] Вопрос о перезапуске проигнорирован — пользователь сменил тему."),
            keywords_yes={"да", "да конечно", "перезапусти", "restart", "yes",
                          "перезагрузи", "окей", "ок", "конечно"},
            keywords_no={"нет", "не надо", "позже", "no", "отмена", "cancel"},
        )


def _do_restart():
    """Перезапускает процесс без подмены файлов (dev-режим: файлы уже
    подменены поштучно функциями выше — просто нужен новый запуск
    интерпретатора, чтобы Python перечитал изменённые модули)."""
    _say("Перезапускаю JARVIS.")
    time.sleep(1.5)
    try:
        subprocess.Popen([sys.executable] + sys.argv)
    except Exception as e:
        print(f"[UPDATE] restart error: {e}")
    finally:
        os._exit(0)


# ── Pending voice confirmation (общий менеджер) ───────────────────────────────
_pending_stack: list[dict] = []


def set_pending_confirm(on_yes, on_no, keywords_yes: set, keywords_no: set,
                        on_ignore=None):
    """
    Регистрирует одно голосовое да/нет-ожидание.
      - on_yes   — вызывается, если ответ явно совпал с keywords_yes.
      - on_no    — вызывается, если ответ явно совпал с keywords_no
                   (пользователь осознанно отказался).
      - on_ignore — вызывается (не обязателен), если ответ не совпал НИ С ЧЕМ
                   из вышеперечисленного — то есть пользователь просто заговорил
                   о чём-то другом, не ответив на вопрос напрямую.
    Если в моменте уже есть другое незакрытое ожидание, они складываются в
    стек: проверяется от самого нового к самому старому.
    """
    _pending_stack.append({
        "on_yes":    on_yes,
        "on_no":     on_no,
        "on_ignore": on_ignore,
        "yes":       keywords_yes,
        "no":        keywords_no,
    })


def _set_pending(on_yes, on_no, keywords_yes: set, keywords_no: set, on_ignore=None):
    """Внутренний алиас — используется самим updater.py."""
    set_pending_confirm(on_yes, on_no, keywords_yes, keywords_no, on_ignore=on_ignore)


def consume_pending_confirm(user_text: str) -> bool:
    """
    Called by main_app for every recognised phrase, до любой другой логики.

    Returns True  — фраза была явным ответом "да"/"нет" на вопрос.
    Returns False — вопроса не было, либо пользователь на него не ответил
                    впрямую — ожидание тихо снимается, фраза уходит в
                    обычную обработку.
    """
    if not _pending_stack:
        return False
    t = user_text.strip().lower()
    for i in range(len(_pending_stack) - 1, -1, -1):
        item = _pending_stack[i]
        if any(k in t for k in item["yes"]):
            _pending_stack.pop(i)
            try: item["on_yes"]()
            except Exception as e: print(f"[PENDING] on_yes error: {e}")
            return True
        if any(k in t for k in item["no"]):
            _pending_stack.pop(i)
            try: item["on_no"]()
            except Exception as e: print(f"[PENDING] on_no error: {e}")
            return True
    item = _pending_stack.pop()
    if item.get("on_ignore"):
        try: item["on_ignore"]()
        except Exception as e: print(f"[PENDING] on_ignore error: {e}")
    return False


def check_and_update(silent: bool = False) -> str:
    """
    Главная функция. Вызывается голосовой командой или при старте.

    silent=True  → не говорит если всё актуально (только при старте).
    silent=False → всегда отвечает голосом.
    """
    local_ver = _get_local_version()
    print(f"[UPDATE] локальная версия: {local_ver}")

    raw = _fetch(_VERSION_URL)
    if raw is None:
        msg = "Сэр, не удалось подключиться к серверу обновлений."
        if not silent: _say(msg)
        else: print(f"[UPDATE] {msg}")
        return msg

    try:
        import re as _re
        text = raw.decode("utf-8")
        text = _re.sub(r",\s*([}\]])", r"\1", text)
        manifest = json.loads(text)
    except Exception as e:
        msg = f"Сэр, ошибка чтения манифеста обновлений: {e}"
        if not silent: _say(msg)
        return msg

    remote_ver = manifest.get("version", "0.0.0")
    remote_files = manifest.get("files", {})
    changed = _find_changed(remote_files)

    if not changed and local_ver == remote_ver:
        msg = f"Сэр, JARVIS актуален. Версия {local_ver}."
        if not silent: _say(msg)
        else: print(f"[UPDATE] {msg}")
        return msg

    n = len(changed)
    if local_ver != remote_ver:
        msg = f"Сэр, доступна новая версия JARVIS: {remote_ver}."
    else:
        msg = f"Сэр, обнаружены изменения в {n} файлах версии {remote_ver}."
    _say(msg)

    time.sleep(0.8)

    def _on_confirm():
        _say(f"Начинаю загрузку {n} файлов.")
        threading.Thread(
            target=_do_download_and_restart,
            args=(changed, remote_ver, remote_files),
            daemon=True, name="JarvisDownload"
        ).start()

    def _on_cancel():
        _say("Обновление отменено.")

    def _on_ignore():
        print("[UPDATE] Вопрос об установке проигнорирован — пользователь сменил тему.")

    _say("Установить обновление?")
    _set_pending(
        on_yes=_on_confirm,
        on_no=_on_cancel,
        on_ignore=_on_ignore,
        keywords_yes={"да", "да конечно", "установи", "скачай", "yes", "install",
                      "обнови", "загрузи", "конечно", "окей", "ок"},
        keywords_no={"нет", "не надо", "позже", "no", "отмена", "cancel", "пропусти"},
    )
    return msg


def check_startup(silent: bool = True):
    """Проверка при запуске в фоновом потоке.
    Ждёт 6 сек — чтобы Qt window инициализировался и пользователь
    услышал приветствие, прежде чем Джарвис сообщит об обновлении.
    silent=True  → молчит если всё актуально.
    silent=False → говорит всегда.
    """
    def _run():
        time.sleep(6)
        check_and_update(silent=silent)
    threading.Thread(target=_run, daemon=True, name="JarvisUpdateStartup").start()


# ═══════════════════════════════════════════════════════════════════════════
#  ИСТОРИЯ ОБНОВЛЕНИЙ (журнал версий из веб-бэкенда)
# ═══════════════════════════════════════════════════════════════════════════
# Отвечает на вопросы вида "что было в обновлении 0.9?" / "что нового в
# последнем обновлении?", беря данные из публичного эндпоинта бэкенда
# GET /api/updates (routers/updates.py, без авторизации). Это ОТДЕЛЬНАЯ вещь
# от check_and_update() выше: там мы качаем и ставим САМИ ФАЙЛЫ программы
# (поштучно в dev-режиме или целой сборкой в frozen-режиме), здесь — просто
# читаем ЖУРНАЛ ИЗМЕНЕНИЙ (текстовое описание версий) с сайта, ничего не
# устанавливаем.
UPDATES_HISTORY_API_URL = "http://localhost:8000/api/updates"

_TAG_LABELS_RU = {"new": "Новое", "improved": "Улучшено", "fixed": "Исправлено"}


def _parse_version_tuple(v: str):
    """"1.0.1" → (1, 0, 1). Возвращает None, если в строке нет ни одной цифры."""
    import re as _re
    nums = _re.findall(r'\d+', v or "")
    return tuple(int(n) for n in nums) if nums else None


def fetch_update_history(timeout: float = 5):
    """
    GET /api/updates — полный журнал обновлений с бэкенда (новые сверху —
    так их уже отдаёт сам роутер). Возвращает None при сетевой ошибке
    (бэкенд выключен / нет интернета) — вызывающий код должен явно
    сообщить об этом пользователю, а не тихо промолчать или выдумать ответ.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(UPDATES_HISTORY_API_URL, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[UPDATE-HISTORY] Сервер недоступен: {e}")
        return None


def format_update_entry_speech(entry: dict) -> str:
    """Превращает одну запись обновления (из ответа API) в связную речь."""
    version = entry.get("version", "?")
    title = (entry.get("title") or "").strip()
    parts = [f"Версия {version}" + (f", «{title}»" if title else "") + "."]

    by_tag = {"new": [], "improved": [], "fixed": []}
    for item in (entry.get("changelog") or []):
        tag = item.get("tag", "new")
        text = (item.get("text") or "").strip()
        if text:
            by_tag.setdefault(tag, []).append(text)

    for tag in ("new", "improved", "fixed"):
        if by_tag.get(tag):
            label = _TAG_LABELS_RU.get(tag, tag)
            parts.append(f"{label}: " + "; ".join(by_tag[tag]) + ".")
    return " ".join(parts)


def answer_update_history_query(query_version: str | None, latest: bool = False) -> str:
    """
    Основная точка входа для голосового вопроса про историю обновлений.
      - latest=True (или query_version не задан) → рассказывает про самое
        последнее обновление в журнале.
      - query_version задан → ищет точное совпадение по версии; если такой
        версии в журнале нет — сравнивает номера версий численно и
        предлагает две ближайшие существующие (одну до, одну после).
    """
    updates = fetch_update_history()
    if updates is None:
        return ("Сэр, сейчас не могу получить эту информацию — "
                "сервер JARVIS недоступен.")
    if not updates:
        return "Сэр, в базе пока нет ни одной записи об обновлениях."

    if latest or not query_version:
        return "Последнее обновление. " + format_update_entry_speech(updates[0])

    target = _parse_version_tuple(query_version)
    if target is None:
        return f"Сэр, не удалось распознать номер версии «{query_version}»."

    exact = next((u for u in updates if _parse_version_tuple(u.get("version", "")) == target), None)
    if exact:
        return format_update_entry_speech(exact)

    below = above = None
    below_v = above_v = None
    for u in updates:
        v = _parse_version_tuple(u.get("version", ""))
        if v is None:
            continue
        if v < target and (below_v is None or v > below_v):
            below, below_v = u, v
        if v > target and (above_v is None or v < above_v):
            above, above_v = u, v

    msg = f"Сэр, информации об обновлении {query_version} нет."
    near = [u for u in (below, above) if u]
    if near:
        msg += " Ближайшие версии: " + " ".join(format_update_entry_speech(u) for u in near)
    return msg


# ── Утилита для разработчика: генерация version.json ─────────────────────────
def generate_version_json(version: str, project_dir: Path | None = None):
    """
    Генерирует version.json с SHA-256 хешами всех .py-файлов проекта.

    Запустить перед каждым релизом:
        python updater.py --generate 1.0.9
    """
    base = project_dir or Path(".").resolve()
    files = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file(): continue
        rel = path.relative_to(base).as_posix()
        if any(rel.startswith(d+"/") for d in _PROTECTED_DIRS): continue
        if any(rel == p for p in _PROTECTED): continue
        files[rel] = _sha256(path)
        print(f"  + {rel}")

    payload = {"version": version, "files": files,
               "updated": time.strftime("%Y-%m-%d %H:%M:%S")}

    out = base / "version.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n✓ version.json → {out}  ({len(files)} файлов, версия {version})")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="JARVIS Updater")
    p.add_argument("--generate", metavar="VER", help="Generate version.json, e.g. 1.0.1")
    p.add_argument("--check",    action="store_true", help="Check for updates now")
    a = p.parse_args()
    if a.generate:
        generate_version_json(a.generate, Path("."))
    elif a.check:
        print(check_and_update(silent=False))
    else:
        p.print_help()
