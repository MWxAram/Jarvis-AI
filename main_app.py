import os
import sys
import io
import json
import wave
import threading
import asyncio
import re
import warnings
import getpass
import time
import winreg
import ctypes
import webbrowser
import subprocess
import zipfile
import shutil
import urllib.request
import importlib.util
from ctypes import wintypes
from datetime import datetime

# ── Зеркалирование консоли в logs.txt ────────────────────────────────────────
# Всё, что раньше уходило только в окно консоли (обычные print() по всей
# программе, а также трейсбеки необработанных исключений и ошибок в потоках,
# как например RuntimeError из PyQt-потоков) — теперь дублируется в файл
# logs.txt рядом с программой. Сделано через подмену sys.stdout/sys.stderr,
# а не через logging-модуль, чтобы не переписывать сотни существующих print()
# по всему проекту.
if getattr(sys, "frozen", False):
    _LOG_DIR = os.path.dirname(sys.executable)
else:
    _LOG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_LOG_DIR, "logs.txt")


class _TeeStream:
    """Пишет одновременно в оригинальный поток консоли (если есть) и в файл."""

    def __init__(self, original, log_file):
        self._original = original  # может быть None (например, pythonw/--noconsole сборка)
        self._log_file = log_file

    def write(self, data):
        if self._original is not None:
            try:
                self._original.write(data)
            except Exception:
                pass
        try:
            self._log_file.write(data)
        except Exception:
            pass

    def flush(self):
        if self._original is not None:
            try: self._original.flush()
            except Exception: pass
        try: self._log_file.flush()
        except Exception: pass

    def isatty(self):
        return False


