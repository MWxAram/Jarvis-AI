"""
updater.py — умная система обновлений JARVIS
=============================================

ДВА РЕЖИМА РАБОТЫ (определяются автоматически через sys.frozen):

  1) РЕЖИМ РАЗРАБОТКИ (запуск как `python main_app.py`, sys.frozen нет):
     Старый механизм без изменений — качает version.json с GitHub,
     сравнивает SHA-256 каждого .py-файла и докачивает только изменённые
     файлы поштучно. Удобно на машине разработчика: правки сразу видны,
     не нужно каждый раз пересобирать .exe.

  2) РЕЖИМ FROZEN (собранный JARVIS.exe через PyInstaller):
     Поштучная замена .py-файлов здесь БЕССМЫСЛЕННА — все модули запечены
     внутрь бандла (dist\\JARVIS\\_internal\\...), отдельных .py-файлов
     рядом с .exe нет, и заменить их — значит ничего не изменить в
     реально работающем процессе. Поэтому вместо этого:
       - version.json дополнительно содержит "build_sha256" — хеш ЦЕЛОГО
         zip-архива готовой сборки (dist\\JARVIS, упакованный в .zip и
         выложенный в GitHub Releases).
       - Если build_sha256 в манифесте отличается от того, что записано
         локально — скачиваем этот zip целиком, проверяем его хеш,
         распаковываем во временную папку и запускаем маленький .bat,
         который: ждёт закрытия JARVIS.exe → копирует новые файлы поверх
         старых (robocopy, БЕЗ /MIR — то есть ничего лишнего не удаляет,
         так что jarvis_config.json/logs.txt и т.п. не трогаются, они
         просто не входят в архив сборки) → перезапускает JARVIS.exe →
         подчищает за собой временную папку и себя самого.

  Для разработчика: после сборки .exe (JARVIS.spec) и упаковки
  dist\\JARVIS в zip запусти:
      python updater.py --generate 1.0.5 --build-zip JARVIS-1.0.5-win64.zip
  Это создаст version.json с обоими наборами данных ("files" — для
  dev-режима, "build_sha256"/"build_asset" — для frozen-режима) одной
  командой. Дальше просто загрузи version.json на GitHub (main-ветка) и
  сам zip — в GitHub Releases под тегом vX.Y.Z (или пропиши свой URL в
  "build_url" внутри version.json, если хранишь архив в другом месте).

Папки python_env/ venv/ и пользовательские JSON — никогда не трогаются.
"""

from __future__ import annotations
import hashlib, json, os, sys, threading, time, shutil, py_compile, zipfile, tempfile, subprocess
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

# Имя exe внутри собранной папки (JARVIS.spec: name="JARVIS") — используется,
# чтобы найти процесс/файл при перезапуске и проверить целостность архива.
_EXE_NAME = "JARVIS.exe"

# Шаблон URL релизного архива на GitHub Releases, если в version.json не
# указан явный "build_url". Тег релиза ожидается в виде vX.Y.Z.
def _default_build_url(version: str, asset_name: str | None) -> str:
    asset = asset_name or f"JARVIS-{version}-win64.zip"
    return f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/download/v{version}/{asset}"


# Файлы и папки, которые НИКОГДА не трогаем (актуально для dev-режима —
# в frozen-режиме защита обеспечивается тем, что этих файлов просто нет
# в скачиваемом zip-архиве сборки, см. докстринг модуля выше).
_PROTECTED = {
    "jarvis_config.json", "jarvis_commands.json",
    "jarvis_notes.json",  "jarvis_chat_log.json",
    "jarvis_usage.json",           # счётчики токенов/запросов — меняются на каждый ИИ-запрос
    "jarvis_analytics_queue.json", # локальная очередь аналитики — меняется на каждый track()
    "updater.py",    # нельзя обновить себя пока запущен — заменяется лаунчером
    "version.json",  # манифест — перезаписывается локально через _save_version()
}
_PROTECTED_DIRS = ("python_env", "venv", ".git", "__pycache__")

