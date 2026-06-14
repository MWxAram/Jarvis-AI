"""
updater.py — умная система обновлений JARVIS
=============================================
Алгоритм:
  1. Скачивает version.json с GitHub
  2. Сравнивает SHA-256 каждого файла локально
  3. Если есть изменения — показывает диалог «Установить?»
  4. После согласия скачивает ТОЛЬКО изменённые файлы
  5. Показывает диалог «Перезапустить?»

Папки python_env/ venv/ и пользовательские JSON — никогда не трогаются.
"""

from __future__ import annotations
import hashlib, json, os, sys, threading, time, shutil
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

# Файлы и папки, которые НИКОГДА не трогаем
_PROTECTED = {
    "jarvis_config.json", "jarvis_commands.json",
    "jarvis_notes.json",  "jarvis_chat_log.json",
    "updater.py",   # нельзя обновить себя пока запущен — заменяется лаунчером
}
_PROTECTED_DIRS = ("python_env", "venv", ".git", "__pycache__")

# Папка проекта (рядом с main_app.py / Run_AI.exe)
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
    print("[UPDATE] Модуль обновлений инициализирован.")


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


def _download_file(rel: str, expected_sha: str) -> bool:
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
        shutil.move(str(tmp), str(dest))
        print(f"[UPDATE] ✓ {rel}")
        return True
    except Exception as e:
        print(f"[UPDATE] write error {rel}: {e}")
        try: tmp.unlink()
        except: pass
        return False


def _save_version(ver: str, files: dict):
    try:
        (_BASE / "version.json").write_text(
            json.dumps({"version": ver, "files": files,
                        "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[UPDATE] version save error: {e}")


# ── Основная логика ───────────────────────────────────────────────────────────
def _do_download_and_restart(changed: list, remote_ver: str, remote_files: dict):
    """Скачивает изменённые файлы, затем голосом предлагает перезапуск."""
    ok, fail = 0, []
    for rel, sha in changed:
        if _download_file(rel, sha): ok += 1
        else:                        fail.append(rel)

    if ok:
        _save_version(remote_ver, remote_files)

    if fail:
        msg = f"Обновлено {ok} из {len(changed)} файлов. Не удалось: {', '.join(fail)}."
    else:
        msg = f"Обновление завершено, {ok} файлов. Версия {remote_ver}."
    _say(msg)

    if ok > 0:
        # Голосом предлагаем перезапуск — ответ ловит main_app через _pending_confirm
        time.sleep(1.5)
        _say("Для применения обновлений требуется перезапуск. Перезапустить сейчас?")
        _set_pending(
            on_yes=_do_restart,
            on_no=lambda: _say("Перезапуск отложен. Перезапустите вручную для применения обновлений."),
            keywords_yes={"да", "да конечно", "перезапусти", "restart", "yes",
                          "перезагрузи", "окей", "ок", "конечно"},
            keywords_no={"нет", "не надо", "позже", "no", "отмена", "cancel"},
        )


def _do_restart():
    """Перезапускает процесс."""
    _say("Перезапускаю JARVIS.")
    time.sleep(1.5)
    try:
        import subprocess
        subprocess.Popen([sys.executable] + sys.argv)
    except Exception as e:
        print(f"[UPDATE] restart error: {e}")
    finally:
        os._exit(0)


# ── Pending voice confirmation ────────────────────────────────────────────────
# When Jarvis asks a yes/no question, this dict holds the callbacks.
# main_app checks consume_pending_confirm(user_text) before any other logic.
_pending: dict | None = None

def _set_pending(on_yes, on_no, keywords_yes: set, keywords_no: set):
    global _pending
    _pending = {
        "on_yes":       on_yes,
        "on_no":        on_no,
        "yes":          keywords_yes,
        "no":           keywords_no,
    }

def consume_pending_confirm(user_text: str) -> bool:
    """
    Called by main_app for every recognised phrase.
    Returns True if the phrase was consumed as a yes/no answer (skip normal processing).
    Returns False if there is no pending confirm or phrase doesn't match.
    """
    global _pending
    if _pending is None:
        return False
    t = user_text.strip().lower()
    if any(k in t for k in _pending["yes"]):
        cb = _pending["on_yes"]
        _pending = None
        try: cb()
        except Exception as e: print(f"[UPDATE] on_yes error: {e}")
        return True
    if any(k in t for k in _pending["no"]):
        cb = _pending["on_no"]
        _pending = None
        try: cb()
        except Exception as e: print(f"[UPDATE] on_no error: {e}")
        return True
    # Neither match — remind user
    _say("Скажите «да» для подтверждения или «нет» для отмены.")
    return True   # still consumed — don't pass to normal logic


def check_and_update(silent: bool = False) -> str:
    """
    Главная функция. Вызывается голосовой командой или при старте.

    silent=True  → не говорит если всё актуально (только при старте).
    silent=False → всегда отвечает голосом.
    """
    local_ver = _get_local_version()
    print(f"[UPDATE] локальная версия: {local_ver}")

    # 1. Получаем манифест
    raw = _fetch(_VERSION_URL)
    if raw is None:
        msg = "Сэр, не удалось подключиться к серверу обновлений."
        if not silent: _say(msg)
        else: print(f"[UPDATE] {msg}")
        return msg

    try:
        import re as _re
        # Убираем trailing commas перед } или ] — JSON их не поддерживает,
        # но люди часто оставляют при ручном редактировании на GitHub.
        text = raw.decode("utf-8")
        text = _re.sub(r",\s*([}\]])", r"\1", text)
        manifest = json.loads(text)
    except Exception as e:
        msg = f"Сэр, ошибка чтения манифеста обновлений: {e}"
        if not silent: _say(msg)
        return msg

    remote_ver   = manifest.get("version", "0.0.0")
    remote_files = manifest.get("files",   {})

    # 2. Ищем изменения по SHA-256
    changed = _find_changed(remote_files)

    if not changed and local_ver == remote_ver:
        msg = f"Сэр, JARVIS актуален. Версия {local_ver}."
        if not silent: _say(msg)
        else: print(f"[UPDATE] {msg}")
        return msg

    # 3. Сообщаем голосом и ждём голосового «да/нет»
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

    # Голосовой вопрос — ответ ловит main_app через consume_pending_confirm()
    _say("Установить обновление?")
    _set_pending(
        on_yes=_on_confirm,
        on_no=_on_cancel,
        keywords_yes={"да", "да конечно", "установи", "скачай", "yes", "install",
                      "обнови", "загрузи", "конечно", "окей", "ок"},
        keywords_no={"нет", "не надо", "позже", "no", "отмена", "cancel", "пропусти"},
    )
    return msg


def check_startup(silent: bool = True):
    """Тихая проверка при запуске в фоновом потоке.
    Ждёт 12 сек — чтобы Qt window точно успел инициализироваться,
    аудио-поток запустился и пользователь услышал приветствие.
    """
    def _run():
        time.sleep(12)
        check_and_update(silent=silent)
    threading.Thread(target=_run, daemon=True, name="JarvisUpdateStartup").start()


# ── Утилита для разработчика: генерация version.json ─────────────────────────
def generate_version_json(version: str, project_dir: Path | None = None):
    """
    Генерирует version.json с SHA-256 хешами.
    Запустить перед каждым релизом:
        python updater.py --generate 1.0.1
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

    out = base / "version.json"
    out.write_text(
        json.dumps({"version": version, "files": files,
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=2),
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