def _init_console_logging():
    try:
        # Простая ротация: чтобы logs.txt не рос бесконечно за месяцы работы,
        # если файл раздулся больше 5 МБ — переносим в logs.txt.old (одна
        # предыдущая копия) и начинаем новый.
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 5 * 1024 * 1024:
            old_path = os.path.join(_LOG_DIR, "logs.txt.old")
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
                os.replace(_LOG_PATH, old_path)
            except Exception as e:
                # Не критично для запуска, но стоит знать, почему ротация
                # не сработала (например, файл занят другим процессом) —
                # иначе logs.txt будет расти бесконечно без объяснений.
                print(f"[!] Не удалось выполнить ротацию logs.txt: {e}")

        log_file = open(_LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
        log_file.write(f"\n{'='*70}\n[СЕССИЯ] Запуск {datetime.now().isoformat(sep=' ', timespec='seconds')}\n{'='*70}\n")
        log_file.flush()
        sys.stdout = _TeeStream(sys.stdout, log_file)
        sys.stderr = _TeeStream(sys.stderr, log_file)
    except Exception as e:
        # Если логирование в файл не завелось — программа всё равно должна
        # нормально работать, просто без сохранения в logs.txt.
        print(f"[!] Не удалось инициализировать logs.txt: {e}")


_init_console_logging()

import numpy as np
import pytz

# ── Тяжёлые импорты — загружаются немедленно (нужны в callback) ──────────────
import sounddevice as sd
import speech_recognition as sr
import win32gui
import win32process
import win32con
import psutil

# ── Импорты которые можно отложить на ~0.5с — грузим в фоне ─────────────────
# (edge_tts, gtts, AppOpener, soundfile, pygame, openwakeword)
# Загрузка в фоне позволяет UI появиться быстрее.
_heavy_loaded = threading.Event()   # взводится когда всё готово

# Заранее объявляем как None — если какой-то из импортов в фоне упадёт,
# остальной код увидит понятное "не загружено" (None), а не NameError.
pygame = None
gTTS = None
edge_tts = None
open_app = None
sf = None
openwakeword = None

# URL известно-рабочей версии пакета openwakeword — используется, только если
# обычный "import openwakeword" падает (см. ниже). Залей openwakeword.zip
# (папка openwakeword/... внутри архива, БЕЗ __pycache__) в GitHub Release
# этого репозитория и подставь сюда точную ссылку на asset.
_OWW_FIX_URL = "https://github.com/MWxAram/Jarvis-AI/releases/download/openwakeword-fix/openwakeword.zip"


def _repair_openwakeword() -> bool:
    """
    Скачивает известно-рабочую версию пакета openwakeword и подменяет ею
    установленную — на практике встречалась сборка, где версия openwakeword,
    подтянутая pip'ом (requirements.txt не пинует точную версию), несовместима
    с окружением и падает при импорте с "DLL load failed while importing
    onnxruntime_pybind11_state" — при этом сам onnxruntime не сломан, ломает
    именно эта версия openwakeword. Подмена пакета на известно-рабочую версию
    это чинит.

    ТОЛЬКО текст в консоль/logs.txt — без голоса: голосовое объявление здесь
    перебивалось параллельным озвучиванием проверки обновлений (см. main_app
    → check_startup), и звук просто обрывался на середине. Печатать надёжнее.

    ВАЖНО: НЕ пытается тут же повторно импортировать openwakeword и продолжить
    работу в этой же сессии — Python уже мог что-то закэшировать/частично
    инициализировать при первой неудачной попытке импорта, поэтому после
    подмены файлов требуется полный перезапуск процесса. Функция только
    готовит файлы и просит перезапуск; повторный импорт НЕ делает.

    Возвращает True, если файлы подменены успешно (не значит, что импорт
    заработает — это будет ясно уже при следующем запуске).
    """
    try:
        # find_spec просто НАХОДИТ пакет на диске, не выполняя его код —
        # работает, даже если сам import падает при выполнении __init__.py.
        spec = importlib.util.find_spec("openwakeword")
        if spec is None or not spec.origin:
            print("[REPAIR] openwakeword не найден в site-packages — чинить нечего.")
            return False
        pkg_dir = os.path.dirname(spec.origin)          # .../site-packages/openwakeword
        site_packages_dir = os.path.dirname(pkg_dir)

        print("[REPAIR] openwakeword не загрузился — качаю известно-рабочую версию...")
        with urllib.request.urlopen(_OWW_FIX_URL, timeout=30) as resp:
            data = resp.read()

        # Сначала распаковываем во временную папку и проверяем, что внутри
        # архива действительно лежит "openwakeword/" — только потом сносим
        # старую версию, чтобы при битом/неверном архиве не остаться без
        # пакета вообще.
        tmp_extract = pkg_dir + "_repair_tmp"
        shutil.rmtree(tmp_extract, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(tmp_extract)
        new_pkg_dir = os.path.join(tmp_extract, "openwakeword")
        if not os.path.isdir(new_pkg_dir):
            print("[REPAIR] В архиве не нашлась папка openwakeword/ — отмена.")
            shutil.rmtree(tmp_extract, ignore_errors=True)
            return False

        shutil.rmtree(pkg_dir, ignore_errors=True)
        shutil.move(new_pkg_dir, pkg_dir)
        shutil.rmtree(tmp_extract, ignore_errors=True)

        print(f"[REPAIR] openwakeword переустановлен в {pkg_dir}.")
        print("[REPAIR] Требуется перезапуск JARVIS, чтобы изменения вступили в силу.")
        return True
    except Exception as e:
        print(f"[REPAIR] Не удалось автоматически починить openwakeword: {e}")
        return False


def _load_heavy_imports():
    global pygame, gTTS, edge_tts, open_app, sf, openwakeword
    # ВАЖНО: каждый импорт — в своём try/except. Раньше все шесть импортов
    # были в одном блоке без обработки ошибок: если ЛЮБОЙ из них падал
    # (например, openwakeword/onnxruntime не встал корректно в frozen-сборку),
    # исключение тихо убивало этот фоновый поток и _heavy_loaded.set() ниже
    # никогда не вызывался. Из-за этого:
    #   - _load_ww_model() вечно ждал на _heavy_loaded.wait() → wake-word
    #     ("Hey Jarvis") никогда не запускался → JARVIS не реагировал на голос;
    #   - pygame не был инициализирован → воспроизведение TTS тоже не работало.
    # Теперь падение одной библиотеки не блокирует остальные, а в консоль/
    # logs.txt печатается точная причина — по ней и чиним конкретный пакет.
    try:
        import pygame as _pg
        pygame = _pg
    except Exception as e:
        print(f"[HEAVY IMPORT] pygame не загрузился: {e}")

    try:
        from gtts import gTTS as _gtts
        gTTS = _gtts
    except Exception as e:
        print(f"[HEAVY IMPORT] gTTS не загрузился: {e}")

    try:
        import edge_tts as _edge_tts
        edge_tts = _edge_tts
    except Exception as e:
        print(f"[HEAVY IMPORT] edge_tts не загрузился: {e}")

    try:
        from AppOpener import open as _open_app
        open_app = _open_app
    except Exception as e:
        print(f"[HEAVY IMPORT] AppOpener не загрузился: {e}")

    try:
        import soundfile as _sf
        sf = _sf
    except Exception as e:
        print(f"[HEAVY IMPORT] soundfile не загрузился: {e}")

    try:
        import openwakeword as _oww
        openwakeword = _oww
    except Exception as e:
        print(f"[HEAVY IMPORT] openwakeword не загрузился: {e}")
        if _repair_openwakeword():
            print("[HEAVY IMPORT] Автопочинка завершена. Закройте JARVIS и запустите "
                  "заново, чтобы голосовая активация 'Hey Jarvis' заработала.")
        else:
            print("[HEAVY IMPORT] Автопочинка не удалась — голосовая активация "
                  "'Hey Jarvis' недоступна в этой сессии.")

    _heavy_loaded.set()  # выставляем ВСЕГДА, даже если что-то выше упало

# Запускаем фоновую загрузку сразу — к моменту первой команды всё будет готово
threading.Thread(target=_load_heavy_imports, daemon=True, name="HeavyImports").start()

from google import genai
import jarvis_ui
import jarvis_features as jf
import updater
try:
    import jarvis_analytics as analytics
except Exception as _e:
    analytics = None
    print(f"[ANALYTICS] jarvis_analytics.py не загружен ({_e}) - сбор статистики недоступен.")

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide"
warnings.filterwarnings("ignore")

import ctypes
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("mycompany.jarvis.system.v07")
except Exception:
    pass

def _run_async(coro):
    """
    Запускает корутину в свежем event loop текущего потока.
    Совместимо с Python 3.10+ (get_event_loop deprecated без running loop).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ── Hot-reload all settings without restart ────────────────────────────────
# Called automatically by jarvis_ui every time the user saves settings.
# Updates ALL module-level globals so changes take effect immediately.
def _on_settings_saved(cfg: dict):
    global VOICE_RATE, VOICE_PITCH, VOICE_JARVIS, VOICE_RUSSIAN
    global MODEL_ID, VOICE_THRESHOLD, SILENCE_LIMIT, MAX_HISTORY
    if "voice_rate" in cfg:
        VOICE_RATE = cfg["voice_rate"]
        print(f"[CFG] voice_rate → {VOICE_RATE}")
    if "voice_pitch" in cfg:
        try:
            _hz = int(round(float(cfg["voice_pitch"]) * 25))
            VOICE_PITCH = f"+{_hz}Hz" if _hz >= 0 else f"{_hz}Hz"
        except Exception:
            VOICE_PITCH = "+0Hz"
        print(f"[CFG] voice_pitch → {VOICE_PITCH}")
    if "voice_jarvis" in cfg:
        VOICE_JARVIS = cfg["voice_jarvis"]
        VOICE_MAP["en"] = VOICE_JARVIS   # detect_voice тоже использует новый голос
        print(f"[CFG] voice_jarvis → {VOICE_JARVIS}")
    if "voice_russian" in cfg:
        VOICE_RUSSIAN = cfg["voice_russian"]
        VOICE_MAP["ru"] = VOICE_RUSSIAN  # detect_voice тоже использует новый голос
        print(f"[CFG] voice_russian → {VOICE_RUSSIAN}")
    if "model_id" in cfg:
        MODEL_ID = cfg["model_id"]
        print(f"[CFG] model_id → {MODEL_ID}")
    if "threshold" in cfg:
        try: VOICE_THRESHOLD = float(cfg["threshold"])
        except Exception: pass
        print(f"[CFG] threshold → {VOICE_THRESHOLD}")
    if "silence_limit" in cfg:
        try: SILENCE_LIMIT = int(cfg["silence_limit"])
        except Exception: pass
        print(f"[CFG] silence_limit → {SILENCE_LIMIT}")
    if "max_history" in cfg:
        try: MAX_HISTORY = int(cfg["max_history"])
        except Exception: pass
        print(f"[CFG] max_history → {MAX_HISTORY}")

# --- КОНФИГУРАЦИЯ ---

_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
_cfg_lock = threading.Lock()   # защищает чтение/запись jarvis_config.json от гонок между потоками

# ✅ ОПТИМИЗАЦИЯ: кэш конфига по mtime файла. Раньше _load_jarvis_config()
# перечитывала и парсила jarvis_config.json с диска на КАЖДЫЙ вызов
# _get_client(), то есть на каждый запрос к Gemini — лишний disk I/O +
# JSON-parsing на каждый диалоговый ход. Теперь читаем заново только если
# файл реально изменился (например, после сохранения настроек в UI).
_cfg_cache = {}
_cfg_cache_mtime = None

def _load_jarvis_config(force: bool = False):
    """
    Возвращает конфиг из jarvis_config.json с кэшированием по mtime.
    force=True — игнорировать кэш (используется сразу после записи файла).
    """
    global _cfg_cache, _cfg_cache_mtime
    with _cfg_lock:
        try:
            mtime = os.path.getmtime(_CFG_PATH)
        except OSError:
            return {}
        if not force and _cfg_cache_mtime == mtime:
            return _cfg_cache
        try:
            with open(_CFG_PATH, encoding="utf-8") as f:
                _cfg_cache = json.load(f)
            _cfg_cache_mtime = mtime
        except Exception as e:
            print(f"[CFG][WARN] Не удалось прочитать jarvis_config.json: {e}")
        return _cfg_cache

_CFG = _load_jarvis_config()

# API-ключ читается лениво: при первом обращении берём из конфига или env.
# Это позволяет запускать программу без ключа и добавить его позже через настройки.
_genai_client = None   # None = ещё не инициализирован

def _get_client():
    """
    Возвращает genai.Client.
    Порядок чтения ключа:
      1. api_key_enc (зашифрован через jarvis_ui._decrypt_key) — приоритет
      2. api_key (plain) — только для обратной совместимости, сразу мигрирует в enc
      3. GEMINI_API_KEY (переменная окружения)
    """
    global _genai_client
    _live_cfg = _load_jarvis_config()   # быстрый кэш-хит, если файл не менялся

    # 1. Encrypted key (приоритет)
    api_key = ""
    enc = _live_cfg.get("api_key_enc", "").strip()
    if enc:
        try:
            api_key = jarvis_ui._decrypt_key(enc) or ""
        except Exception:
            api_key = ""

    # 2. Plain key — fallback + автоматическая миграция в зашифрованный
    if not api_key:
        plain = _live_cfg.get("api_key", "").strip()
        if plain:
            api_key = plain
            # Мигрируем: шифруем и убираем plain из конфига
            try:
                new_enc = jarvis_ui._encrypt_key(plain)
                if new_enc:
                    # ✅ FIX: чтение+запись конфига теперь под _cfg_lock — раньше
                    # два потока могли одновременно прочитать/переписать файл и
                    # затереть изменения друг друга (гонка при записи).
                    with _cfg_lock:
                        with open(_CFG_PATH, "r", encoding="utf-8") as _f:
                            _cfg_data = json.load(_f)
                        _cfg_data["api_key_enc"] = new_enc
                        _cfg_data.pop("api_key", None)   # убираем plain
                        with open(_CFG_PATH, "w", encoding="utf-8") as _f:
                            json.dump(_cfg_data, _f, ensure_ascii=False, indent=2)
                    _load_jarvis_config(force=True)  # обновляем кэш сразу, не ждём следующего mtime-чека
                    print("[AI] API ключ автоматически зашифрован и plain-версия удалена.")
            except Exception as _e:
                print(f"[AI][WARN] Не удалось зашифровать ключ при миграции: {_e}")

    # 3. Environment variable
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        return None

    if _genai_client is None or getattr(_genai_client, "_api_key_used", None) != api_key:
        _genai_client = genai.Client(api_key=api_key)
        _genai_client._api_key_used = api_key
        print(f"[AI] Клиент Gemini инициализирован (ключ: ...{api_key[-4:]})")
    return _genai_client

MODEL_ID   = _CFG.get("model_id",   "gemini-2.5-flash-lite")
VOICE_RATE = _CFG.get("voice_rate", "+15%")
# Конфиг хранит pitch в семитонах ("0.0", "0.5", "-1.0" …)
# edge_tts ожидает строку вида "+0Hz", "+25Hz", "-25Hz"
try:
    _pitch_hz = int(round(float(_CFG.get("voice_pitch", "0.0")) * 25))
    VOICE_PITCH = f"+{_pitch_hz}Hz" if _pitch_hz >= 0 else f"{_pitch_hz}Hz"
except Exception:
    VOICE_PITCH = "+0Hz"

# --- КАСТОМНЫЕ КОМАНДЫ И WAKE WORD ---
import uuid as _uuid

_COMMANDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_commands.json")

def _load_custom_data():
    if os.path.exists(_COMMANDS_FILE):
        try:
            return json.load(open(_COMMANDS_FILE, encoding="utf-8"))
        except Exception as e:
            # Раньше это молча возвращало пустые defaults — пользователь мог
            # потерять свой custom_wake_word/команды из-за битого JSON и даже
            # не узнать об этом. Теперь хотя бы видно в логе.
            print(f"[!] jarvis_commands.json повреждён или не читается ({e}) — использую значения по умолчанию.")
    return {"custom_wake_word": None, "commands": []}

def _exec_custom_command(cmd):
    """Выполняет список действий кастомной команды с задержками."""
    def _run():
        for action in cmd.get("actions", []):
            delay = action.get("delay_ms", 0)
            if delay > 0:
                time.sleep(delay / 1000.0)
            path = action.get("path", "").strip()
            if not path:
                continue
            try:
                if path.startswith("http://") or path.startswith("https://"):
                    webbrowser.open(path)
                else:
                    os.startfile(path)
                print(f"[CUSTOM] Открыто: {path}")
            except Exception as e:
                print(f"[CUSTOM] Ошибка открытия '{path}': {e}")
    threading.Thread(target=_run, daemon=True).start()


USER_NAME = getpass.getuser()
VOICE_JARVIS   = _CFG.get("voice_jarvis",  "en-GB-ThomasNeural")
VOICE_RUSSIAN  = _CFG.get("voice_russian", "ru-RU-DmitryNeural")
VOICE_ARMENIAN = "hy"

FS = 16000
VOICE_THRESHOLD = float(_CFG.get("threshold",     12.0))
SILENCE_LIMIT   = int(_CFG.get("silence_limit",  40))
CHUNK = 1024

# ✅ ОПТИМИЗАЦИЯ: глобальный recognizer переиспользуется (не создаётся в каждом вызове)
r_global = sr.Recognizer()
r_global.non_speaking_duration = 0.2
r_global.pause_threshold = 1.0
r_global.phrase_threshold = 0.3

is_processing = threading.Event()  # set() пока background_worker работает
is_speaking   = threading.Event()  # set() пока TTS воспроизводится

# Флаг прерывания TTS — устанавливается кнопкой "Стоп" в UI.
# play_voice_async проверяет его каждые 50мс и останавливает pygame.
_tts_stop = threading.Event()

recording = False
_listening_printed = True  # флаг вне функции callback
audio_buffer = []
silence_counter = 0

_jarvis_lock = threading.Lock()   # защита opened_by_jarvis
_last_opened = None  # {'type': 'browser'|'app'|'folder', 'name': str, 'target': str}
# Подтверждение "сохранить каталог команд в .txt?" теперь регистрируется через
# updater.set_pending_confirm() — общий стек да/нет-ожиданий (см. updater.py),
# чтобы не конфликтовать с подтверждением обновления/перезапуска.

VIRTUAL_CABLE_NAME = "CABLE Input"

# --- АВТОСМЕНА МИКРОФОНА ---
# Имя виртуального микрофона (CABLE Output — это то, что слышат другие программы)
VIRTUAL_MIC_NAME = "CABLE Output"
_saved_default_mic_id = None    # ID оригинального микрофона (сохраняем перед сменой)
_mic_was_switched = False       # флаг: мы меняли микрофон или нет

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
realtime_translation_mode = False
conversation_mode = False
conversation_timeout = 0
LAST_ACTIVITY = 0
translator_mode = False
translator_target_lang = None
translator_voice = None

conversation_history = []
opened_by_jarvis = []
MAX_HISTORY = int(_CFG.get("max_history", 20))

# ✅ FIX: раньше SYSTEM_PROMPT вообще не содержал номер версии — Джарвис
# на вопрос "какая у тебя версия" галлюцинировал случайное число (видели
# в логах "1.0", "1.0.0"), хотя реальная версия лежит в version.json и
# updater.py её прекрасно знает. Читаем её один раз при старте и жёстко
# прописываем в промпт, чтобы модели было неоткуда взять другое число.
JARVIS_VERSION = updater._get_local_version()

# ── Учёт использованных токенов ─────────────────────────────────────────
# У Gemini API нет публичного эндпоинта "сколько квоты осталось" — Google
# отдаёт это только через веб-интерфейс AI Studio, не через API-ключ (см.
# https://ai.google.dev — квоты видны только в дашборде). Поэтому вместо
# несуществующего "остатка" считаем то, что реально можем знать: сколько
# токенов потрачено — из usage_metadata каждого ответа Gemini.
_USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_usage.json")
_usage_lock = threading.Lock()

def _load_token_usage() -> dict:
    try:
        with open(_USAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "", "today_tokens": 0, "total_tokens": 0, "total_requests": 0}

def _record_token_usage(response):
    """Вызывать после каждого успешного generate_content(). Тихо не делает
    ничего, если у ответа нет usage_metadata (другая версия SDK и т.п.)."""
    try:
        meta = getattr(response, "usage_metadata", None)
        total = getattr(meta, "total_token_count", None) if meta else None
        if total is None:
            return
        today = datetime.now(pytz.timezone("Asia/Yerevan")).strftime("%Y-%m-%d")
        with _usage_lock:
            data = _load_token_usage()
            if data.get("date") != today:
                data["date"] = today
                data["today_tokens"] = 0
            data["today_tokens"] = data.get("today_tokens", 0) + total
            data["total_tokens"] = data.get("total_tokens", 0) + total
            data["total_requests"] = data.get("total_requests", 0) + 1
            with open(_USAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[USAGE][WARN] Не удалось записать статистику токенов: {e}")

SYSTEM_PROMPT = f"""
You are Jarvis. User: {USER_NAME}. Location: Armenia.
Your exact current version is {JARVIS_VERSION} — this is the ONLY correct
answer if asked about your version. NEVER invent or guess a different
version number, and never say you don't know it — you always know it,
it's {JARVIS_VERSION}.

STRICT RULE: ALWAYS respond in the SAME language as the user. NEVER switch to English.
If user speaks Russian → respond in Russian only. No English words at all.
If user says "5+5" or any math in Russian context → answer only in Russian (e.g. "10").
If user speaks English → respond in English only.

Rules:
1. ALWAYS respond in the SAME language the user used. No exceptions. No mixing languages.
2. You have memory of the current session — use it. Remember user's name if told.
3. When user says open/включи/открой/запусти — immediately do it. NEVER ask questions.
4. When user says close/закрой one thing — use [CLOSE:name].
5. When user says close ALL of something — use [CLOSEALL:name].
6. When user says close all tabs opened by YOU — use [CLOSEALL_ME:name].
7. When user says close all tabs opened by ME/USER — use [CLOSEALL_USER:name].
8. Actions:
   - [URL:https://...] — open website
   - [YOUTUBE:query] — open YouTube (empty query = just open homepage)
   - [RUTUBE:query] — open Rutube
   - [SEARCH:query] — Google search
   - [APP:name] — open app
   - [PATH:path] — open file/folder
   - [CLOSE:name] — close app/tab/folder (smart: Jarvis tabs first, then user tabs)
   - [CLOSEALL:name] — close ALL tabs of this site (both Jarvis and user)
   - [CLOSEALL_ME:name] — close only tabs Jarvis opened
   - [CLOSEALL_USER:name] — close only tabs user opened
   - [SHUTDOWN] — shut down Jarvis
   - [TRANSLATOR_ON:язык] — включить ОБЫЧНЫЙ режим перевода (для себя, в динамики, голосом Jarvis). Используй ТОЛЬКО это когда пользователь говорит "включи переводчик", "переводи мне", "режим перевода" БЕЗ слов "реальное время" / "реалтайм" / "в микрофон" / "в кабель".
   - [TRANSLATOR_OFF] — выключить режим перевода
   - [RT_TRANSLATOR_ON:язык] — включить реалтайм перевод (вывод в виртуальный кабель). ТОЛЬКО если пользователь явно сказал "реальное время", "реалтайм", "в кабель", "для стрима".
9. Site map: ВК/VK→[URL:https://vk.com], YouTube/Ютуб→[URL:https://youtube.com], Google/Гугл→[URL:https://google.com]
10. Style: concise, polite, use user's name if known, match user's language exactly.
11. Below the rules you'll see a "Текущий статус Джарвиса/ПК" block with REAL live
    data (volume, VIP status, whether the Gemini API key is set, version, model,
    tokens used). If asked about yourself/the PC (your volume, VIP, API key,
    version, tokens, etc.) — answer ONLY using that block. Never guess or invent
    a different value, never say you don't know — you always have this data.
    If asked how many tokens are LEFT/remaining — be honest: Google's API gives
    no way to check the remaining free-tier quota programmatically, only the
    amount USED (which you do know, from the block). Tell the user how much was
    used and mention the exact remaining quota is only visible in Google AI Studio.
12. When your answer covers a concrete real-world topic that naturally has a web
    page (news, a wiki article, a game/product update, a place, an event, etc.) —
    after answering, ASK in your own words whether to open a page about it. Do
    NOT include [URL:...]/[SEARCH:...] in that same message — just ask, e.g.
    "Хотите, я открою страницу об этом?". Only if the user confirms in a LATER
    message (да, открой, давай, конечно, ...) — THEN, using the session history
    above to recall what was discussed, respond with the actual [URL:...] or
    [SEARCH:...] tag to open it. Don't offer this for trivial answers (math,
    time, simple facts) — only when a page would genuinely add value.
"""

TRANSLATOR_LANGS = {
    "русский": ("Russian", "ru-RU-DmitryNeural"),
    "по-русски": ("Russian", "ru-RU-DmitryNeural"),
    "английский": ("English", "en-GB-ThomasNeural"),
    "по-английски": ("English", "en-GB-ThomasNeural"),
    "english": ("English", "en-GB-ThomasNeural"),
    "армянский": ("Armenian", VOICE_ARMENIAN),
    "по-армянски": ("Armenian", VOICE_ARMENIAN),
    "немецкий": ("German", "de-DE-ConradNeural"),
    "по-немецки": ("German", "de-DE-ConradNeural"),
    "французский": ("French", "fr-FR-HenriNeural"),
    "по-французски": ("French", "fr-FR-HenriNeural"),
    "испанский": ("Spanish", "es-ES-AlvaroNeural"),
    "по-испански": ("Spanish", "es-ES-AlvaroNeural"),
    "турецкий": ("Turkish", "tr-TR-AhmetNeural"),
    "по-турецки": ("Turkish", "tr-TR-AhmetNeural"),
    "арабский": ("Arabic", "ar-SA-HamedNeural"),
    "по-арабски": ("Arabic", "ar-SA-HamedNeural"),
    "китайский": ("Chinese", "zh-CN-YunxiNeural"),
    "по-китайски": ("Chinese", "zh-CN-YunxiNeural"),
    "японский": ("Japanese", "ja-JP-KeitaNeural"),
    "по-японски": ("Japanese", "ja-JP-KeitaNeural"),
}

# ✅ ОПТИМИЗАЦИЯ: компилируем regex один раз при запуске, а не при каждом вызове
RE_PATTERNS = {
    "yt":            re.compile(r'\[YOUTUBE:(.*?)\]'),
    "rt_tube":       re.compile(r'\[RUTUBE:(.*?)\]'),
    "srch":          re.compile(r'\[SEARCH:(.*?)\]'),
    "url":           re.compile(r'\[URL:(.*?)\]'),
    "app":           re.compile(r'\[APP:(.*?)\]'),
    "path":          re.compile(r'\[PATH:(.*?)\]'),
    "close":         re.compile(r'\[CLOSE:(.*?)\]'),
    "close_all":     re.compile(r'\[CLOSEALL:(.*?)\]'),
    "close_all_me":  re.compile(r'\[CLOSEALL_ME:(.*?)\]'),
    "close_all_usr": re.compile(r'\[CLOSEALL_USER:(.*?)\]'),
    "shutdown":      re.compile(r'\[SHUTDOWN\]'),
    "trans_on":      re.compile(r'\[TRANSLATOR_ON:(.*?)\]'),
    "trans_off":     re.compile(r'\[TRANSLATOR_OFF\]'),
    "rt_trans_on":   re.compile(r'\[RT_TRANSLATOR_ON:(.*?)\]'),
    "clean_tags":    re.compile(r'\[.*?\]'),
}

VOICE_MAP = {
    "ru": _CFG.get("voice_russian", "ru-RU-DmitryNeural"),
    "en": _CFG.get("voice_jarvis",  "en-GB-ThomasNeural"),
    "hy": "hy",
    "de": "de-DE-ConradNeural",
    "fr": "fr-FR-HenriNeural",
    "es": "es-ES-AlvaroNeural",
    "ar": "ar-SA-HamedNeural",
    "tr": "tr-TR-AhmetNeural",
    "zh": "zh-CN-YunxiNeural",
    "ja": "ja-JP-KeitaNeural",
}

# ✅ ОПТИМИЗАЦИЯ: кэш индекса виртуального устройства
_virtual_device_idx = None

def get_virtual_device_idx():
    global _virtual_device_idx
    if _virtual_device_idx is None:
        devices = sd.query_devices()
        _virtual_device_idx = next(
            (i for i, d in enumerate(devices)
             if VIRTUAL_CABLE_NAME in d['name'] and d['max_output_channels'] > 0),
            -1
        )
    return _virtual_device_idx if _virtual_device_idx != -1 else None


# --- АВТОСМЕНА МИКРОФОНА (для реалтайм-перевода) ---

def _get_default_mic_id_from_registry():
    """
    Читает ID текущего дефолтного микрофона из реестра Windows.
    Возвращает строку вида '{0.0.1.00000000}.{guid}' или None.
    """
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Multimedia\Sound Mapper"
        )
        val, _ = winreg.QueryValueEx(key, "Record")
        winreg.CloseKey(key)
        return val  # это имя устройства (friendly name), не ID
    except Exception:
        pass
    # Резервный путь: читаем через MMDevice API через реестр
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
        )
        # Не надёжно — лучше используем PowerShell ниже
        winreg.CloseKey(key)
    except Exception:
        pass
    return None


def _powershell(cmd: str) -> str:
    """Выполняет PowerShell-команду и возвращает stdout (stripped)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=8
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"[MIC] PowerShell error: {e}")
        return ""


def get_default_mic_name() -> str | None:
    """
    Возвращает friendly-name текущего дефолтного микрофона через PowerShell.
    Например: 'Microphone (Realtek High Definition Audio)'
    """
    # Надёжный метод: читаем из реестра Sound Mapper
    cmd_reliable = r"""
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
if (-not (Test-Path $key)) { exit }
$default = $null
Get-ChildItem $key | ForEach-Object {
    $props = Get-ItemProperty (Join-Path $_.PSPath 'Properties') -ErrorAction SilentlyContinue
    if ($props) {
        $role = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DeviceState
        # Ищем активное устройство, выбранное по умолчанию
    }
}
# Самый простой метод: через реестр Sound Mapper
$sm = (Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Multimedia\Sound Mapper' -ErrorAction SilentlyContinue).Record
if ($sm) { Write-Output $sm }
"""
    name = _powershell(cmd_reliable).strip()
    if name:
        return name

    # Запасной метод: через sounddevice — берём текущий дефолтный input
    try:
        dev = sd.query_devices(kind='input')
        return dev['name'] if dev else None
    except Exception:
        return None


def _mic_find_full_name(keyword: str) -> str | None:
    """Ищет устройство ввода по ключевому слову. Возвращает полное имя."""
    kw = keyword.lower()
    for dev in sd.query_devices():
        if dev['max_input_channels'] > 0 and kw in dev['name'].lower():
            return dev['name']
    return None


def _mic_get_default_name() -> str | None:
    """
    Возвращает имя текущего дефолтного микрофона через sounddevice.
    sounddevice читает Windows Audio API напрямую — кодировка всегда правильная.
    """
    try:
        dev = sd.query_devices(kind='input')
        if dev:
            return dev['name']
    except Exception:
        pass
    # Запасной вариант: первое активное устройство ввода не являющееся виртуальным
    for dev in sd.query_devices():
        if dev['max_input_channels'] > 0 and VIRTUAL_MIC_NAME.lower() not in dev['name'].lower():
            return dev['name']
    return None


def _mic_set_default(partial_name: str) -> bool:
    """
    Устанавливает системный микрофон по умолчанию через PowerShell + IPolicyConfig.
    partial_name — часть имени (например 'CABLE Output' или 'Usb Microphone').
    Возвращает True при успехе.
    """
    if not partial_name:
        return False

    # C#-код компилируется внутри PowerShell — не зависит от кодировки консоли
    # Имя передаём как параметр, PowerShell принимает его как Unicode-строку
    ps_script = r"""
param([string]$DeviceName)
$code = @'
using System;
using System.Runtime.InteropServices;
[Guid("f8679f50-850a-41cf-9c72-430f290290c8"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
    void _1();void _2();void _3();void _4();void _5();void _6();void _7();void _8();void _9();void _10();
    [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)]string id,int role);
}
[Guid("a95664d2-9614-4f35-a746-de8db63617e6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    [PreserveSig]int EnumAudioEndpoints(int f,int s,out IMMDeviceCollection c);
    [PreserveSig]int GetDefaultAudioEndpoint(int f,int r,out IMMDevice d);
    [PreserveSig]int GetDevice([MarshalAs(UnmanagedType.LPWStr)]string id,out IMMDevice d);
    void _4();void _5();
}
[Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceCollection{[PreserveSig]int GetCount(out uint n);[PreserveSig]int Item(uint i,out IMMDevice d);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    [PreserveSig]int Activate(ref Guid g,int c,IntPtr p,out IntPtr i);
    [PreserveSig]int OpenPropertyStore(int a,out IPropertyStore s);
    [PreserveSig]int GetId([MarshalAs(UnmanagedType.LPWStr)]out string id);
    void _4();
}
[Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore{void _1();void _2();[PreserveSig]int GetValue(ref PK k,out PV v);void _4();}
[StructLayout(LayoutKind.Sequential)]struct PK{public Guid g;public uint p;}
[StructLayout(LayoutKind.Sequential)]struct PV{public ushort vt,r1,r2,r3;public IntPtr ptr;public int p2;}
public class MicSwitch {
    static Guid ECLSID=new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E");
    static Guid PCLSID=new Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9");
    public static bool Set(string partial){
        try{
            var e=(IMMDeviceEnumerator)Activator.CreateInstance(Type.GetTypeFromCLSID(ECLSID));
            IMMDeviceCollection col; e.EnumAudioEndpoints(1,1,out col);
            uint cnt; col.GetCount(out cnt);
            for(uint i=0;i<cnt;i++){
                IMMDevice d; col.Item(i,out d);
                string id; d.GetId(out id);
                IPropertyStore s; d.OpenPropertyStore(0,out s);
                var k=new PK{g=new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"),p=14};
                var v=new PV(); s.GetValue(ref k,out v);
                string nm=Marshal.PtrToStringUni(v.ptr)??"";
                if(nm.ToLower().Contains(partial.ToLower())){
                    var pc=(IPolicyConfig)Activator.CreateInstance(Type.GetTypeFromCLSID(PCLSID));
                    pc.SetDefaultEndpoint(id,0);
                    pc.SetDefaultEndpoint(id,1);
                    pc.SetDefaultEndpoint(id,2);
                    return true;
                }
            }
        }catch(Exception ex){Console.Error.WriteLine(ex.Message);}
        return false;
    }
}
'@
Add-Type -TypeDefinition $code -Language CSharp -ErrorAction Stop
# Выводим результат как ASCII-цифру: 1=true, 0=false — не зависит от кодировки консоли
if ([MicSwitch]::Set($DeviceName)) { Write-Output "1" } else { Write-Output "0" }
"""
    ps_path = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "_jarvis_mic.ps1")
    try:
        # UTF-8 с BOM — PowerShell читает файл корректно
        with open(ps_path, "w", encoding="utf-8-sig") as f:
            f.write(ps_script)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", ps_path,
             "-DeviceName", partial_name],
            capture_output=True, timeout=12
        )
        # Читаем сырые байты и декодируем: ищем "1" или "0"
        raw = result.stdout
        out = ""
        for enc in ("utf-16", "utf-8", "cp1251"):
            try:
                out = raw.decode(enc).strip()
                break
            except Exception:
                continue
        ok = out.strip() == "1"
        print(f"[MIC] set '{partial_name}' → {'✓ OK' if ok else '✗ FAIL'} (raw: {out!r})")
        return ok
    except Exception as e:
        print(f"[MIC] set error: {e}")
        return False
    finally:
        try:
            os.remove(ps_path)
        except Exception:
            pass