# Модули проекта, которые импортируются друг другом во время работы
# (jarvis_ui импортирует jarvis_vip; main_app импортирует updater,
# jarvis_features и jarvis_ui). Для них перед принятием скачанного файла
# делаем py_compile-проверку синтаксиса — битый файл от плохого релиза
# не должен молча подменить рабочий модуль и заставить, например,
# jarvis_ui.py откатиться в режим "_vip = None" после перезапуска
# без внятного объяснения пользователю. (Актуально только для dev-режима.)
_CRITICAL_MODULES = {
    "jarvis_vip.py", "jarvis_features.py", "jarvis_ui.py", "main_app.py",
}

# ── Определяем режим и папку установки ────────────────────────────────────
# sys.frozen выставляется PyInstaller-ом в True для собранного .exe.
_IS_FROZEN = getattr(sys, "frozen", False)

if _IS_FROZEN:
    # sys.executable — это сам JARVIS.exe (надёжнее argv[0], который в
    # редких случаях может отличаться, например при запуске через ярлык
    # с параметрами).
    _BASE = Path(sys.executable).parent.resolve()
else:
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
    mode = "frozen (.exe)" if _IS_FROZEN else "dev (python)"
    print(f"[UPDATE] Модуль обновлений инициализирован. Режим: {mode}. Папка: {_BASE}")


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


def _get_local_build_sha() -> str:
    """Хеш ЦЕЛОГО zip-архива сборки, который сейчас установлен (frozen-режим).
    Пусто, если ещё ни разу не обновлялись через этот механизм (например,
    самая первая установка, поставленная напрямую через инсталлятор) —
    в этом случае просто доверяем версии из локального version.json и не
    докачиваем архив, пока version действительно не разойдётся."""
    try:
        d = json.loads((_BASE / "version.json").read_text(encoding="utf-8"))
        return d.get("build_sha256", "")
    except Exception:
        return ""


def _find_changed(remote_files: dict) -> list[tuple[str, str]]:
    """Возвращает список (rel_path, remote_sha) только для изменённых файлов.
    Используется только в dev-режиме (см. докстринг модуля)."""
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
    (Только dev-режим.)
    """
    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except Exception as e:
        print(f"[UPDATE] синтаксическая ошибка в {path.name}: {e}")
        return False


def _download_file(rel: str, expected_sha: str) -> bool:
    """Скачивает и подменяет ОДИН .py-файл. Только dev-режим."""
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


def _save_version(ver: str, files: dict, build_sha256: str | None = None):
    """Сохраняет версию локально. build_sha256 сохраняется, только если
    передан явно (frozen-режим) — иначе поле остаётся как было (dev-режим
    его не трогает, чтобы не затирать значение, записанное frozen-режимом
    ранее на этой же машине)."""
    payload = {"version": ver, "files": files,
               "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    if build_sha256 is not None:
        payload["build_sha256"] = build_sha256
    else:
        # сохраняем предыдущее значение, если было
        prev = _get_local_build_sha()
        if prev:
            payload["build_sha256"] = prev
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


# ═══════════════════════════════════════════════════════════════════════════
#  FROZEN-РЕЖИМ: скачивание/установка целой сборки
# ═══════════════════════════════════════════════════════════════════════════

def _extract_and_stage(zip_bytes: bytes) -> Path | None:
    """Распаковывает zip во временную папку и проверяет, что внутри
    действительно лежит JARVIS.exe (защита от битого/левого архива —
    не хотим готовить подмену на основе мусора)."""
    staging = Path(tempfile.mkdtemp(prefix="jarvis_update_"))
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
            zf.extractall(staging)
    except Exception as e:
        print(f"[UPDATE] Не удалось распаковать архив обновления: {e}")
        shutil.rmtree(staging, ignore_errors=True)
        return None

    # Архив может содержать сам JARVIS.exe либо в корне, либо в одной
    # вложенной папке (частый случай при zip "dist\JARVIS" -> JARVIS/...).
    # Находим папку, где реально лежит EXE, и используем её как источник.
    if (staging / _EXE_NAME).exists():
        return staging
    for sub in staging.iterdir():
        if sub.is_dir() and (sub / _EXE_NAME).exists():
            return sub

    print("[UPDATE] В скачанном архиве не найден JARVIS.exe — отменяю установку.")
    shutil.rmtree(staging, ignore_errors=True)
    return None


def _write_and_launch_swap_script(source_dir: Path, app_dir: Path):
    """
    Пишет .bat, который:
      1) ждёт, пока текущий процесс JARVIS.exe реально завершится,
      2) копирует новые файлы поверх старых (robocopy БЕЗ /MIR — ничего
         лишнего не удаляет, поэтому jarvis_config.json / logs.txt и
         прочие пользовательские файлы в app_dir остаются нетронутыми,
         они попросту не входят в source_dir),
      3) запускает обновлённый JARVIS.exe,
      4) удаляет временную папку и сам себя.
    Запускается ДО того, как текущий процесс завершится (os._exit ниже) —
    поэтому шаг 1 обязателен, а не просто "подождать секунду".
    """
    pid = os.getpid()
    bat_path = Path(tempfile.gettempdir()) / f"jarvis_update_{pid}.bat"
    bat_content = f"""@echo off