def switch_mic_to_virtual():
    """При включении RT-перевода: сохраняет текущий микрофон и переключает на CABLE Output."""
    global _saved_default_mic_id, _mic_was_switched

    if _mic_was_switched:
        return

    # Проверяем что CABLE Output вообще есть в системе
    virtual_full = _mic_find_full_name(VIRTUAL_MIC_NAME)
    if not virtual_full:
        print(f"[MIC] ⚠ '{VIRTUAL_MIC_NAME}' не найден — смена пропущена")
        return

    # Сохраняем текущий микрофон через sounddevice (всегда правильная кодировка)
    saved = _mic_get_default_name()
    if saved:
        _saved_default_mic_id = saved
        print(f"[MIC] Сохранён: '{saved}'")
    else:
        print("[MIC] ⚠ Не удалось определить текущий микрофон")

    # Переключаем на CABLE Output
    ok = _mic_set_default(VIRTUAL_MIC_NAME)
    if ok:
        _mic_was_switched = True
        print(f"[MIC] ✓ Переключён на '{virtual_full}'")
    else:
        print(f"[MIC] ✗ Не удалось переключить на '{virtual_full}'")


def restore_mic():
    """При выключении RT-перевода: возвращает сохранённый микрофон."""
    global _saved_default_mic_id, _mic_was_switched

    if not _mic_was_switched:
        return

    if not _saved_default_mic_id:
        print("[MIC] ⚠ Нет сохранённого микрофона")
        _mic_was_switched = False
        return

    ok = _mic_set_default(_saved_default_mic_id)
    if ok:
        print(f"[MIC] ✓ Восстановлен: '{_saved_default_mic_id}'")
    else:
        print(f"[MIC] ✗ Не удалось восстановить '{_saved_default_mic_id}'")

    _mic_was_switched = False
    _saved_default_mic_id = None


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_live_status_context():
    """
    Живой статус самого Джарвиса/ПК на момент запроса — чтобы модель
    отвечала на вопросы о себе ("какая у тебя громкость", "активирован
    ли VIP", "есть ли у тебя ключ ИИ") реальными данными, а не гадала
    (как раньше было с версией — см. фикс JARVIS_VERSION).
    Читаем по живым данным на каждый запрос, а не кэшируем — громкость и
    VIP-статус могут поменяться между запросами.
    """
    cfg = _load_jarvis_config()

    # Громкость системы (0-100%, либо "не удалось определить")
    try:
        vol = jf._get_master_volume()
        vol_str = f"{round(vol * 100)}%" if vol is not None else "не удалось определить"
    except Exception:
        vol_str = "не удалось определить"

    # VIP: по последней успешной/неуспешной проверке (см. jarvis_vip.py)
    vip_valid = bool(cfg.get("vip_last_valid"))
    if vip_valid:
        vip_plan = cfg.get("vip_last_plan") or "неизвестный план"
        vip_expires = cfg.get("vip_last_expires_at")
        if vip_expires:
            try:
                _exp_dt = datetime.fromisoformat(vip_expires.replace("Z", "+00:00"))
                vip_expires_str = _exp_dt.strftime("%d.%m.%Y")
            except Exception:
                vip_expires_str = vip_expires
            vip_str = f"активирован ({vip_plan}), истекает {vip_expires_str}"
        else:
            vip_str = f"активирован ({vip_plan}), бессрочно / дата окончания неизвестна"
    else:
        vip_str = "не активирован"

    # Наличие ключа Gemini (сам ключ никогда не раскрываем модели)
    has_key = bool((cfg.get("api_key_enc") or "").strip() or (cfg.get("api_key") or "").strip())
    key_str = "указан" if has_key else "НЕ указан — функции ИИ не будут работать"

    # Токены: у Gemini API нет способа узнать ОСТАТОК квоты через API-ключ
    # (это видно только в вебе, AI Studio) — считаем то, что реально можем
    # знать: сколько потрачено, из usage_metadata каждого ответа.
    _usage = _load_token_usage()
    usage_str = (
        f"использовано сегодня — {_usage.get('today_tokens', 0)}, "
        f"всего за всё время — {_usage.get('total_tokens', 0)} "
        f"({_usage.get('total_requests', 0)} запросов). "
        "Точный ОСТАТОК бесплатной квоты Google не отдаёт через API — "
        "только в вебе, в Google AI Studio."
    )

    return (
        "--- Текущий статус Джарвиса/ПК (реальные данные, отвечай ТОЛЬКО ими, "
        "никогда не выдумывай другие значения) ---\n"
        f"Версия: {JARVIS_VERSION}\n"
        f"Громкость системы: {vol_str}\n"
        f"VIP-кастомизация: {vip_str}\n"
        f"API-ключ Gemini: {key_str}\n"
        f"Модель ИИ: {MODEL_ID}\n"
        f"Токены (потрачено, НЕ остаток — см. пояснение): {usage_str}\n"
        "---\n"
    )


def get_session_context():
    if not conversation_history:
        return ""
    # ✅ ОПТИМИЗАЦИЯ: join вместо конкатенации в цикле
    lines = ["--- История сессии ---"]
    for entry in conversation_history[-MAX_HISTORY:]:
        role = "Пользователь" if entry["role"] == "user" else "Jarvis"
        lines.append(f"{role}: {entry['text']}")
    if opened_by_jarvis:
        lines.append("--- Открыто Jarvis'ом ---")
        for item in opened_by_jarvis:
            lines.append(f"- {item['type']}: {item['name']}")
    lines.append("---")
    return "\n" + "\n".join(lines) + "\n"


def detect_voice(text):
    """Определяет голос по языку текста."""
    if any('\u0530' <= c <= '\u058F' for c in text): return VOICE_MAP["hy"]
    if any('\u4e00' <= c <= '\u9fff' for c in text): return VOICE_MAP["zh"]
    if any('\u3040' <= c <= '\u30ff' for c in text): return VOICE_MAP["ja"]
    if any('\u0600' <= c <= '\u06ff' for c in text): return VOICE_MAP["ar"]
    if any('а' <= c.lower() <= 'я' for c in text):   return VOICE_MAP["ru"]
    return VOICE_MAP["en"]


def find_app_exe(app_name):
    """Ищет .exe приложения в реестре и популярных папках установки."""
    import winreg
    name_lower = app_name.lower().strip()

    # Словарь известных приложений → имя exe
    known_apps = {
        "telegram": "Telegram.exe",
        "discord": "Discord.exe",
        "spotify": "Spotify.exe",
        "steam": "Steam.exe",
        "vscode": "Code.exe",
        "vs code": "Code.exe",
        "visual studio code": "Code.exe",
        "notepad++": "notepad++.exe",
        "блокнот": "notepad.exe",
        "notepad": "notepad.exe",
        "калькулятор": "calc.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "пэйнт": "mspaint.exe",
        "word": "WINWORD.EXE",
        "excel": "EXCEL.EXE",
        "powerpoint": "POWERPNT.EXE",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "edge": "msedge.exe",
        "проводник": "explorer.exe",
        "explorer": "explorer.exe",
        "taskmgr": "taskmgr.exe",
        "диспетчер задач": "taskmgr.exe",
    }
    for key, exe in known_apps.items():
        if key in name_lower:
            return exe, None  # (exe_name, full_path)

    # Папки где обычно стоят приложения
    search_dirs = [
        os.path.expandvars(r"%LOCALAPPDATA%"),
        os.path.expandvars(r"%APPDATA%"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
    ]
    # Ключевые слова для поиска папки
    words = [w for w in name_lower.split() if len(w) > 2]

    for base in search_dirs:
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.scandir(base):
                if not entry.is_dir():
                    continue
                dir_lower = entry.name.lower()
                if any(w in dir_lower for w in words):
                    # Ищем .exe внутри
                    for root, dirs, files in os.walk(entry.path):
                        for f in files:
                            if f.lower().endswith(".exe"):
                                f_lower = f.lower().replace(".exe", "")
                                if any(w in f_lower for w in words):
                                    return f, os.path.join(root, f)
                        break  # только верхний уровень папки
        except Exception:
            continue

    # Поиск в реестре (uninstall keys)
    reg_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
    ]
    for reg_path in reg_paths:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                sub_name = winreg.EnumKey(key, i)
                if any(w in sub_name.lower() for w in words):
                    sub = winreg.OpenKey(key, sub_name)
                    try:
                        path, _ = winreg.QueryValueEx(sub, "")
                        if path and os.path.exists(path):
                            return sub_name, path
                    except Exception:
                        pass
        except Exception:
            continue

    return None, None


def open_app_safe(app_name):
    name_lower = app_name.lower().strip()

    # Системные приложения — всегда в PATH
    system_map = {
        "проводник": "explorer.exe",
        "этот компьютер": "explorer.exe",
        "my computer": "explorer.exe",
        "блокнот": "notepad.exe",
        "калькулятор": "calc.exe",
        "диспетчер задач": "taskmgr.exe",
        "paint": "mspaint.exe",
        "пэйнт": "mspaint.exe",
    }
    for key, exe in system_map.items():
        if key in name_lower:
            try:
                subprocess.Popen(exe, shell=True)
                return True
            except Exception:
                pass

    # Прямые известные пути для популярных приложений
    LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
    APPDATA = os.environ.get("APPDATA", "")
    direct_paths = {
        "telegram": [
            os.path.join(APPDATA, "Telegram Desktop", "Telegram.exe"),
            os.path.join(LOCALAPPDATA, "Telegram Desktop", "Telegram.exe"),
            os.path.join(LOCALAPPDATA, "Programs", "Telegram Desktop", "Telegram.exe"),
            r"C:\Program Files\Telegram Desktop\Telegram.exe",
        ],
        "discord": [],  # заполняется через glob ниже
        "spotify": [
            os.path.join(APPDATA, "Spotify", "Spotify.exe"),
            os.path.join(LOCALAPPDATA, "Microsoft", "WindowsApps", "Spotify.exe"),
        ],
        "steam": [
            r"C:\Program Files (x86)\Steam\Steam.exe",
            r"C:\Program Files\Steam\Steam.exe",
        ],
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe"),
        ],
        "firefox": [
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
        ],
        "edge": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
        "vscode": [
            os.path.join(LOCALAPPDATA, "Programs", "Microsoft VS Code", "Code.exe"),
            r"C:\Program Files\Microsoft VS Code\Code.exe",
        ],
        "vs code": [
            os.path.join(LOCALAPPDATA, "Programs", "Microsoft VS Code", "Code.exe"),
        ],
        "obs": [
            r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
            r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
        ],
        "vlc": [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ],
    }

    # Discord — ищем через glob т.к. папка содержит версию
    import glob
    discord_patterns = [
        os.path.join(LOCALAPPDATA, "Discord", "app-*", "Discord.exe"),
        os.path.join(APPDATA, "Discord", "app-*", "Discord.exe"),
    ]
    discord_paths = []
    for pat in discord_patterns:
        discord_paths.extend(sorted(glob.glob(pat), reverse=True))  # последняя версия первой
    direct_paths["discord"] = discord_paths

    DETACHED = 0x00000008  # DETACHED_PROCESS — нет унаследованного терминала
    CREATE_NO_WINDOW = 0x08000000
    for key, paths in direct_paths.items():
        if key in name_lower:
            for p in paths:
                if os.path.exists(p):
                    try:
                        # Chrome: добавляем CDP-порт для точного управления вкладками
                        args = [p]
                        if key == "chrome":
                            args += [f"--remote-debugging-port={_CDP_PORT}", "--no-first-run"]
                        subprocess.Popen(
                            args,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=DETACHED | CREATE_NO_WINDOW,
                        )
                        print(f"[✓] Запущено: {p}")
                        if key == "chrome":
                            # Ждём пока CDP заработает (до 3 сек)
                            for _ in range(30):
                                time.sleep(0.1)
                                if _cdp_ping():
                                    print("[CDP] ✓ Chrome с CDP готов")
                                    break
                        return True
                    except Exception as e:
                        print(f"[!] Ошибка запуска {p}: {e}")
            break

    # Поиск через find_app_exe (реестр + папки)
    exe_name, full_path = find_app_exe(app_name)
    if full_path and os.path.exists(full_path):
        try:
            subprocess.Popen([full_path])
            print(f"[✓] Запущено через поиск: {full_path}")
            return True
        except Exception:
            pass

    # AppOpener как последний вариант
    try:
        open_app(app_name, match_closest=True, throw_error=True)
        return True
    except Exception:
        pass

    print(f"[!] Не удалось найти приложение: {app_name}")
    return False




# ── WinAPI keyboard injection (Ctrl+W без pyautogui) ─────────────────
# Структуры для SendInput — стандартный WinAPI способ инжекта клавиш.
# Работает даже если SetForegroundWindow вернул False (Windows 10 focus lock).
_VK_CONTROL      = 0x11
_VK_W            = 0x57
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD  = 1

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         wintypes.WORD),
        ("wScan",       wintypes.WORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 28)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("_u", _INPUT_UNION)]

def _winapi_send_ctrl_w():
    """
    Инжектирует Ctrl+W через SendInput (нижний уровень WinAPI).
    НЕ использует pyautogui. Работает для Chrome / Edge / Opera / Firefox / Brave.
    """
    seq = [
        (_VK_CONTROL, 0),
        (_VK_W,       0),
        (_VK_W,       _KEYEVENTF_KEYUP),
        (_VK_CONTROL, _KEYEVENTF_KEYUP),
    ]
    inputs = (_INPUT * len(seq))()
    for i, (vk, flags) in enumerate(seq):
        inputs[i].type        = _INPUT_KEYBOARD
        inputs[i]._u.ki.wVk   = vk
        inputs[i]._u.ki.dwFlags = flags
    ctypes.windll.user32.SendInput(len(seq), inputs, ctypes.sizeof(_INPUT))


# ═══════════════════════════════════════════════════════════════════════════
#  CHROME DEVTOOLS PROTOCOL (CDP)
#  Точное управление вкладками Chrome без Ctrl+W «наугад».
#
#  Требования: Chrome должен быть запущен с флагом:
#      chrome.exe --remote-debugging-port=9222
#  Jarvis автоматически добавляет этот флаг когда открывает Chrome сам.
#  Если Chrome уже запущен без CDP — используется WinAPI-fallback.
# ═══════════════════════════════════════════════════════════════════════════

_CDP_PORT = 9222

def _cdp_ping() -> bool:
    """True если Chrome слушает CDP на порту 9222."""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", _CDP_PORT), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def _cdp_request(path: str):
    """
    GET-запрос к CDP REST API.
    Возвращает распакованный dict/list, строку, True (пустое тело), или None при ошибке.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{_CDP_PORT}{path}", timeout=2
        ) as resp:
            raw = resp.read()
            if not raw:
                return True
            try:
                return json.loads(raw)
            except Exception:
                return raw.decode(errors="replace")
    except Exception as e:
        print(f"[CDP] {path}: {e}")
        return None


def _cdp_open_tab(url: str) -> str | None:
    """
    Открывает URL в новой вкладке Chrome через CDP.
    Возвращает tab_id (str) или None если CDP недоступен / ошибка.
    """
    from urllib.parse import quote
    data = _cdp_request(f"/json/new?{quote(url, safe=':/?=&%#+@')}")
    if isinstance(data, dict) and "id" in data:
        print(f"[CDP] ✓ Открыта вкладка {data['id'][:8]}… → {url}")
        return data["id"]
    print(f"[CDP] open_tab вернул: {data!r}")
    return None


def _cdp_close_tab(tab_id: str) -> bool:
    """Закрывает вкладку Chrome по tab_id. Возвращает True при успехе."""
    result = _cdp_request(f"/json/close/{tab_id}")
    ok = result is not None
    if ok:
        print(f"[CDP] ✓ Закрыта вкладка {tab_id[:8]}…")
    return ok


def _cdp_list_tabs(search_term: str | None = None) -> list:
    """
    Возвращает список живых вкладок Chrome (type=page).
    Если search_term задан — фильтрует по title и url.
    """
    tabs = _cdp_request("/json") or []
    pages = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"]
    if not search_term:
        return pages
    st = search_term.lower()
    return [t for t in pages
            if st in t.get("title", "").lower() or st in t.get("url", "").lower()]


def _open_browser_url(url: str, name: str) -> dict:
    """
    Открывает URL и возвращает запись для opened_by_jarvis.
    CDP-путь: открывает точно в Chrome, сохраняет tab_id → закрытие будет точным.
    Fallback: webbrowser.open() если CDP недоступен.
    """
    tab_id = None
    if _cdp_ping():
        tab_id = _cdp_open_tab(url)
    if tab_id is None:
        webbrowser.open(url)
    return {"type": "browser", "name": name, "url": url, "tab_id": tab_id}


def _close_tab_cdp(search_term: str | None, mode: str) -> bool:
    """Закрывает вкладки через CDP (точный метод)."""
    # ── Чистим "мёртвые" записи ──────────────────────────────────────────
    # Если пользователь вручную закрыл вкладку, которую раньше открыл
    # Jarvis (просто крестиком в браузере), запись об этой вкладке
    # оставалась в opened_by_jarvis навсегда — Jarvis об этом не узнавал.
    # Из-за этого "smart"-режим мог намертво зависнуть на одной такой
    # записи (см. ниже) и список постепенно копил мусор. Сверяем со
    # списком РЕАЛЬНО живых вкладок (без фильтра по search_term, чтобы
    # не спутать "не подошло под поиск" с "вкладки больше не существует")
    # и вычищаем всё, чего там больше нет.
    all_live_ids = {t["id"] for t in _cdp_list_tabs()}
    with _jarvis_lock:
        for entry in list(opened_by_jarvis):
            if (entry.get("type") == "browser" and entry.get("tab_id")
                    and entry["tab_id"] not in all_live_ids):
                try: opened_by_jarvis.remove(entry)
                except Exception: pass

    with _jarvis_lock:
        jarvis_tabs = [
            item for item in opened_by_jarvis
            if item.get("type") == "browser"
               and item.get("tab_id")
               and (not search_term
                    or search_term in item.get("name", "").lower()
                    or search_term in item.get("url",  "").lower())
        ]

    live_tabs = _cdp_list_tabs(search_term)

    if mode == "smart":
        # Приоритет: вкладка открытая Jarvis'ом → закрываем по сохранённому tab_id.
        # Пробуем от самой недавней записи к самой старой — после чистки
        # выше все они должны быть живыми, но на случай гонки (вкладку
        # закрыли между чисткой и этим моментом) не сдаёмся на первой же
        # неудаче, а пробуем следующую.
        for entry in reversed(jarvis_tabs):
            ok = _cdp_close_tab(entry["tab_id"])
            with _jarvis_lock:
                try: opened_by_jarvis.remove(entry)
                except Exception: pass
            if ok:
                return True
        # Нет (живой) записи → ищем по title/url среди живых вкладок
        if live_tabs:
            return _cdp_close_tab(live_tabs[-1]["id"])
        return False

    elif mode == "all":
        if not live_tabs: return False
        closed_ids = {t["id"] for t in live_tabs if _cdp_close_tab(t["id"])}
        n = len(closed_ids)
        with _jarvis_lock:
            for entry in list(opened_by_jarvis):
                # Убираем из bookkeeping только те записи, чьи вкладки
                # реально были закрыты только что (по id), а не все, что
                # просто совпали по имени.
                if entry.get("type") == "browser" and entry.get("tab_id") in closed_ids:
                    try: opened_by_jarvis.remove(entry)
                    except Exception: pass
        print(f"[CDP] Закрыто вкладок: {n}")
        return n > 0

    elif mode == "all_jarvis":
        if not jarvis_tabs: return False
        n = 0
        for entry in list(jarvis_tabs):
            if _cdp_close_tab(entry["tab_id"]):
                n += 1
            # Удаляем запись в любом случае: либо успешно закрыли, либо
            # она уже была мёртвой (не смысла хранить дальше) — реальные
            # ошибки сети здесь маловероятны, т.к. CDP только что отвечал.
            with _jarvis_lock:
                try: opened_by_jarvis.remove(entry)
                except Exception: pass
        return n > 0

    elif mode == "all_user":
        with _jarvis_lock:
            jarvis_ids = {e.get("tab_id") for e in jarvis_tabs}
        user_tabs = [t for t in live_tabs if t.get("id") not in jarvis_ids]
        return sum(1 for t in user_tabs if _cdp_close_tab(t["id"])) > 0

    return False


def close_browser_tab(site_name: str | None = None, mode: str = "smart") -> bool:
    """
    Закрывает вкладку(и) браузера.
    • CDP-режим  (Chrome с --remote-debugging-port=9222): точное закрытие по tab_id
    • WinAPI-fallback (Chrome без CDP): SetForegroundWindow + Ctrl+W
    """
    search_term = site_name.lower().strip() if site_name else None
    aliases = {
        "ютуб": "youtube", "гугл": "google", "вк": "vk",
        "твич": "twitch",  "твиттер": "twitter", "тикток": "tiktok",
        "инстаграм": "instagram", "фейсбук": "facebook",
        "нетфликс": "netflix",    "спотифай": "spotify",
    }
    if search_term in aliases:
        search_term = aliases[search_term]

    if _cdp_ping():
        print(f"[CDP] Закрываю: '{search_term}' mode={mode}")
        return _close_tab_cdp(search_term, mode)

    print(f"[CDP] недоступен → WinAPI fallback: '{search_term}'")
    return _close_tab_winapi(search_term, mode)


def _close_tab_winapi(search_term: str | None, mode: str) -> bool:
    """WinAPI + Ctrl+W fallback (закрывает активную вкладку в найденном окне)."""
    _BROWSER_EXE         = ['chrome', 'firefox', 'msedge', 'opera', 'brave', 'yandex']
    _BROWSER_TITLE_HINTS = ['chrome', 'firefox', 'edge',   'opera', 'brave', 'yandex']

    def _is_browser_hwnd(hwnd):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if any(b in psutil.Process(pid).name().lower() for b in _BROWSER_EXE):
                return True
        except Exception:
            pass
        return any(b in win32gui.GetWindowText(hwnd).lower() for b in _BROWSER_TITLE_HINTS)

    def get_matching_windows():
        result = []
        def _enum(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd): return
            if not _is_browser_hwnd(hwnd): return
            title = win32gui.GetWindowText(hwnd).lower()
            if search_term is None or search_term in title:
                result.append(hwnd)
        win32gui.EnumWindows(_enum, None)
        return result

    def bring_and_close(hwnd):
        try:
            if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMINIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)
            _tid_t, _ = win32process.GetWindowThreadProcessId(hwnd)
            _tid_s = ctypes.windll.kernel32.GetCurrentThreadId()
            ctypes.windll.user32.AttachThreadInput(_tid_s, _tid_t, True)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.user32.BringWindowToTop(hwnd)
            ctypes.windll.user32.AttachThreadInput(_tid_s, _tid_t, False)
            for _ in range(8):
                if ctypes.windll.user32.GetForegroundWindow() == hwnd: break
                time.sleep(0.05)
            _winapi_send_ctrl_w()
            time.sleep(0.25)
            return True
        except Exception as e:
            print(f"[WinAPI] bring_and_close: {e}")
            return False

    with _jarvis_lock:
        jarvis_tabs = [item for item in opened_by_jarvis
                       if item['type'] == 'browser' and
                       (not search_term or search_term in item['name'].lower())]

    if mode == "smart":
        windows = get_matching_windows()
        if windows:
            hwnd = windows[-1]
            # Запоминаем заголовок ДО закрытия — по нему сверяем, какую
            # именно запись из opened_by_jarvis это было (если вообще было).
            title_before = win32gui.GetWindowText(hwnd).lower()
            ok = bring_and_close(hwnd)
            if ok and jarvis_tabs:
                # Раньше здесь вслепую удаляли jarvis_tabs[-1], даже если
                # закрытое окно на самом деле не имело к нему отношения —
                # учёт "что ещё открыл Jarvis" мог разойтись с реальностью.
                # Теперь удаляем только ту запись, чьё имя действительно
                # входит в заголовок закрытого окна.
                match = next((t for t in jarvis_tabs
                              if t.get("name", "").lower() in title_before), None)
                if match:
                    with _jarvis_lock:
                        try: opened_by_jarvis.remove(match)
                        except Exception: pass
            return ok
        if search_term:
            all_wins = []
            def _ea(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd): return
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if any(b in psutil.Process(pid).name().lower()
                           for b in _BROWSER_EXE):
                        all_wins.append(hwnd)
                except Exception: pass
            win32gui.EnumWindows(_ea, None)
            if all_wins: return bring_and_close(all_wins[-1])
        return False

    elif mode == "all":
        closed = 0
        for _ in range(30):
            w = get_matching_windows()
            if not w: break
            if bring_and_close(w[0]): closed += 1
            else: break
        with _jarvis_lock:
            for tab in list(jarvis_tabs):
                try: opened_by_jarvis.remove(tab)
                except Exception: pass
        return closed > 0

    elif mode == "all_jarvis":
        n = 0
        for _ in list(jarvis_tabs):
            w = get_matching_windows()
            if w and bring_and_close(w[0]):
                n += 1
                with _jarvis_lock:
                    try: opened_by_jarvis.remove(_)
                    except Exception: pass
        return n > 0

    elif mode == "all_user":
        jarvis_names = {t['name'].lower() for t in jarvis_tabs}
        n = 0
        for hwnd in get_matching_windows():
            title = win32gui.GetWindowText(hwnd).lower()
            if not any(jn in title for jn in jarvis_names):
                if bring_and_close(hwnd): n += 1
        return n > 0

    return False


def close_folder(folder_name):
    """Закрывает окна проводника с совпадающим именем папки."""
    name_clean = folder_name.strip().lower()
    for word in ["папку", "папка", "folder", "закрой", "закрыть", "открытую", "на рабочем столе"]:
        name_clean = name_clean.replace(word, "").strip()

    if not name_clean or name_clean in ["проводник", "explorer", ""]:
        script = "$shell = New-Object -ComObject Shell.Application; $shell.Windows() | ForEach-Object { $_.Quit() }"
    else:
        script = f"""
$shell = New-Object -ComObject Shell.Application
$shell.Windows() | Where-Object {{
    $_.LocationName -like '*{name_clean}*' -or
    $_.LocationURL -like '*{name_clean}*'
}} | ForEach-Object {{ $_.Quit() }}
"""
    result = subprocess.run(["powershell", "-Command", script], capture_output=True, text=True)
    print(f"[✓] close_folder '{name_clean}': выполнено")

    # ── Fuzzy fallback: если PowerShell ничего не закрыл ──────────────
    # Проверим открытые окна проводника и найдём похожее
    import difflib as _dfl
    _check = subprocess.run(
        ["powershell", "-Command",
         "$shell = New-Object -ComObject Shell.Application; $shell.Windows() | Select-Object -ExpandProperty LocationName"],
        capture_output=True, text=True)
    _open_folders = [ln.strip().lower() for ln in _check.stdout.splitlines() if ln.strip()]
    if _open_folders and name_clean:
        _fm = _dfl.get_close_matches(name_clean, _open_folders, n=1, cutoff=0.5)
        if _fm:
            _matched_name = _fm[0]
            print(f"[FUZZY] Закрываю папку похожую на '{name_clean}': '{_matched_name}'")
            _close_script = f"""