setlocal
set "PID={pid}"
set "SRC={source_dir}"
set "DST={app_dir}"

:waitloop
tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
)

robocopy "%SRC%" "%DST%" /E /IS /IT /R:3 /W:1 /NFL /NDL /NJH /NJS >nul

start "" "%DST%\\{_EXE_NAME}"

rmdir /s /q "%SRC%" >nul 2>nul
(goto) 2>nul & del "%~f0"
"""
    bat_path.write_text(bat_content, encoding="utf-8")

    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP — чтобы .bat пережил
    # завершение текущего процесса (os._exit ниже) и не унаследовал от
    # него никаких хендлов, из-за которых Windows могла бы отказаться
    # считать процесс завершённым в `tasklist`-проверке выше.
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def _do_download_and_apply_frozen(remote_ver: str, build_sha: str,
                                   build_url: str):
    """Скачивает архив сборки, готовит подмену и просит перезапустить."""
    _say(f"Скачиваю обновление до версии {remote_ver}. Это может занять минуту.")
    data = _fetch(build_url, timeout=120)
    if data is None:
        _say("Сэр, не удалось скачать архив обновления. Проверьте подключение.")
        return
    if _sha256_bytes(data) != build_sha:
        _say("Сэр, скачанный архив обновления повреждён (не совпал контрольный хеш). Отменяю установку.")
        return

    staging = _extract_and_stage(data)
    if staging is None:
        _say("Сэр, архив обновления оказался некорректным. Отменяю установку.")
        return

    # Кладём version.json с новыми данными ВНУТРЬ staging — после
    # robocopy он окажется на месте старого автоматически, без отдельной
    # гонки "успели ли мы дописать файл до старта .bat".
    try:
        (staging / "version.json").write_text(
            json.dumps({
                "version": remote_ver,
                "files": _get_local_files_record(),
                "build_sha256": build_sha,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[UPDATE] Не удалось подготовить version.json в staging: {e}")

    _say(f"Обновление скачано, версия {remote_ver} готова к установке. Перезапустить и установить сейчас?")

    def _on_confirm():
        _say("Устанавливаю обновление и перезапускаюсь.")
        time.sleep(1.0)
        try:
            _write_and_launch_swap_script(staging, _BASE)
        except Exception as e:
            print(f"[UPDATE] Не удалось запустить установку обновления: {e}")
            _say("Сэр, не удалось запустить установку обновления.")
            return
        os._exit(0)

    def _on_cancel():
        _say("Хорошо, обновление не будет установлено сейчас. При следующей проверке предложу снова.")
        shutil.rmtree(staging, ignore_errors=True)

    def _on_ignore():
        print("[UPDATE] Вопрос об установке (frozen) проигнорирован — пользователь сменил тему.")
        # Оставляем staging на диске — если пользователь позже согласится
        # без повторного вопроса, он всё равно не сможет — pending уже
        # снят. Проще и безопаснее один раз почистить: следующая проверка
        # перекачает архив заново, это дешевле, чем гонятся за отложенным
        # confirm-состоянием без вопроса на экране.
        shutil.rmtree(staging, ignore_errors=True)

    _set_pending(
        on_yes=_on_confirm,
        on_no=_on_cancel,
        on_ignore=_on_ignore,
        keywords_yes={"да", "да конечно", "установи", "перезапусти", "yes", "install",
                      "обнови", "конечно", "окей", "ок", "restart"},
        keywords_no={"нет", "не надо", "позже", "no", "отмена", "cancel", "пропусти"},
    )


# ── Основная логика (dev-режим, поштучные файлы) ──────────────────────────────
def _do_download_and_restart(changed: list, remote_ver: str, remote_files: dict):
    """Скачивает изменённые файлы, затем голосом предлагает перезапуск.
    Только dev-режим (см. докстринг модуля)."""
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

    Ветвится на dev/frozen режим сразу после получения манифеста.
    """
    local_ver = _get_local_version()
    print(f"[UPDATE] локальная версия: {local_ver} | режим: {'frozen' if _IS_FROZEN else 'dev'}")

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

    # ═══════════════════════════════ FROZEN ═══════════════════════════════
    if _IS_FROZEN:
        remote_build_sha = manifest.get("build_sha256", "")
        local_build_sha  = _get_local_build_sha()

        if not remote_build_sha:
            # Разработчик ещё не выложил build_sha256 в version.json —
            # значит релиз для frozen-режима не подготовлен. Не пытаемся
            # угадывать URL архива вслепую.
            msg = ("Сэр, версия на сервере не содержит данных о готовой сборке — "
                   "автообновление для установленной версии пока недоступно.")
            if not silent: _say(msg)
            else: print(f"[UPDATE] {msg}")
            return msg

        if remote_build_sha == local_build_sha and local_ver == remote_ver:
            msg = f"Сэр, JARVIS актуален. Версия {local_ver}."
            if not silent: _say(msg)
            else: print(f"[UPDATE] {msg}")
            return msg

        msg = f"Сэр, доступна новая версия JARVIS: {remote_ver}."
        _say(msg)
        build_url = manifest.get("build_url") or _default_build_url(
            remote_ver, manifest.get("build_asset"))
        threading.Thread(
            target=_do_download_and_apply_frozen,
            args=(remote_ver, remote_build_sha, build_url),
            daemon=True, name="JarvisFrozenUpdate"
        ).start()
        return msg

    # ═══════════════════════════════ DEV ══════════════════════════════════
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
def generate_version_json(version: str, project_dir: Path | None = None,
                          build_zip: Path | None = None,
                          build_url: str | None = None):
    """
    Генерирует version.json с SHA-256 хешами.

    - Секция "files" (поштучные хеши .py) — как и раньше, для dev-режима.
    - Если передан build_zip — дополнительно добавляет "build_sha256"
      (хеш ВСЕГО архива) и "build_asset" (имя файла архива) для
      frozen-режима. Если архив лежит не по стандартному пути на GitHub
      Releases (https://github.com/USER/REPO/releases/download/vX.Y.Z/имя),
      укажи build_url явно.

    Запустить перед каждым релизом:
        python updater.py --generate 1.0.5
        python updater.py --generate 1.0.5 --build-zip dist\\JARVIS-1.0.5-win64.zip
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

    if build_zip is not None:
        build_zip = Path(build_zip)
        if not build_zip.exists():
            print(f"[!] --build-zip указан, но файл не найден: {build_zip}")
        else:
            zip_sha = _sha256(build_zip)
            payload["build_sha256"] = zip_sha
            payload["build_asset"]  = build_zip.name
            if build_url:
                payload["build_url"] = build_url
            print(f"  + build: {build_zip.name}  sha256={zip_sha}")

    out = base / "version.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n✓ version.json → {out}  ({len(files)} файлов, версия {version})")
    if build_zip is not None and build_zip.exists():
        print(f"  Не забудь выложить {build_zip.name} в GitHub Releases под тегом v{version}"
              f" (или укажи свой --build-url, если хранишь архив в другом месте).")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="JARVIS Updater")
    p.add_argument("--generate", metavar="VER", help="Generate version.json, e.g. 1.0.1")
    p.add_argument("--build-zip", metavar="PATH",
                   help="Path to the built distribution zip (adds build_sha256 for frozen-mode updates)")
    p.add_argument("--build-url", metavar="URL",
                   help="Explicit download URL for the build zip (default: GitHub Releases pattern)")
    p.add_argument("--check",    action="store_true", help="Check for updates now")
    a = p.parse_args()
    if a.generate:
        generate_version_json(a.generate, Path("."),
                              build_zip=Path(a.build_zip) if a.build_zip else None,
                              build_url=a.build_url)
    elif a.check:
        print(check_and_update(silent=False))
    else:
        p.print_help()