$shell = New-Object -ComObject Shell.Application
$shell.Windows() | Where-Object {{
    $_.LocationName -like '*{_matched_name}*'
}} | ForEach-Object {{ $_.Quit() }}
"""
            subprocess.run(["powershell", "-Command", _close_script],
                           capture_output=True, text=True)


def close_app_safe(app_name):
    name_lower = app_name.lower().strip()
    for word in ["закрой", "закрыть", "выключи", "выключить", "останови"]:
        name_lower = name_lower.replace(word, "").strip()

    print(f"[DEBUG] close_app_safe: '{name_lower}'")

    # Папки / проводник
    folder_keywords = ["папку", "папка", "folder", "проводник", "explorer"]
    if any(k in name_lower for k in folder_keywords):
        close_folder(name_lower)
        return True

    # Браузерные сайты — расширенный список
    browser_keywords = [
        "youtube", "ютуб", "yt",
        "вк", "vk", "vk.com", "вконтакте",
        "google", "гугл",
        "facebook", "фейсбук",
        "instagram", "инстаграм",
        "twitter", "твиттер", "x.com",
        "tiktok", "тикток",
        "twitch", "твич",
        "reddit",
        "github",
        "netflix", "нетфликс",
        "spotify", "спотифай",
        "вкладку", "вкладка", "сайт", "браузер", "таб", "tab",
    ]
    if any(k in name_lower for k in browser_keywords):
        for word in ["вкладку", "вкладка", "сайт", "браузер", "таб", "tab"]:
            name_lower = name_lower.replace(word, "").strip()
        # ← ИСПРАВЛЕНО: возвращаем реальный результат (True/False),
        #   а не всегда True — чтобы Jarvis мог сказать "не удалось".
        return close_browser_tab(name_lower.strip(), mode="smart")

    # Словарь популярных приложений → все возможные имена exe
    app_exe_map = {
        "telegram": ["Telegram.exe"],
        "discord": ["Discord.exe"],
        "spotify": ["Spotify.exe"],
        "steam": ["Steam.exe"],
        "chrome": ["chrome.exe"],
        "firefox": ["firefox.exe"],
        "edge": ["msedge.exe"],
        "vscode": ["Code.exe"],
        "vs code": ["Code.exe"],
        "visual studio code": ["Code.exe"],
        "notepad++": ["notepad++.exe"],
        "блокнот": ["notepad.exe"],
        "paint": ["mspaint.exe"],
        "пэйнт": ["mspaint.exe"],
        "word": ["WINWORD.EXE"],
        "excel": ["EXCEL.EXE"],
        "powerpoint": ["POWERPNT.EXE"],
        "диспетчер задач": ["Taskmgr.exe"],
        "taskmgr": ["Taskmgr.exe"],
        "obs": ["obs64.exe", "obs32.exe"],
        "vlc": ["vlc.exe"],
        "проводник": ["explorer.exe"],
    }
    for key, exes in app_exe_map.items():
        if key in name_lower:
            for exe in exes:
                r = subprocess.run(["taskkill", "/f", "/im", exe],
                                   capture_output=True, text=True)
                print(f"[DEBUG] taskkill {exe}: rc={r.returncode} err={r.stderr.strip()[:60]}")
                if r.returncode == 0:
                    print(f"[✓] Закрыто: {exe}")
                    return True
            break  # нашли ключ, больше не ищем

    # Поиск среди запущенных процессов по частичному совпадению
    words = [w for w in name_lower.split() if len(w) > 2]
    best_match = None
    best_score = 0
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            proc_name = proc.info['name']
            proc_clean = proc_name.lower().replace(".exe", "")
            if name_lower == proc_clean or name_lower == proc_name.lower():
                best_match = proc_name
                best_score = 999
                break
            for w in words:
                if w in proc_clean:
                    score = len(w)
                    if score > best_score:
                        best_score = score
                        best_match = proc_name
        except Exception:
            continue

    if best_match and best_score >= 3:
        r = subprocess.run(["taskkill", "/f", "/im", best_match],
                           capture_output=True, text=True)
        print(f"[DEBUG] taskkill {best_match}: rc={r.returncode}")
        if r.returncode == 0:
            print(f"[✓] Закрыто процессом: {best_match}")
            return True

    # ── Fuzzy fallback: difflib по именам запущенных процессов ────────
    import difflib as _dl
    _all_procs = []
    for _p in psutil.process_iter(['name']):
        try:
            _all_procs.append(_p.info['name'])
        except Exception: pass
    _all_clean = [_n.lower().replace(".exe","") for _n in _all_procs]
    # Ищем наиболее похожее имя (порог 0.55 — достаточно чтобы "проекты" нашло "projects")
    _matches = _dl.get_close_matches(name_lower, _all_clean, n=1, cutoff=0.55)
    if _matches:
        _idx = _all_clean.index(_matches[0])
        _best_exe = _all_procs[_idx]
        r = subprocess.run(["taskkill", "/f", "/im", _best_exe],
                           capture_output=True, text=True)
        print(f"[FUZZY] taskkill {_best_exe} (похоже на '{name_lower}'): rc={r.returncode}")
        if r.returncode == 0:
            print(f"[✓] Закрыто (fuzzy): {_best_exe}")
            return True

    print(f"[!] Процесс не найден: {name_lower}")
    return False


# --- ПЕРЕВОД ---

def process_translator(text):
    global translator_target_lang, translator_voice
    if not translator_target_lang:
        return None

    client = _get_client()
    if client is None:
        print("[!] TRANSLATOR: API-ключ не задан. Укажите ключ в настройках.")
        return {"content": "Сэр, укажите API-ключ в настройках для работы переводчика.", "voice": VOICE_RUSSIAN}

    for attempt in range(3):
        try:
            prompt = f"Translate to {translator_target_lang}. Output ONLY translated text: {text}"
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config={'candidate_count': 1}
            )
            if response and response.text:
                _record_token_usage(response)
                translated = response.text.strip()
                print(f"[Перевод → {translator_target_lang}]: {translated}")
                return {"content": translated, "voice": translator_voice}
        except Exception as e:
            if "503" in str(e) and attempt < 2:
                time.sleep(0.5)
                continue
            print(f"[!] TRANSLATOR ERROR: {e}")
    return None


# --- ОСНОВНАЯ ЛОГИКА ---

import ast
import operator as op_module

def try_calc_math(text):
    """Пытается вычислить математику локально. Возвращает строку-ответ или None."""
    # Ищем паттерн: "сколько будет X", "X = ?", просто "X + Y" и т.д.
    import re
    # Убираем слова-вопросы на русском
    clean = text.lower()
    for phrase in ["сколько будет", "сколько равно", "посчитай", "вычисли",
                   "сколько это", "результат", "ответ", "что такое", "чему равно"]:
        clean = clean.replace(phrase, "")
    clean = clean.strip().rstrip("?").strip()
    # Заменяем русские слова операций
    clean = clean.replace("плюс", "+").replace("минус", "-") \
                 .replace("умножить на", "*").replace("умножить", "*") \
                 .replace("разделить на", "/").replace("разделить", "/") \
                 .replace("в степени", "**").replace("х", "*").replace("икс", "*")
    # Оставляем только цифры и операторы
    expr = re.sub(r'[^0-9+\-*/().% ]', '', clean).strip()
    if not expr:
        return None
    try:
        # Безопасное вычисление через ast
        allowed_ops = {
            ast.Add: op_module.add, ast.Sub: op_module.sub,
            ast.Mult: op_module.mul, ast.Div: op_module.truediv,
            ast.Pow: op_module.pow, ast.Mod: op_module.mod,
            ast.USub: op_module.neg
        }
        def safe_eval(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            elif isinstance(node, ast.BinOp) and type(node.op) in allowed_ops:
                return allowed_ops[type(node.op)](safe_eval(node.left), safe_eval(node.right))
            elif isinstance(node, ast.UnaryOp) and type(node.op) in allowed_ops:
                return allowed_ops[type(node.op)](safe_eval(node.operand))
            else:
                raise ValueError("unsupported")
        result = safe_eval(ast.parse(expr, mode='eval').body)
        # Красивый вывод: убираем .0 у целых
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return str(result)
    except Exception:
        return None


def _handle_catalog_request(original_text: str) -> dict:
    """
    Отвечает на запрос «что ты умеешь / каталог возможностей» локально,
    без отправки в Gemini → ИИ не выполняет теги из своего ответа.
    В конце предлагает сохранить .txt на рабочем столе.
    """
    has_cyrillic = any('а' <= c.lower() <= 'я' for c in original_text)
    _live_cfg = _load_jarvis_config()
    _ai_lang = _live_cfg.get("ai_language", _CFG.get("ai_language", "Русский"))
    use_ru = has_cyrillic or _ai_lang == "Русский"

    if use_ru:
        catalog = (
            "Вот полный список моих возможностей, сэр:\n\n"
            "ОТКРЫТИЕ:\n"
            "  • Открыть любой веб-сайт\n"
            "  • Открыть YouTube (с поиском или главную)\n"
            "  • Открыть Rutube (с поиском или главную)\n"
            "  • Поиск в Google\n"
            "  • Запустить приложение\n"
            "  • Открыть файл или папку\n\n"
            "УПРАВЛЕНИЕ ОКНАМИ:\n"
            "  • Закрыть приложение, вкладку или папку\n"
            "  • Закрыть все вкладки сайта\n"
            "  • Закрыть только то, что я открывал\n"
            "  • Закрыть только то, что открыли вы\n\n"
            "ПЕРЕВОД:\n"
            "  • Режим перевода (в динамики, голосом Jarvis)\n"
            "  • Реалтайм перевод в виртуальный кабель (для стрима)\n"
            "  • Выключить перевод\n\n"
            "МАТЕМАТИКА:\n"
            "  • Мгновенный счёт без интернета: плюс, минус, умножить, разделить, степень\n\n"
            "ТАЙМЕРЫ И ЗАМЕТКИ:\n"
            "  • Установить таймер на любое время\n"
            "  • Отменить таймер\n"
            "  • Добавить заметку с напоминанием\n"
            "  • Показать список заметок\n\n"
            "БУФЕР ОБМЕНА:\n"
            "  • Прочитать буфер обмена\n"
            "  • Перевести текст из буфера\n"
            "  • Кратко объяснить скопированный текст\n\n"
            "ЭКРАН:\n"
            "  • Сделать скриншот и описать что на экране (Gemini Vision)\n\n"
            "ГРОМКОСТЬ:\n"
            "  • Громче / тише / mute\n"
            "  • Установить громкость на N процентов\n\n"
            "КАСТОМНЫЕ КОМАНДЫ:\n"
            "  • Ваши собственные команды — настраиваются в разделе Команды\n\n"
            "СИСТЕМА:\n"
            "  • Выключить Jarvis\n"
            "  • Помнит историю текущей сессии\n\n"
            "Хотите, чтобы я сохранил этот список в файл jarvis_help.txt на рабочем столе?"
        )
        voice = VOICE_RUSSIAN
    else:
        catalog = (
            "Here is my full list of capabilities, sir:\n\n"
            "OPEN:\n"
            "  • Open any website\n"
            "  • Open YouTube (search or homepage)\n"
            "  • Open Rutube (search or homepage)\n"
            "  • Google search\n"
            "  • Launch any app\n"
            "  • Open file or folder\n\n"
            "WINDOW CONTROL:\n"
            "  • Close app, tab, or folder\n"
            "  • Close all tabs of a site\n"
            "  • Close only what I opened\n"
            "  • Close only what you opened\n\n"
            "TRANSLATION:\n"
            "  • Translation mode (speakers, Jarvis voice)\n"
            "  • Real-time translation to virtual cable (for streaming)\n"
            "  • Turn off translation\n\n"
            "MATH:\n"
            "  • Instant offline math: plus, minus, multiply, divide, power\n\n"
            "TIMERS & NOTES:\n"
            "  • Set a timer for any duration\n"
            "  • Cancel a timer\n"
            "  • Add a note with a reminder\n"
            "  • Show notes list\n\n"
            "CLIPBOARD:\n"
            "  • Read clipboard text\n"
            "  • Translate clipboard text\n"
            "  • Summarize copied text\n\n"
            "SCREEN:\n"
            "  • Screenshot and describe what's on screen (Gemini Vision)\n\n"
            "VOLUME:\n"
            "  • Louder / quieter / mute\n"
            "  • Set volume to N percent\n\n"
            "CUSTOM COMMANDS:\n"
            "  • Your own commands — configured in the Commands tab\n\n"
            "SYSTEM:\n"
            "  • Shut down Jarvis\n"
            "  • Remembers current session history\n\n"
            "Would you like me to save this list to jarvis_help.txt on the Desktop?"
        )
        voice = VOICE_JARVIS

    print(f"[Jarvis]: {catalog[:200]}...")
    jarvis_ui.add_log("jarvis", catalog)
    conversation_history.append({"role": "jarvis", "text": catalog})

    # Регистрируем ожидание да/нет через общий стек updater'а (см. updater.py) —
    # раньше это был отдельный флаг _pending_catalog_save, который мог
    # конфликтовать с подтверждением обновления, если оба вопроса оказывались
    # активны одновременно.
    def _on_save_agreed():
        global recording, conversation_mode
        try:
            _desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            _txt_path = os.path.join(_desktop, "jarvis_help.txt")
            with open(_txt_path, "w", encoding="utf-8") as _f:
                _f.write(catalog)
            _save_msg = "Готово, сэр. Файл jarvis_help.txt сохранён на рабочем столе."
            print(f"[Jarvis]: {_save_msg}")
        except Exception as _e:
            _save_msg = f"Сэр, не удалось сохранить файл: {_e}"
            print(f"[!] catalog save error: {_e}")
        jarvis_ui.add_log("jarvis", _save_msg)
        _speak(_save_msg, VOICE_RUSSIAN)
        recording = False
        conversation_mode = False
        jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
        print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")

    def _on_save_declined():
        global recording, conversation_mode
        _no_msg = "Хорошо, сэр. Если понадоблюсь — я здесь."
        jarvis_ui.add_log("jarvis", _no_msg)
        _speak(_no_msg, VOICE_RUSSIAN)
        recording = False
        conversation_mode = False
        jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
        print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")

    def _on_save_ignored():
        # Пользователь не ответил "да"/"нет", а сказал что-то другое —
        # молча снимаем вопрос, БЕЗ голосового "хорошо, я здесь" и без
        # сброса recording/conversation_mode: его фраза сейчас пойдёт в
        # обычную обработку ниже и сама решит, что делать с этими флагами.
        print("[CATALOG] Вопрос о сохранении проигнорирован — пользователь сменил тему.")

    updater.set_pending_confirm(
        on_yes=_on_save_agreed,
        on_no=_on_save_declined,
        on_ignore=_on_save_ignored,
        keywords_yes={"да", "конечно", "сохрани", "сохраняй", "сохрани пожалуйста",
                      "yes", "sure", "save", "please save", "ok", "ок", "окей",
                      "хочу", "хорошо", "давай"},
        keywords_no={"нет", "не надо", "не нужно", "отмена", "отменить",
                     "no", "don't", "cancel", "skip", "пропусти"},
    )
    return {"content": catalog, "voice": voice}


def process_logic(text):
    global conversation_history, opened_by_jarvis, recording, _last_opened
    global translator_mode, translator_target_lang, translator_voice, realtime_translation_mode
    global _pending_catalog_save

    try:
        current_time = datetime.now(pytz.timezone("Asia/Yerevan")).strftime("%H:%M")
        time_context = (
            f"Время в Ереване: {current_time} (UTC+4). "
            "Москва UTC+3, Лондон UTC+1, Дубай UTC+4, НьюЙорк UTC-4, ЛА UTC-7, Токио UTC+9."
        )

        conversation_history.append({"role": "user", "text": text})
        session_ctx = get_session_context()
        live_status_ctx = get_live_status_context()

        # --- Локальный перехват: запрос каталога возможностей ---
        # Обрабатываем локально чтобы ИИ не выполнял теги из своего же ответа
        _CATALOG_KEYWORDS = [
            "каталог", "список возможностей", "что ты умеешь", "что умеешь",
            "что ты можешь", "что можешь", "список функций", "твои функции",
            "твои возможности", "полный список", "покажи возможности",
            "what can you do", "list of features", "capabilities", "help list",
            "show capabilities", "what do you do",
        ]
        _t_low = text.lower()
        if any(kw in _t_low for kw in _CATALOG_KEYWORDS):
            return _handle_catalog_request(text)

        # --- Локальный калькулятор: если вопрос математический — считаем сами ---
        math_keywords = ["сколько будет", "сколько равно", "посчитай", "вычисли",
                         "плюс", "минус", "умножить", "разделить", "в степени"]
        looks_like_math = any(kw in text.lower() for kw in math_keywords) or \
                          bool(__import__('re').search(r'\d+\s*[+\-*/]\s*\d+', text))
        if looks_like_math:
            calc_result = try_calc_math(text)
            if calc_result is not None:
                answer_text = calc_result
                print(f"[Jarvis]: {answer_text}")
                jarvis_ui.add_log("jarvis", answer_text)
                conversation_history.append({"role": "jarvis", "text": answer_text})
                return {"content": answer_text, "voice": VOICE_RUSSIAN}

        # Определяем язык и явно указываем его прямо В ТЕКСТЕ запроса
        has_cyrillic = any('а' <= c.lower() <= 'я' for c in text)
        has_armenian = any('԰' <= c <= '֏' for c in text)

        # AI language fallback from jarvis_config.json (уже загружен в _CFG, не читаем диск)
        _ai_lang_cfg = _load_jarvis_config().get("ai_language", _CFG.get("ai_language", "Русский"))
        _FALLBACK_MAP = {
            "Русский":  ("ИНСТРУКЦИЯ: твой ответ должен быть ТОЛЬКО на русском языке.", "[Ответь на русском] "),
            "English":  ("INSTRUCTION: your response must be in English only.", "[Reply in English] "),
        }
        _fb_instr, _fb_prefix = _FALLBACK_MAP.get(_ai_lang_cfg, _FALLBACK_MAP["Русский"])

        if has_armenian:
            lang_instruction = "ИНСТРУКЦИЯ: твой ответ должен быть ТОЛЬКО на армянском языке."
            user_msg = f"[Ответь на армянском] {text}"
        elif has_cyrillic:
            lang_instruction = "ИНСТРУКЦИЯ: твой ответ должен быть ТОЛЬКО на русском языке. Ни одного слова по-английски."
            user_msg = f"[Ответь на русском] {text}"
        else:
            # Latin / unknown script — honour configured AI language
            lang_instruction = _fb_instr
            user_msg = _fb_prefix + text

        response = None
        for attempt in range(3):
            try:
                client = _get_client()
                if client is None:
                    return {"content": "Сэр, API-ключ не задан. Укажите ключ Gemini в настройках.", "voice": VOICE_RUSSIAN}
                response = client.models.generate_content(
                    model=MODEL_ID,
                    contents=f"{SYSTEM_PROMPT}\n{time_context}\n{lang_instruction}\n{live_status_ctx}\n{session_ctx}\nUser: {user_msg}"
                )
                break
            except Exception as e:
                if "503" in str(e) and attempt < 2:
                    time.sleep(2)
                    continue
                raise e

        if not response or not response.text:
            return {"content": "Сэр, ответ пуст.", "voice": VOICE_RUSSIAN}

        _record_token_usage(response)
        answer = response.text.replace("*", "").strip()
        clean_print = RE_PATTERNS['clean_tags'].sub('', answer).strip()
        print(f"[Jarvis]: {clean_print}")
        jarvis_ui.add_log("jarvis", clean_print)

        # ✅ ОПТИМИЗАЦИЯ: используем предкомпилированные паттерны
        p = RE_PATTERNS
        yt            = p["yt"].search(answer)
        rt_tube       = p["rt_tube"].search(answer)
        srch          = p["srch"].search(answer)
        url           = p["url"].search(answer)
        app           = p["app"].search(answer)
        path          = p["path"].search(answer)
        close         = p["close"].search(answer)
        close_all     = p["close_all"].search(answer)
        close_all_me  = p["close_all_me"].search(answer)
        close_all_usr = p["close_all_usr"].search(answer)
        shutdown      = p["shutdown"].search(answer)
        translator_on    = p["trans_on"].search(answer)
        translator_off   = p["trans_off"].search(answer)
        rt_translator_on = p["rt_trans_on"].search(answer)

        action_done = False

        if url and not action_done:
            _u = url.group(1)
            _name = _u.replace("https://", "").replace("www.", "").split("/")[0]
            _entry = _open_browser_url(_u, _name)
            with _jarvis_lock:
                opened_by_jarvis.append(_entry)
                _last_opened = {"type": "browser", "name": _name, "target": _name}
            action_done = True
        elif yt and not action_done:
            query = yt.group(1).strip()
            link  = f"https://www.youtube.com/results?search_query={query}" if query else "https://www.youtube.com"
            _name = f"youtube {query}".strip()
            _entry = _open_browser_url(link, _name)
            with _jarvis_lock:
                opened_by_jarvis.append(_entry)
                _last_opened = {"type": "browser", "name": _name, "target": "youtube"}
            action_done = True
        elif rt_tube and not action_done:
            query = rt_tube.group(1).strip()
            link  = f"https://rutube.ru/search/?query={query}" if query else "https://rutube.ru"
            _name = f"rutube {query}".strip()
            _entry = _open_browser_url(link, _name)
            with _jarvis_lock:
                opened_by_jarvis.append(_entry)
                _last_opened = {"type": "browser", "name": _name, "target": "rutube"}
            action_done = True
        elif srch and not action_done:
            link  = f"https://www.google.com/search?q={srch.group(1)}"
            _name = f"google {srch.group(1)}"
            _entry = _open_browser_url(link, _name)
            with _jarvis_lock:
                opened_by_jarvis.append(_entry)
                _last_opened = {"type": "browser", "name": _name, "target": "google"}
            action_done = True

        if app:
            _run_async(play_voice_async("Выполняю", VOICE_RUSSIAN, silent=True))
            open_app_safe(app.group(1))
            with _jarvis_lock:
                opened_by_jarvis.append({"type": "app", "name": app.group(1)})
                _last_opened = {"type": "app", "name": app.group(1), "target": app.group(1)}

        if path:
            p_val = path.group(1).replace("/", "\\")
            if os.path.exists(p_val):
                _run_async(play_voice_async("Выполняю", VOICE_RUSSIAN, silent=True))
                os.startfile(p_val)
                with _jarvis_lock:
                    opened_by_jarvis.append({"type": "folder", "name": p_val})
                    _last_opened = {"type": "folder", "name": p_val, "target": p_val}

        if close:
            name = close.group(1).strip()
            print(f"[DEBUG] CLOSE запрос: '{name}'")
            _run_async(play_voice_async("Выполняю", VOICE_RUSSIAN, silent=True))
            closed = close_app_safe(name)
            if not closed:
                close_browser_tab(site_name=name, mode="smart")
            print(f"[DEBUG] CLOSE результат: {closed}")

        if close_all:
            close_browser_tab(site_name=close_all.group(1).strip(), mode="all")

        if close_all_me:
            close_browser_tab(site_name=close_all_me.group(1).strip(), mode="all_jarvis")

        if close_all_usr:
            close_browser_tab(site_name=close_all_usr.group(1).strip(), mode="all_user")

        if shutdown:
            clean_answer = RE_PATTERNS["clean_tags"].sub('', answer).strip()
            return {"content": clean_answer or "До свидания, сэр.", "voice": VOICE_RUSSIAN, "shutdown": True}

        if rt_translator_on:
            lang_key = rt_translator_on.group(1).strip().lower()
            if lang_key in TRANSLATOR_LANGS:
                lang_name, voice = TRANSLATOR_LANGS[lang_key]
                realtime_translation_mode = True
                translator_mode = True
                translator_target_lang = lang_name
                translator_voice = voice
                recording = True
                print(f"\n[⚡] РЕЖИМ ПЕРЕВОДА АКТИВИРОВАН: {lang_name}")
                # Переключаем системный микрофон на CABLE Output в отдельном потоке
                # чтобы не блокировать основной поток ответа
                threading.Thread(target=switch_mic_to_virtual, daemon=True).start()
                _run_async(play_voice_async(
                    f"Режим перевода на {lang_name} включен, сэр. Я вас слушаю.",
                    VOICE_RUSSIAN, force_virtual=False
                ))
                return None

        elif translator_on:
            lang_key = translator_on.group(1).strip().lower()
            if lang_key in TRANSLATOR_LANGS:
                lang_name, voice = TRANSLATOR_LANGS[lang_key]
                translator_mode = True
                translator_target_lang = lang_name
                translator_voice = voice
                print(f"[✓] Переводчик включён: {lang_name}")
            else:
                translator_mode = True
                translator_target_lang = translator_on.group(1).strip()
                translator_voice = VOICE_JARVIS
                print(f"[✓] Переводчик включён: {translator_target_lang}")

        if translator_off:
            translator_mode = False
            realtime_translation_mode = False
            translator_target_lang = None
            translator_voice = None
            print("[✓] Переводчик выключен")
            threading.Thread(target=restore_mic, daemon=True).start()

        clean_answer = RE_PATTERNS["clean_tags"].sub('', answer).strip()

        conversation_history.append({"role": "jarvis", "text": clean_answer})
        # Обрезаем кратно 2, чтобы не разрывать пары user/jarvis
        max_entries = MAX_HISTORY * 2
        if len(conversation_history) > max_entries:
            conversation_history = conversation_history[-max_entries:]

        v = detect_voice(clean_answer)
        return {"content": clean_answer, "voice": v}

    except Exception as e:
        print(f"[!] ERROR: {e}")
        err_str = str(e)
        if "429" in err_str:
            return {"content": "Сэр, квота исчерпана.", "voice": VOICE_RUSSIAN}
        if "503" in err_str:
            return {"content": "Сэр, сервер перегружен.", "voice": VOICE_RUSSIAN}
        return {"content": "Произошла ошибка, сэр.", "voice": VOICE_RUSSIAN}


# --- ВОСПРОИЗВЕДЕНИЕ ---

# Если текст длиннее этого — озвучиваем его по кускам (по границам
# предложений), чтобы не ждать синтеза ВСЕГО текста перед первым звуком.
_SPEECH_CHUNK_THRESHOLD = 256
# Целевой размер одного куска при склейке соседних предложений. Меньше —
# первый звук раньше, но больше отдельных запросов к edge_tts; больше —
# наоборот. 200 символов — разумный компромисс.
_SPEECH_CHUNK_TARGET_LEN = 200

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?…])\s+|\n+')
# ✅ FIX: раньше сплитили ТОЛЬКО по .!?… — списки с буллетами ("• Открыть
# YouTube\n") без точек в конце строки вообще не разбивались и уезжали
# ОДНИМ куском в несколько тысяч символов (например, каталог возможностей),
# то есть chunking фактически не работал именно там, где он нужнее всего.
# Теперь дополнительно разбиваем по переносам строк.

# Предохранитель на случай сплошного текста без единого разделителя (ни
# точек, ни переносов строк) — принудительно режем по словам, чтобы
# гарантированно не получить один гигантский кусок.
_HARD_CHUNK_MAX = 400


def _split_into_speech_chunks(text: str, target_len: int = _SPEECH_CHUNK_TARGET_LEN) -> list:
    """
    Разбивает длинный текст на куски по границам предложений/строк (точка,
    !?…, перенос строки), а не по количеству символов "в лоб" — иначе можно
    обрезать посреди слова. Соседние короткие куски склеиваются примерно до
    target_len символов, чтобы не дробить речь слишком мелко.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return [text]

    # Если даже один "кусок" после разбиения всё ещё длиннее _HARD_CHUNK_MAX
    # (сплошной текст без точек и переносов) — рубим его по словам.
    safe_sentences = []
    for s in sentences:
        if len(s) <= _HARD_CHUNK_MAX:
            safe_sentences.append(s)
            continue
        words = s.split(' ')
        piece = ""
        for w in words:
            candidate = f"{piece} {w}".strip() if piece else w
            if len(candidate) > _HARD_CHUNK_MAX and piece:
                safe_sentences.append(piece)
                piece = w
            else:
                piece = candidate
        if piece:
            safe_sentences.append(piece)
    sentences = safe_sentences

    chunks = []
    current = ""
    for s in sentences:
        candidate = f"{current} {s}".strip() if current else s
        if len(candidate) > target_len and current:
            chunks.append(current)
            current = s
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _synthesize_speech_chunk(text_chunk: str, voice_name: str) -> io.BytesIO:
    """Синтезирует один кусок текста в mp3 (edge_tts, либо gTTS для армянского)."""
    buf = io.BytesIO()
    if voice_name == "hy":
        tts = gTTS(text_chunk, lang="hy")
        tts.write_to_fp(buf)
    else:
        communicate = edge_tts.Communicate(text_chunk, voice_name, rate=VOICE_RATE, pitch=VOICE_PITCH)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
    buf.seek(0)
    return buf


async def play_voice_async(text, voice_name, force_virtual=False, silent=False):
    if not text: return

    # Ждём пока pygame/edge_tts загружены (обычно уже готово к первому вызову)
    if not _heavy_loaded.is_set():
        await asyncio.get_event_loop().run_in_executor(None, _heavy_loaded.wait)

    _tts_stop.clear()
    is_speaking.set()
    if not silent:
        jarvis_ui.set_status("speaking", text[:80])

    def _interrupted() -> bool:
        if _tts_stop.is_set():
            pygame.mixer.music.stop()
            print("[СТОП] ✓ Речь прервана кнопкой.")
            return True
        return False

    try:
        # ── Длинный текст, обычное (не виртуальное) воспроизведение ──────
        # Синтезируем и проигрываем по кускам-предложениям, "внахлёст":
        # пока звучит кусок N, кусок N+1 уже синтезируется в фоне — это
        # убирает и долгое ожидание перед стартом речи (не нужно ждать
        # синтеза всего текста), и паузы между кусками (следующий уже
        # готов к моменту, когда доиграл предыдущий).
        # ✅ FIX: раньше армянский (voice_name == "hy") исключался из чанкинга
        # и длинный текст на gTTS всегда синтезировался одним куском целиком —
        # заметная пауза перед стартом речи. _synthesize_speech_chunk() уже
        # умеет синтезировать "hy"-куски через gTTS, так что ограничение было
        # лишним — оставляем только реальное условие (виртуальный кабель не
        # поддерживает потоковую подмену буфера на лету).
        if not force_virtual and len(text) > _SPEECH_CHUNK_THRESHOLD:
            chunks = _split_into_speech_chunks(text)
            next_task = asyncio.create_task(_synthesize_speech_chunk(chunks[0], voice_name))
            for i in range(len(chunks)):
                buf = await next_task
                if i + 1 < len(chunks):
                    next_task = asyncio.create_task(
                        _synthesize_speech_chunk(chunks[i + 1], voice_name))
                pygame.mixer.music.load(buf, "mp3")
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if _interrupted():
                        if i + 1 < len(chunks):
                            next_task.cancel()
                        return
                    await asyncio.sleep(0.05)
            return

        # Армянский голос: gTTS (edge_tts не поддерживает "hy")
        if voice_name == "hy":
            tts = gTTS(text, lang="hy")
            mp3_buffer = io.BytesIO()
            tts.write_to_fp(mp3_buffer)
            mp3_buffer.seek(0)
            pygame.mixer.music.load(mp3_buffer, "mp3")
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if _interrupted(): break
                await asyncio.sleep(0.05)
            return

        mp3_buffer = io.BytesIO()
        communicate = edge_tts.Communicate(text, voice_name, rate=VOICE_RATE, pitch=VOICE_PITCH)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buffer.write(chunk["data"])
        mp3_buffer.seek(0)

        if force_virtual and realtime_translation_mode:
            dev_idx = get_virtual_device_idx()
            if dev_idx is not None:
                from pydub import AudioSegment
                seg = AudioSegment.from_mp3(mp3_buffer)
                samples = np.array(seg.get_array_of_samples(), dtype=np.float32)
                samples /= float(1 << (seg.sample_width * 8 - 1))
                if seg.channels == 2:
                    samples = samples.reshape((-1, 2))
                sd.play(samples, seg.frame_rate, device=dev_idx)
                sd.wait()
                return

        pygame.mixer.music.load(mp3_buffer, "mp3")
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if _interrupted(): break
            await asyncio.sleep(0.05)

    except Exception as e:
        print(f"[!] TTS ERROR: {e}")
        try:
            fallback_buffer = io.BytesIO()
            fallback = edge_tts.Communicate("Ошибка", voice_name, rate=VOICE_RATE)
            async for chunk in fallback.stream():
                if chunk["type"] == "audio":
                    fallback_buffer.write(chunk["data"])
            fallback_buffer.seek(0)
            pygame.mixer.music.load(fallback_buffer, "mp3")
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if _interrupted(): break
                await asyncio.sleep(0.05)
        except Exception as e:
            # И основной синтез, и запасной звук "Ошибка" не сработали —
            # пользователь останется вообще без звука и без объяснений.
            # Раньше это глоталось молча.
            print(f"[!] TTS fallback тоже не сработал: {e}")
    finally:
        _tts_stop.clear()
        is_speaking.clear()
        jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))


# --- ФОНОВЫЙ ОБРАБОТЧИК ---

def _speak(text, voice):
    """
    Озвучивает ответ Джарвиса.
    Сбрасывает буфер предсказаний перед воспроизведением,
    чтобы исключить ложные триггеры от предыдущей команды.

    ВАЖНО: эта функция НЕ логирует текст (не print, не add_log, не
    conversation_history) — раньше здесь было такое логирование, но
    process_logic() уже логирует свой ответ ДО возврата, и получалось
    задвоение (см. лог: каждая фраза печаталась дважды). Логировать нужно
    один раз, в точке, где ответ впервые формируется — см. вызов
    jf.try_handle() в background_worker, где логирования раньше не было
    вообще (это и было настоящей дырой).
    """
    for _k in ww_model.prediction_buffer:
        _b = ww_model.prediction_buffer[_k]
        _n = len(_b)
        _b.clear()
        _b.extend([0.0] * _n)
    _run_async(play_voice_async(text, voice))


def background_worker(audio_data_copy):
    global translator_mode, translator_target_lang, translator_voice
    global realtime_translation_mode, recording, conversation_mode, _last_opened
    global conversation_history

    is_processing.set()  # блокируем wake word пока обрабатываем
    try:
        byte_io = io.BytesIO()
        with wave.open(byte_io, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(FS)
            # Предваряем 0.3с тишины — Google STT иначе обрезает первое слово
            silence_pad = np.zeros(int(FS * 0.3), dtype=np.int16)
            wf.writeframes(silence_pad.tobytes())
            wf.writeframes((audio_data_copy * 32767).astype(np.int16).tobytes())
        byte_io.seek(0)

        with sr.AudioFile(byte_io) as source:
            audio_recorded = r_global.record(source)
            try:
                user_text = r_global.recognize_google(audio_recorded, language="ru-RU")
            except Exception:
                # STT не распознал ничего — возвращаемся в режим ожидания
                jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                return

            # Если custom wake word задан и совпадает — это не команда, а повторный триггер
            _cdata = _load_custom_data()
            _cww = (_cdata.get("custom_wake_word") or "").strip().lower()
            if _cww and user_text and _cww in user_text.lower():
                # Это сама фраза пробуждения — игнорируем как команду
                jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                return

            if user_text:
                print(f"[Поток]: {user_text}")
                jarvis_ui.add_log("user", user_text)
                jarvis_ui.set_status("processing", user_text[:60])

                # --- ОБЩЕЕ ОЖИДАНИЕ ДА/НЕТ (сохранение каталога, апдейтер, и т.д.) ---
                # Проверяется ПЕРВЫМ, до любой другой логики. Раньше здесь и в
                # апдейтере были два РАЗНЫХ независимых pending-флага, слушавших
                # одни и те же слова "да"/"нет" — теперь это один общий стек
                # (updater.set_pending_confirm / consume_pending_confirm), так
                # что при столкновении вопросов ответ уходит по адресу, а не в
                # первый попавшийся обработчик.
                if updater.consume_pending_confirm(user_text):
                    jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                    print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                    return

                # --- СТОП-КОМАНДЫ: проверяем ПЕРВЫМИ, до любой другой логики ---
                STOP_WORDS = [
                    "стоп перевод", "stop перевод", "stop translation",
                    "выключи перевод", "выключи режим перевода",
                    "отключи перевод", "прекрати перевод", "хватит переводить"
                ]
                if any(w in user_text.lower() for w in STOP_WORDS):
                    realtime_translation_mode = False
                    translator_mode = False
                    translator_target_lang = None
                    translator_voice = None
                    recording = False
                    conversation_mode = False
                    print("[✓] Переводчик полностью выключен")
                    # Восстанавливаем оригинальный микрофон
                    threading.Thread(target=restore_mic, daemon=True).start()
                    _run_async(play_voice_async("Перевод остановлен, сэр.", VOICE_RUSSIAN))
                    print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                    return

                # --- Режим перевода в реальном времени ---
                elif realtime_translation_mode:
                    res = process_translator(user_text)
                    if res:
                        # ✅ FIX: перевод раньше только печатался в консоль
                        # (process_translator) и озвучивался — в UI/историю/
                        # jarvis_chat_log.json не попадал вообще.
                        jarvis_ui.add_log("jarvis", res["content"])
                        conversation_history.append({"role": "jarvis", "text": res["content"]})
                        _run_async(play_voice_async(res["content"], res["voice"], force_virtual=True))
                    else:
                        print(f"[!] Пропущена фраза из-за ошибки сервера: {user_text}")

                # --- Обычный режим перевода (не реалтайм) ---
                elif translator_mode:
                    res = process_translator(user_text)
                    if res:
                        jarvis_ui.add_log("jarvis", res["content"])
                        conversation_history.append({"role": "jarvis", "text": res["content"]})
                        _run_async(play_voice_async(res["content"], res["voice"]))
                    recording = False
                    conversation_mode = False
                    print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")

                # --- Обычная команда Jarvis ---
                else:
                    t = user_text.lower()

                    # ── Проверка кастомных команд пользователя ──────────────
                    _custom_data = _load_custom_data()
                    _matched_cmd = None

                    # Слова-активаторы кастомной команды (открыть/запустить/включить)
                    _OPEN_CMD_WORDS = [
                        "включи", "включить", "открой", "открыть",
                        "запусти", "запустить", "активируй", "активировать",
                        "запусти команду", "выполни команду", "активируй команду",
                    ]

                    for _cmd in _custom_data.get("commands", []):
                        _trigger = (_cmd.get("trigger") or "").strip().lower()
                        if not _trigger:
                            continue

                        # Вариант 1: фраза — ровно триггер (возможно с "джарвис" в начале)
                        _t_clean = t.replace("джарвис", "").replace("jarvis", "").strip()
                        _exact = (_t_clean == _trigger)

                        # Вариант 2: OPEN_WORD + триггер, без лишних слов
                        # Например "открой ВК", "запусти режим игры"
                        _via_open = False
                        for _ow in _OPEN_CMD_WORDS:
                            if t.startswith(_ow):
                                _remainder = t[len(_ow):].strip()
                                if _remainder == _trigger:
                                    _via_open = True
                                    break

                        if _exact or _via_open:
                            _matched_cmd = _cmd
                            break

                    if _matched_cmd:
                        # Проверяем: содержит ли фраза CLOSE_WORD + триггер команды?
                        # Пример: "выключи ВК" → CLOSE_WORD="выключи", триггер="вк"
                        _CLOSE_W = ["закрой","закрыть","выключи","выключить",
                                    "убери","убрать","останови","остановить"]
                        _has_close = any(w in t for w in _CLOSE_W)
                        _trigger_hit = (_matched_cmd.get("trigger") or "").strip().lower()
                        _close_without_cmd = t
                        for w in _CLOSE_W:
                            _close_without_cmd = _close_without_cmd.replace(w, "").strip()

                        if _has_close and _trigger_hit in _close_without_cmd:
                            # Амбигуитет: "выключи ВК" — закрыть вкладку или отменить команду?
                            _q = f"Что выключить — вкладку '{_matched_cmd['name']}' или команду '{_matched_cmd['name']}'?"
                            print(f"[CUSTOM] Уточнение: {_q}")
                            jarvis_ui.set_status("listening", jarvis_ui.t("txt_clarifying"))
                            _run_async(play_voice_async(
                                f"Вы имеете в виду закрыть вкладку или выполнить команду {_matched_cmd['name']}?",
                                VOICE_RUSSIAN))

                            # Коротко записываем ответ (до 4 сек тишины)
                            import io as _io, wave as _wave
                            _ans_buf = []
                            _sil = 0
                            _MAX_SIL = 30   # ~1.9 сек тишины
                            _MAX_CHUNKS = 120  # ~7.5 сек максимум
                            _chunk_n = 0
                            jarvis_ui.set_status("user_speaking", jarvis_ui.t("txt_speak_cmd"))
                            while _chunk_n < _MAX_CHUNKS:
                                _chunk = sd.rec(CHUNK, samplerate=FS, channels=1, dtype='float32')
                                sd.wait()
                                _chunk_n += 1
                                _v = np.linalg.norm(_chunk) * 10
                                if _v > 3:
                                    _ans_buf.append(_chunk.copy())
                                if _v < VOICE_THRESHOLD:
                                    _sil += 1
                                    if _sil > _MAX_SIL and len(_ans_buf) > 3:
                                        break
                                else:
                                    _sil = 0

                            _ans_text = ""
                            if _ans_buf:
                                _bio = _io.BytesIO()
                                _arr = np.concatenate(_ans_buf).flatten()
                                with _wave.open(_bio, 'wb') as _wf:
                                    _wf.setnchannels(1); _wf.setsampwidth(2); _wf.setframerate(FS)
                                    _wf.writeframes((_arr * 32767).astype(np.int16).tobytes())
                                _bio.seek(0)
                                try:
                                    import speech_recognition as _sr2
                                    _r2 = _sr2.Recognizer()
                                    with _sr2.AudioFile(_bio) as _asrc:
                                        _arec = _r2.record(_asrc)
                                    _ans_text = _r2.recognize_google(_arec, language="ru-RU").lower()
                                    print(f"[CUSTOM] Ответ: {_ans_text}")
                                except Exception:
                                    pass

                            _TAB_WORDS   = ["вкладку","вкладка","таб","tab","сайт","браузер","страницу"]
                            _CMD_WORDS   = ["команду","команда","программу","программа","запуск"]
                            _chose_tab   = any(w in _ans_text for w in _TAB_WORDS)
                            _chose_cmd   = any(w in _ans_text for w in _CMD_WORDS)

                            if _chose_tab or (not _chose_cmd and not _chose_tab):
                                # По умолчанию (не понял) — закрываем вкладку
                                _tab_msg = "Закрываю вкладку, сэр."
                                jarvis_ui.add_log("jarvis", _tab_msg)
                                _speak(_tab_msg, VOICE_RUSSIAN)
                                close_app_safe(_close_without_cmd)
                            else:
                                # Пользователь сказал "команду" — выполняем команду
                                _cmd_msg = "Выполняю команду, сэр."
                                jarvis_ui.add_log("jarvis", _cmd_msg)
                                _speak(_cmd_msg, VOICE_RUSSIAN)
                                _exec_custom_command(_matched_cmd)

                            recording = False
                            conversation_mode = False
                            jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                            print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                            return

                        # Нет CLOSE_WORD — просто выполняем кастомную команду
                        _exec_custom_command(_matched_cmd)
                        msg = f"Выполняю команду '{_matched_cmd['name']}', сэр."
                        print(f"[CUSTOM] {msg}")
                        jarvis_ui.add_log("jarvis", msg)
                        _speak(msg, VOICE_RUSSIAN)
                        recording = False
                        conversation_mode = False
                        jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                        print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                        return

                    # ── Локальный перехват: команды выключения самого Джарвиса ──
                    SELF_SHUTDOWN_WORDS = [
                        "выключись", "отключись", "выключи себя", "отключи себя",
                        "выключись джарвис", "отключись джарвис",
                        "завершись", "завершить работу", "стоп джарвис",
                        "goodbye jarvis", "shutdown jarvis", "turn off jarvis",
                    ]
                    if any(w in t for w in SELF_SHUTDOWN_WORDS):
                        _run_async(play_voice_async("До свидания, сэр.", VOICE_RUSSIAN))
                        # ── Статистика: этот путь выключения раньше вызывал
                        # os._exit(0) напрямую, минуя _close_app() в jarvis_ui.py —
                        # из-за этого вся очередь событий за сессию (громкость,
                        # заметки, буфер обмена, ai_query и т.д.) оставалась
                        # неотправленной. Дублируем здесь ту же логику: фиксируем
                        # app_close и пытаемся отправить очередь одним коротким
                        # блокирующим запросом перед выходом.
                        if analytics:
                            try:
                                analytics.track("app_close")
                                analytics.flush()
                            except Exception as _e:
                                print(f"[ANALYTICS] Ошибка при отправке статистики перед выходом: {_e}")
                        import os as _os; _os._exit(0)

                    # Локальный перехват: закрыть приложение/папку (без API)
                    CLOSE_WORDS = ["закрой", "закрыть", "выключи", "выключить",
                                   "убери", "убрать", "останови", "остановить"]
                    OPEN_FOLDER_WORDS = ["открой папку", "открыть папку", "зайди в папку"]

                    if any(w in t for w in CLOSE_WORDS):
                        # Убираем глагол и получаем имя
                        clean = t
                        for w in CLOSE_WORDS:
                            clean = clean.replace(w, "").strip()

                        # Слова без конкретного объекта — "закрой это/его/последнее"
                        BLANK_CLOSE = {"", "это", "его", "её", "их", "последнее",
                                       "последнюю", "последний", "то что открыл",
                                       "то что открыто", "данную", "данный"}
                        if clean in BLANK_CLOSE:
                            if _last_opened:
                                # Есть запись о последнем открытом — закрываем его
                                clean = _last_opened.get("target") or _last_opened.get("name") or ""
                                print(f"[LOCAL] Закрываю последнее открытое: '{clean}' ({_last_opened['type']})")
                            else:
                                # Ничего не открыто через Jarvis — просим уточнить
                                with _jarvis_lock:
                                    _recent = [i["name"] for i in opened_by_jarvis[-3:]] if opened_by_jarvis else []
                                if _recent:
                                    _hint = ", ".join(_recent)
                                    _q = f"Сэр, что закрыть? Открыто: {_hint}."
                                else:
                                    _q = "Сэр, что именно закрыть? Назовите приложение или сайт."
                                print(f"[LOCAL] Уточнение: {_q}")
                                jarvis_ui.add_log("jarvis", _q)
                                _speak(_q, VOICE_RUSSIAN)
                                recording = False
                                conversation_mode = False
                                jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                                print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                                return
                        else:
                            print(f"[LOCAL] Закрываю: '{clean}'")

                        _run_async(play_voice_async("Выполняю", VOICE_RUSSIAN, silent=True))

                        # Выбираем метод закрытия в зависимости от типа последнего
                        if _last_opened and clean in BLANK_CLOSE or (
                                _last_opened and _last_opened.get("target","") == clean and
                                _last_opened["type"] == "folder"):
                            # Это папка — закрываем через close_folder
                            ok = True; close_folder(clean)
                        else:
                            ok = close_app_safe(clean)

                        msg = "Закрыто, сэр." if ok else f"Сэр, не удалось закрыть '{clean}'."
                        print(f"[Jarvis]: {msg}")
                        jarvis_ui.add_log("jarvis", msg)
                        _speak(msg, VOICE_RUSSIAN)
                        recording = False
                        conversation_mode = False
                        jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                        print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                        return  # ← КРИТИЧНО: выходим до process_logic

                    if any(w in t for w in OPEN_FOLDER_WORDS):
                        clean = t
                        for w in OPEN_FOLDER_WORDS:
                            clean = clean.replace(w, "").strip()
                        # Ищем папку на рабочем столе и в стандартных местах
                        search_bases = [
                            os.path.expanduser("~/Desktop"),
                            os.path.expanduser("~"),
                            "C:\\",
                        ]
                        found = None
                        for base in search_bases:
                            candidate = os.path.join(base, clean)
                            if os.path.isdir(candidate):
                                found = candidate
                                break
                            # Нечёткий поиск: папка содержит искомое слово
                            try:
                                for entry in os.scandir(base):
                                    if entry.is_dir() and clean in entry.name.lower():
                                        found = entry.path
                                        break
                            except Exception:
                                pass
                            if found:
                                break
                        _run_async(play_voice_async("Выполняю", VOICE_RUSSIAN, silent=True))
                        if analytics:
                            analytics.track("open_folder")
                        if found:
                            os.startfile(found)
                            _last_opened = {"type": "folder", "name": clean, "target": os.path.basename(found).lower()}
                            msg = f"Открываю папку, сэр."
                        else:
                            msg = f"Сэр, папка '{clean}' не найдена."
                        print(f"[Jarvis]: {msg}")
                        jarvis_ui.add_log("jarvis", msg)
                        _speak(msg, VOICE_RUSSIAN)
                        recording = False
                        conversation_mode = False
                        print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                        return

                    # ── Расширенные команды (таймер, буфер, скриншот, громкость, заметки, обновления) ──
                    jf_result = jf.try_handle(user_text)
                    if jf_result:
                        _jf_text = jf_result["content"]
                        if _jf_text:
                            print(f"[Jarvis]: {_jf_text}")
                            jarvis_ui.add_log("jarvis", _jf_text)
                            conversation_history.append({"role": "jarvis", "text": _jf_text})
                            max_entries = MAX_HISTORY * 2
                            if len(conversation_history) > max_entries:
                                conversation_history = conversation_history[-max_entries:]
                        _speak(jf_result["content"], jf_result["voice"])
                        recording = False
                        conversation_mode = False
                        jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                        print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")
                        return

                    # Обычный запрос — отправляем в API
                    if analytics:
                        analytics.track("ai_query")
                    res = process_logic(user_text)
                    if res:
                        _speak(res["content"], res["voice"])
                        if res.get("shutdown"):
                            # ── Статистика: тот же случай, что и с локальным
                            # перехватом SELF_SHUTDOWN_WORDS выше — этот путь
                            # тоже завершает процесс напрямую через os._exit(0),
                            # минуя _close_app(). Отправляем очередь событий
                            # перед выходом, чтобы ничего не терялось.
                            if analytics:
                                try:
                                    analytics.track("app_close")
                                    analytics.flush()
                                except Exception as _e:
                                    print(f"[ANALYTICS] Ошибка при отправке статистики перед выходом: {_e}")
                            import os as _os
                            _os._exit(0)
                    recording = False
                    conversation_mode = False
                    jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
                    print("\n>>> ЖДУ КОМАНДУ 'HEY JARVIS'...")

    except Exception as e:
        print(f"[!] WORKER ERROR: {e}")
    finally:
        is_processing.clear()

# --- АУДИО CALLBACK ---

_say_and_listen_active = False  # guard: не запускаем say_and_listen повторно
_ww_cooldown_until = 0.0   # timestamp до которого wake word игнорируется (КД)


def say_and_listen():
    """Произносит приветствие по активации wake word и сбрасывает буфер ПОСЛЕ TTS.
    Язык приветствия берётся из настройки 'ai_language' в jarvis_config.json."""
    global audio_buffer, silence_counter, _say_and_listen_active, _listening_printed
    _say_and_listen_active = True
    _listening_printed = False  # блокируем повторный триггер ДО начала TTS
    jarvis_ui.set_status("listening", jarvis_ui.t("txt_listening"))

    # Читаем актуальный язык ИИ из конфига (пользователь мог поменять без перезапуска)
    _live_cfg = _load_jarvis_config()
    _ai_lang = _live_cfg.get("ai_language", _CFG.get("ai_language", "Русский"))

    if _ai_lang == "English":
        _greet_text  = "I'm listening"
        _greet_voice = VOICE_JARVIS
    else:  # Русский (default)
        _greet_text  = "Слушаю"
        _greet_voice = VOICE_RUSSIAN

    _run_async(play_voice_async(_greet_text, _greet_voice, silent=True))
    audio_buffer = []       # сбрасываем ПОСЛЕ TTS, чтобы не писать тишину под приветствие
    silence_counter = 0
    jarvis_ui.set_status("user_speaking", jarvis_ui.t("txt_listening"))
    _say_and_listen_active = False

# Глобально
_stop_consec = 0
_last_tts_start = 0.0
_low_score_count = 0
_voice_frames = 0
_silence_frames = 0
_last_voice_time = time.time()


def _reset_ww_buffer():
    """
    Обнуляет prediction_buffer модели wake word (заполняет нулями,
    сохраняя длину — deque не поддерживает срез [:], поэтому clear+extend).
    Нужно вызывать после каждой детекции wake word и после каждого TTS-
    ответа, чтобы «память» модели не вызвала ложный повторный триггер.
    Раньше этот блок был продублирован в 3 местах — вынесено сюда.
    """
    if ww_model is None:
        return
    for key in ww_model.prediction_buffer:
        buf = ww_model.prediction_buffer[key]
        n = len(buf)
        buf.clear()
        buf.extend([0.0] * n)


def callback(indata, frames, time_info, status):
    global recording, audio_buffer, silence_counter, is_speaking
    global conversation_mode, LAST_ACTIVITY, realtime_translation_mode, translator_mode
    global _listening_printed, _say_and_listen_active, _ww_cooldown_until

    # Модель ещё не загружена (фоновый поток) — пропускаем
    if ww_model is None:
        return

    # Пока Джарвис говорит — не слушаем микрофон (кроме реалтайм-перевода).
    # Остановка осуществляется кнопкой в интерфейсе.
    if is_speaking.is_set() and not realtime_translation_mode:
        return

    vol = np.linalg.norm(indata) * 10
    # FIX: был порог vol < 3 — при тихом микрофоне silence_counter
    # никогда не рос и программа зависала в режиме "Говорите".
    # Убираем ранний выход: пустые фреймы нужны для счётчика тишины.
    if vol < 0.5:   # только абсолютная тишина (отключённый mic)
        return

    # === РЕЖИМ РЕАЛТАЙМ-ПЕРЕВОДА: всегда пишем, без wake word ===
    if realtime_translation_mode:
        audio_buffer.append(indata.copy())
        if vol < VOICE_THRESHOLD:
            silence_counter += 1
        else:
            silence_counter = 0

        if (silence_counter > 7 and len(audio_buffer) > 10) or len(audio_buffer) > 60:
            raw_audio = np.concatenate(audio_buffer).flatten()
            audio_buffer = []
            silence_counter = 0
            threading.Thread(target=background_worker, args=(raw_audio,), daemon=True).start()
        return
 # === ОБЫЧНЫЙ РЕЖИМ: ждём wake word ===
    if not recording and not is_processing.is_set():
        # ✅ FIX: Не подаём аудио в модель пока Jarvis говорит —
        # это исключает попадание TTS-сигнала в prediction_buffer.
        if is_speaking.is_set():
            return
        ww_audio = (indata * 32767).astype(np.int16).flatten()
        if len(ww_audio) >= CHUNK:
            ww_model.predict(ww_audio)

            # FIX: Порог wake word теперь адаптируется к уровню микрофона.
            # При тихом микрофоне модель даёт меньший скор — снижаем порог.
            # _low_vol_mode = True если последние 3 сек mic был тихим (< 5)
            _score = ww_model.prediction_buffer["hey_jarvis"][-1]
            _cww = jarvis_ui.get_custom_wake_word()
            # Адаптивный порог: если vol низкий — порог ниже
            if vol < 5:
                _threshold = 0.3 if _cww else 0.45
            else:
                _threshold = 0.4 if _cww else 0.6
            _ww_fired = _score > _threshold

            if _ww_fired:
                # ✅ FIX: Сбрасываем буфер предсказаний сразу после детекции,
                # чтобы старое высокое значение не вызвало повторный триггер.
                _reset_ww_buffer()
                if _listening_printed and not _say_and_listen_active:
                    now = time.time()
                    if now < _ww_cooldown_until:
                        return  # КД: игнорируем повторный триггер
                    _ww_cooldown_until = now + 3.0  # 3 сек КД после активации
                    _cww_label = jarvis_ui.get_custom_wake_word() or "hey jarvis"
                    print(f"\n[!] СЛУШАЮ... ")
                    _listening_printed = False
                    recording = True
                    conversation_mode = True
                    LAST_ACTIVITY = time.time()
                    silence_counter = 0
                    jarvis_ui.set_status("listening", jarvis_ui.t("txt_listening"))
                    # audio_buffer сбрасывается в say_and_listen ПОСЛЕ TTS
                    threading.Thread(target=say_and_listen, daemon=True).start()

        return  # <-- return здесь правильно, выходим только если не recording

    # === РЕЖИМ ЗАПИСИ: пишем аудио и ждём тишины ===
    audio_buffer.append(indata.copy())

    if vol < VOICE_THRESHOLD:
        silence_counter += 1
    else:
        silence_counter = 0
        LAST_ACTIVITY = time.time()

    # Передаём реальную громкость в UI (нормализуем: VOICE_THRESHOLD — это ~50% шкалы)
    jarvis_ui.set_volume(min(1.0, vol / (VOICE_THRESHOLD * 2.5)))

    if silence_counter > SILENCE_LIMIT:
        recording = False
        silence_counter = 0

        # ✅ FIX: Не запускаем второй worker если предыдущий ещё не завершился.
        # Без этой проверки callback может снова набрать silence_counter и запустить
        # delayed_start с пустым/шумовым буфером пока первый worker ещё работает.
        if is_processing.is_set():
            audio_buffer = []
            return

        if len(audio_buffer) > 8:
            raw_audio = np.concatenate(audio_buffer).flatten()
            audio_buffer = []
            is_processing.set()

            def delayed_start(audio):
                global _listening_printed, _ww_cooldown_until

                jarvis_ui.set_status("processing", jarvis_ui.t("txt_processing"))
                time.sleep(0.5)  # ждём 0.5 сек перед обработкой
                background_worker(audio)
                # ✅ FIX: Ждём полного завершения озвучки перед сбросом флага.
                # Дополнительная пауза гарантирует, что pygame закончил воспроизведение
                # и буфер openwakeword не получит остатки сигнала с динамиков.
                while is_speaking.is_set():
                    time.sleep(0.05)
                time.sleep(0.3)  # пауза после окончания речи
                # ✅ FIX: Принудительно обнуляем буфер предсказаний перед возвратом
                # в режим ожидания, чтобы исключить ложный триггер от «памяти» модели.
                _reset_ww_buffer()
                # Сдвигаем кулдаун: не реагировать на wake word ещё 2 сек
                # после окончания TTS-ответа (иначе эхо голоса джарвиса триггерит повторно)
                _ww_cooldown_until = time.time() + 2.0
                _listening_printed = True

            threading.Thread(target=delayed_start, args=(raw_audio,), daemon=True).start()

        else:
            # FIX: пользователь ничего не сказал после wake word (буфер пустой).
            # Сбрасываем все флаги и возвращаемся в режим ожидания без зависания.
            audio_buffer = []
            recording = False
            conversation_mode = False
            _reset_ww_buffer()
            jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
            print("\n>>> ЖДУ КОМАНДУ HEY JARVIS...")
            _listening_printed = True

# ── СТАРТ ───────────────────────────────────────────────────────────────────
# ww_model грузится в фоне — самая долгая операция (~2-4с на onnx).
# До готовности модели callback() просто пропускает аудио (ww_model is None).
ww_model = None
_ww_model_ready = threading.Event()

def _load_ww_model():
    global ww_model
    _heavy_loaded.wait()   # ждём пока openwakeword импортирован (или не импортирован)
    if openwakeword is None:
        print("[WAKEWORD] openwakeword не загрузился при старте (см. [HEAVY IMPORT] "
              "выше) — голосовая активация 'Hey Jarvis' недоступна в этой сессии. "
              "Если сработала автопочинка (см. [REPAIR] в логе), перезапустите JARVIS.")
        return
    ww_model = openwakeword.Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    _ww_model_ready.set()
    print(">>> JARVIS ГОТОВ | Жду команду 'HEY JARVIS'...")

threading.Thread(target=_load_ww_model, daemon=True, name="LoadWakeWord").start()

# Pygame mixer — инициализируем в фоне после загрузки heavy imports
def _init_pygame():
    _heavy_loaded.wait()
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
    pygame.mixer.init()

threading.Thread(target=_init_pygame, daemon=True, name="InitPygame").start()

def _audio_loop():
    """Аудио-поток: запускается в фоне, чтобы Qt event loop остался в главном потоке."""
    global conversation_mode
    try:
        with sd.InputStream(samplerate=FS, channels=1, callback=callback, blocksize=CHUNK):
            while True:
                if conversation_mode and not realtime_translation_mode:
                    if time.time() - LAST_ACTIVITY > 20:
                        conversation_mode = False
                sd.sleep(100)
    except Exception as e:
        print(f"[!] AUDIO LOOP ERROR: {e}")
    finally:
        restore_mic()
        pygame.mixer.quit()

def stop_tts():
    """Останавливает текущую озвучку Джарвиса (вызывается кнопкой в UI)."""
    _tts_stop.set()
    print("[СТОП] Кнопка стоп нажата.")


# ── Статистика: запуск JARVIS ────────────────────────────────────────────
# Фиксируем сам факт запуска и в фоне пробуем досослать очередь, которая
# не отправилась при прошлом выключении (если сервер тогда был недоступен).
# Неблокирующе — не задерживаем появление UI.
if analytics:
    analytics.track("app_start")
    analytics.flush_async()

# Инициализируем UI в главном потоке (требование Qt)
jarvis_ui.register_settings_callback(_on_settings_saved)
jarvis_ui.register_stop_tts_callback(stop_tts)
jarvis_ui.start_ui()
jarvis_ui.set_status("idle", jarvis_ui.t("txt_idle"))
print(f"\n>>> JARVIS SYSTEM v0.5 ONLINE | MODEL: {MODEL_ID}")

# Инициализируем модуль расширений (таймеры, буфер, скриншоты, громкость, заметки, обновления)
jf.init(
    play_voice_fn  = lambda text, voice: _run_async(play_voice_async(text, voice)),
    get_client_fn  = _get_client,
    get_model_fn   = lambda: MODEL_ID,
    get_ru_fn      = lambda: VOICE_RUSSIAN,
    get_en_fn      = lambda: VOICE_JARVIS,
    set_status_fn  = jarvis_ui.set_status,
    add_log_fn     = jarvis_ui.add_log,
    # ── Локальные команды без Gemini ──────────────────────────────
    open_app_fn    = open_app_safe,
    close_app_fn   = close_app_safe,
    open_url_fn    = _open_browser_url,
    close_browser_fn = close_browser_tab,
    shutdown_fn    = lambda action: (
        os.system("shutdown /s /t 3") if action == "shutdown"
        else os.system("shutdown /r /t 3")
    ),
    opened_list_fn = lambda: opened_by_jarvis,
    history_fn     = lambda: conversation_history,
)

# ── Система обновлений ────────────────────────────────────────────────────────
updater.init_updater(
    voice_fn  = lambda text, voice: _run_async(play_voice_async(text, voice)),
    log_fn    = jarvis_ui.add_log,
    status_fn = jarvis_ui.set_status,
    voice_ru  = VOICE_RUSSIAN,
)
updater.check_startup(silent=True)   # тихая проверка через 6 сек после запуска

# Аудио крутится в daemon-потоке
_audio_thread = threading.Thread(target=_audio_loop, daemon=True, name="JarvisAudio")
_audio_thread.start()

# Qt event loop блокирует главный поток — так требует Qt на Windows
try:
    jarvis_ui.run_ui_blocking()
except KeyboardInterrupt:
    print("\n>>> СИСТЕМА ВЫКЛЮЧЕНА.")
finally:
    restore_mic()
    pygame.mixer.quit()