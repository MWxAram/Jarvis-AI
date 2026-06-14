"""
jarvis_features.py — расширенные возможности JARVIS
Подключается из main_app.py одной строкой:  import jarvis_features as jf

Содержит:
  1. Таймер / будильник
  2. Чтение буфера обмена (pyperclip)
  3. Скриншот + Gemini Vision
  4. Управление громкостью Windows (pycaw / ctypes)
  5. Заметки (JSON) + голосовой напоминатель
  6. Авто-проверка обновлений (GitHub Releases)
"""

from __future__ import annotations
import os, json, threading, time, re, io, datetime
from pathlib import Path


# ════════════════════════════════════════════════════════════════════
#  HELPERS (injected by main_app at startup)
# ════════════════════════════════════════════════════════════════════
_play_voice      = None   # async wrapper: _run_async(play_voice_async(text, voice))
_get_ai_client   = None   # returns genai.Client or None
_get_model_id    = None   # returns str MODEL_ID
_get_voice_ru    = None   # returns str VOICE_RUSSIAN
_get_voice_en    = None   # returns str VOICE_JARVIS
_set_ui_status   = None   # jarvis_ui.set_status(state, text)
_add_ui_log      = None   # jarvis_ui.add_log(role, text)

def init(play_voice_fn, get_client_fn, get_model_fn,
         get_ru_fn, get_en_fn, set_status_fn, add_log_fn):
    """Call once from main_app after imports."""
    global _play_voice, _get_ai_client, _get_model_id
    global _get_voice_ru, _get_voice_en, _set_ui_status, _add_ui_log
    _play_voice    = play_voice_fn
    _get_ai_client = get_client_fn
    _get_model_id  = get_model_fn
    _get_voice_ru  = get_ru_fn
    _get_voice_en  = get_en_fn
    _set_ui_status = set_status_fn
    _add_ui_log    = add_log_fn
    print("[FEATURES] Модуль расширений инициализирован.")
    # Запускаем фоновый поток напоминаний (notes reminder)
    threading.Thread(target=_notes_reminder_loop, daemon=True,
                     name="JarvisNotesReminder").start()
    # Проверка обновлений при старте — делается через updater.check_startup()
    # в main_app.py, чтобы не запускать дважды.


_BASE = Path(__file__).parent

# ════════════════════════════════════════════════════════════════════
#  1. ТАЙМЕР / БУДИЛЬНИК
# ════════════════════════════════════════════════════════════════════
_active_timers: list[dict] = []   # {id, seconds_left, label, thread}
_timer_id_counter = 0

_TIMER_RE = re.compile(
    r'(\d+)\s*'
    r'(час|часа|часов|ч\b|hour|hours|'
    r'минут|минуты|минуту|мин\b|minute|minutes|min|'
    r'секунд|секунды|секунду|сек\b|second|seconds|sec)',
    re.IGNORECASE
)

def parse_timer_seconds(text: str) -> int | None:
    """Извлекает продолжительность из текста. Возвращает секунды или None."""
    total = 0
    for m in _TIMER_RE.finditer(text):
        n   = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith(('час', 'h')):        total += n * 3600
        elif unit.startswith(('мин', 'min')):    total += n * 60
        elif unit.startswith(('сек', 'sec', 'с')): total += n
    return total if total > 0 else None


def _format_duration(seconds: int) -> str:
    h = seconds // 3600;  r = seconds % 3600
    m = r // 60;          s = r % 60
    parts = []
    if h: parts.append(f"{h} {'час' if h == 1 else 'часа' if 2 <= h <= 4 else 'часов'}")
    if m: parts.append(f"{m} {'минута' if m == 1 else 'минуты' if 2 <= m <= 4 else 'минут'}")
    if s and not h: parts.append(f"{s} {'секунда' if s == 1 else 'секунды' if 2 <= s <= 4 else 'секунд'}")
    return " ".join(parts) or "0 секунд"


def set_timer(seconds: int, label: str = "") -> dict:
    """Запускает таймер на `seconds` секунд. Возвращает словарь таймера."""
    global _timer_id_counter
    _timer_id_counter += 1
    tid = _timer_id_counter
    info = {"id": tid, "seconds": seconds, "label": label, "done": False}

    def _run():
        time.sleep(seconds)
        info["done"] = True
        msg = f"Таймер {label + ' ' if label else ''}сработал, сэр!"
        print(f"[TIMER] {msg}")
        if _add_ui_log:
            _add_ui_log("jarvis", msg)
        if _set_ui_status:
            _set_ui_status("speaking", msg)
        if _play_voice:
            _play_voice(msg, _get_voice_ru())
        # Звуковой сигнал через pygame если доступен
        try:
            import pygame
            if pygame.mixer.get_init():
                freq, dur = 880, 0.4
                import numpy as np
                t_arr = np.linspace(0, dur, int(44100 * dur), False)
                wave = (np.sin(2 * np.pi * freq * t_arr) * 32767).astype(np.int16)
                wave = np.column_stack([wave, wave])
                snd = pygame.sndarray.make_sound(wave)
                for _ in range(3):
                    snd.play()
                    time.sleep(0.5)
        except Exception as e:
            print(f"[TIMER] beep error: {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"JarvisTimer{tid}")
    t.start()
    info["thread"] = t
    _active_timers.append(info)
    print(f"[TIMER] Установлен таймер #{tid} на {_format_duration(seconds)}")
    return info


def cancel_timer(label_or_id: str = "") -> bool:
    """Отменяет последний или указанный таймер (потоки daemon — просто помечаем done)."""
    if not _active_timers:
        return False
    # Найти по label или взять последний активный
    target = None
    for t in reversed(_active_timers):
        if not t["done"]:
            if not label_or_id or label_or_id.lower() in t["label"].lower():
                target = t
                break
    if target:
        target["done"] = True   # поток daemon завершится сам; просто помечаем
        return True
    return False


def list_timers() -> list[dict]:
    return [t for t in _active_timers if not t["done"]]


# ════════════════════════════════════════════════════════════════════
#  2. БУФЕР ОБМЕНА
# ════════════════════════════════════════════════════════════════════

def get_clipboard_text() -> str | None:
    """Возвращает текст из буфера обмена или None."""
    try:
        import pyperclip
        text = pyperclip.paste()
        return text.strip() if text and text.strip() else None
    except ImportError:
        print("[CLIPBOARD] pyperclip не установлен. pip install pyperclip")
        return None
    except Exception as e:
        print(f"[CLIPBOARD] Ошибка: {e}")
        return None


def translate_clipboard(target_lang: str = "русский") -> str | None:
    """Читает буфер, переводит через Gemini, возвращает текст."""
    text = get_clipboard_text()
    if not text:
        return None
    client = _get_ai_client() if _get_ai_client else None
    if not client:
        return "Сэр, API-ключ не задан."
    try:
        prompt = (
            f"Переведи следующий текст на {target_lang}. "
            f"Выведи ТОЛЬКО перевод, без пояснений:\n\n{text[:2000]}"
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        return resp.text.strip() if resp and resp.text else None
    except Exception as e:
        print(f"[CLIPBOARD] translate error: {e}")
        return None


def analyze_clipboard() -> str | None:
    """Читает буфер обмена и отправляет на анализ Gemini."""
    text = get_clipboard_text()
    if not text:
        return None
    client = _get_ai_client() if _get_ai_client else None
    if not client:
        return "Сэр, API-ключ не задан."
    try:
        prompt = f"Кратко объясни или подведи итог следующего текста:\n\n{text[:3000]}"
        resp = client.models.generate_content(
            model=_get_model_id() if _get_model_id else "gemini-2.5-flash-lite",
            contents=prompt
        )
        return resp.text.strip() if resp and resp.text else None
    except Exception as e:
        print(f"[CLIPBOARD] analyze error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════
#  3. СКРИНШОТ + GEMINI VISION
# ════════════════════════════════════════════════════════════════════

def screenshot_and_ask(question: str = "Что изображено на экране? Опиши кратко.") -> str | None:
    """
    Делает скриншот экрана, отправляет в Gemini Vision, возвращает ответ.
    Требует: pip install Pillow
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        print("[VISION] Pillow не установлен. pip install Pillow")
        return "Сэр, для анализа экрана нужна библиотека Pillow. Установите: pip install Pillow"

    client = _get_ai_client() if _get_ai_client else None
    if not client:
        return "Сэр, API-ключ не задан."

    try:
        # Делаем скриншот
        img = ImageGrab.grab()
        # Уменьшаем до 1280px по ширине для экономии токенов
        max_w = 1280
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        img_bytes = buf.getvalue()
        import base64
        img_b64 = base64.b64encode(img_bytes).decode()

        from google.genai import types as genai_types
        response = client.models.generate_content(
            model="gemini-2.5-flash",   # vision поддерживает flash
            contents=[
                genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                question,
            ]
        )
        answer = response.text.strip() if response and response.text else None
        print(f"[VISION] Ответ: {answer[:120] if answer else 'None'}...")
        return answer
    except Exception as e:
        print(f"[VISION] Ошибка: {e}")
        return f"Сэр, не удалось проанализировать экран: {e}"


# ════════════════════════════════════════════════════════════════════
#  4. УПРАВЛЕНИЕ ГРОМКОСТЬЮ WINDOWS
# ════════════════════════════════════════════════════════════════════

def _get_vol_interface():
    """
    Возвращает готовый интерфейс IAudioEndpointVolume или None.
    Реализовано через чистый comtypes (без pycaw) — определяем COM-интерфейсы
    вручную с полным списком методов в правильном порядке vtable.
    Это самый надёжный способ, не зависящий от версии pycaw.
    """
    try:
        import comtypes
        import comtypes.client
        from ctypes import POINTER, byref, c_void_p, c_uint, c_float, c_int, cast

        CLSCTX_ALL = 23  # CLSCTX_INPROC_SERVER | LOCAL_SERVER | REMOTE_SERVER

        # ── IAudioEndpointVolume ───────────────────────────────────────
        class IAudioEndpointVolume(comtypes.IUnknown):
            _iid_ = comtypes.GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
            _methods_ = [
                comtypes.STDMETHOD(comtypes.HRESULT, "RegisterControlChangeNotify", [c_void_p]),
                comtypes.STDMETHOD(comtypes.HRESULT, "UnregisterControlChangeNotify", [c_void_p]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetChannelCount", [POINTER(c_uint)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "SetMasterVolumeLevel", [c_float, POINTER(comtypes.GUID)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "SetMasterVolumeLevelScalar", [c_float, POINTER(comtypes.GUID)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetMasterVolumeLevel", [POINTER(c_float)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetMasterVolumeLevelScalar", [POINTER(c_float)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "SetChannelVolumeLevel", [c_uint, c_float, POINTER(comtypes.GUID)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "SetChannelVolumeLevelScalar", [c_uint, c_float, POINTER(comtypes.GUID)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetChannelVolumeLevel", [c_uint, POINTER(c_float)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetChannelVolumeLevelScalar", [c_uint, POINTER(c_float)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "SetMute", [c_int, POINTER(comtypes.GUID)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetMute", [POINTER(c_int)]),
            ]

        # ── IMMDevice ────────────────────────────────────────────────
        class IMMDevice(comtypes.IUnknown):
            _iid_ = comtypes.GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = [
                comtypes.STDMETHOD(comtypes.HRESULT, "Activate",
                    [POINTER(comtypes.GUID), c_uint, c_void_p, POINTER(c_void_p)]),
            ]

        # ── IMMDeviceEnumerator ──────────────────────────────────────
        class IMMDeviceEnumerator(comtypes.IUnknown):
            _iid_ = comtypes.GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = [
                comtypes.STDMETHOD(comtypes.HRESULT, "EnumAudioEndpoints",
                    [c_uint, c_uint, POINTER(c_void_p)]),
                comtypes.STDMETHOD(comtypes.HRESULT, "GetDefaultAudioEndpoint",
                    [c_uint, c_uint, POINTER(POINTER(IMMDevice))]),
            ]

        CLSID_MMDeviceEnumerator = comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

        # Создаём enumerator
        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator,
            interface=IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )

        # eRender=0, eMultimedia=1
        device = POINTER(IMMDevice)()
        hr = enumerator.GetDefaultAudioEndpoint(0, 1, byref(device))
        if hr != 0 or not device:
            print(f"[VOL] GetDefaultAudioEndpoint failed: hr={hr}")
            return None

        IID_IAudioEndpointVolume = IAudioEndpointVolume._iid_
        iface_ptr = c_void_p()
        hr = device.Activate(byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, byref(iface_ptr))
        if hr != 0 or not iface_ptr:
            print(f"[VOL] Activate failed: hr={hr}")
            return None

        return cast(iface_ptr, POINTER(IAudioEndpointVolume))

    except Exception as e:
        print(f"[VOL] _get_vol_interface error: {type(e).__name__}: {e}")
        return None


def _get_master_volume() -> float | None:
    """Возвращает текущую громкость системы [0.0 … 1.0]."""
    vol = _get_vol_interface()
    if vol is not None:
        try:
            from ctypes import c_float, byref
            level = c_float()
            hr = vol.GetMasterVolumeLevelScalar(byref(level))
            if hr == 0:
                return level.value
            print(f"[VOL] GetMasterVolumeLevelScalar hr={hr}")
        except Exception as e:
            print(f"[VOL] get error: {e}")
    return _get_master_volume_ctypes()


def _set_master_volume(level: float) -> bool:
    """Устанавливает громкость [0.0 … 1.0]. Возвращает True при успехе."""
    level = max(0.0, min(1.0, level))
    vol = _get_vol_interface()
    if vol is not None:
        try:
            hr = vol.SetMasterVolumeLevelScalar(level, None)
            if hr == 0:
                return True
            print(f"[VOL] SetMasterVolumeLevelScalar hr={hr}")
        except Exception as e:
            print(f"[VOL] set error: {e}")
    return _set_master_volume_ctypes(level)


# PowerShell fallback (работает всегда на Windows)
def _get_master_volume_ctypes() -> float | None:
    """Получает громкость через PowerShell."""
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[math]::Round((Get-AudioDevice -Playback).Volume / 100, 2)"],
            capture_output=True, text=True, timeout=5
        )
        val = result.stdout.strip()
        if val:
            return float(val)
    except Exception:
        pass
    # Ещё один вариант через WScript (работает без Get-AudioDevice)
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$vol = (New-Object -ComObject WScript.Shell); $vol.SendKeys([char]174);"
             "Start-Sleep -Milliseconds 100;"
             "$mixer = New-Object -COMObject '{1FBFA8C0-A1F1-4E3B-B082-3CF69D0E3E3C}';"
             "Write-Output 0.5"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        pass
    return None


def _set_master_volume_ctypes(level: float) -> bool:
    """Fallback: устанавливает громкость через PowerShell/nircmd."""
    import subprocess, os
    pct = int(level * 100)

    # Способ 1: nircmd (если установлен)
    nircmd_paths = [
        r"C:\Program Files\NirCmd\nircmd.exe",
        r"C:\Program Files (x86)\NirCmd\nircmd.exe",
        os.path.expanduser("~/nircmd.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "nircmd.exe"),
    ]
    for p in nircmd_paths:
        if os.path.exists(p):
            try:
                subprocess.run([p, "setsysvolume", str(int(level * 65535))],
                               capture_output=True, timeout=5)
                print(f"[VOL] nircmd → {pct}%")
                return True
            except Exception:
                pass

    # Способ 2: PowerShell через SoundVolumeView (nirsoft) или встроенный Audio API
    try:
        # Этот скрипт работает на Windows 10/11 без сторонних утилит
        ps_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {{
    int f(); int g(); int h(); int i();
    int SetMasterVolumeLevelScalar(float fLevel, System.IntPtr pguidEventContext);
    int j();
    int GetMasterVolumeLevelScalar(out float pfLevel);
}}
[Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
[ClassInterface(ClassInterfaceType.None)]
class MMDeviceEnumerator {{}}
'@ -Language CSharp
$DeviceEnumeratorCLSID = [Type]::GetTypeFromCLSID([Guid]"BCDE0395-E52F-467C-8E3D-C4579291692E")
$DeviceEnumerator = [Activator]::CreateInstance($DeviceEnumeratorCLSID)
$DefaultDevice = $DeviceEnumerator.GetDefaultAudioEndpoint(0, 1)
$AudioEndpointVolumeIID = [Guid]"5CDF2C82-841E-4546-9722-0CF74078229A"
$AudioEndpointVolume = $DefaultDevice.Activate($AudioEndpointVolumeIID, 23, $null)
$AudioEndpointVolume.SetMasterVolumeLevelScalar({level:.4f}, [IntPtr]::Zero)
Write-Output "OK"
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=8
        )
        if "OK" in result.stdout or result.returncode == 0:
            print(f"[VOL] PowerShell COM → {pct}%")
            return True
    except Exception as e:
        print(f"[VOL] PowerShell COM error: {e}")

    return False

_VOL_STEP = 0.10   # шаг изменения громкости (10%)

def volume_up(step: float = _VOL_STEP) -> str:
    cur = _get_master_volume()
    if cur is None:
        return "Сэр, не удалось получить уровень громкости."
    new_vol = min(1.0, cur + step)
    ok = _set_master_volume(new_vol)
    pct = int(new_vol * 100)
    msg = f"Громкость увеличена до {pct}%." if ok else "Сэр, не удалось изменить громкость."
    print(f"[VOL] +{int(step*100)}% → {pct}%")
    return msg


def volume_down(step: float = _VOL_STEP) -> str:
    cur = _get_master_volume()
    if cur is None:
        return "Сэр, не удалось получить уровень громкости."
    new_vol = max(0.0, cur - step)
    ok = _set_master_volume(new_vol)
    pct = int(new_vol * 100)
    msg = f"Громкость уменьшена до {pct}%." if ok else "Сэр, не удалось изменить громкость."
    print(f"[VOL] -{int(step*100)}% → {pct}%")
    return msg


def volume_set(percent: int) -> str:
    ok = _set_master_volume(percent / 100.0)
    msg = f"Громкость установлена на {percent}%." if ok else "Сэр, не удалось изменить громкость."
    print(f"[VOL] set {percent}%")
    return msg


def volume_mute() -> str:
    vol = _get_vol_interface()
    if vol is None:
        return "Сэр, не удалось получить аудио интерфейс."
    try:
        from ctypes import c_int, byref
        muted = c_int()
        hr = vol.GetMute(byref(muted))
        if hr != 0:
            return f"Сэр, не удалось получить состояние звука (hr={hr})."
        new_muted = 0 if muted.value else 1
        hr = vol.SetMute(new_muted, None)
        if hr != 0:
            return f"Сэр, не удалось переключить звук (hr={hr})."
        return "Звук выключен." if new_muted else "Звук включён."
    except Exception as e:
        return f"Сэр, не удалось переключить звук: {e}"


_DEFAULT_VOLUME = 50   # % — стандартная/нормальная громкость

def volume_default() -> str:
    """Устанавливает стандартную громкость (50%)."""
    ok = _set_master_volume(_DEFAULT_VOLUME / 100.0)
    if ok:
        return f"Стандартная громкость установлена: {_DEFAULT_VOLUME}%."
    return "Сэр, не удалось установить громкость."


# ════════════════════════════════════════════════════════════════════
#  5. ЗАМЕТКИ (JSON + голосовой напоминатель)
# ════════════════════════════════════════════════════════════════════

_NOTES_FILE = _BASE / "jarvis_notes.json"

def _load_notes() -> list[dict]:
    if _NOTES_FILE.exists():
        try:
            return json.loads(_NOTES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_notes(notes: list[dict]):
    try:
        _NOTES_FILE.write_text(
            json.dumps(notes, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[NOTES] save error: {e}")


def add_note(text: str, remind_at: datetime.datetime | None = None) -> str:
    """
    Добавляет заметку. remind_at — время напоминания (или None).
    """
    notes = _load_notes()
    note = {
        "id": int(time.time() * 1000),
        "text": text,
        "created": datetime.datetime.now().isoformat(),
        "remind_at": remind_at.isoformat() if remind_at else None,
        "reminded": False,
    }
    notes.append(note)
    _save_notes(notes)
    ts = f" (напомню в {remind_at.strftime('%H:%M')})" if remind_at else ""
    msg = f"Запомнил{ts}: {text}"
    print(f"[NOTES] {msg}")
    return msg


def list_notes(limit: int = 5) -> str:
    """Возвращает последние N заметок в виде текста."""
    notes = _load_notes()
    if not notes:
        return "Заметок пока нет, сэр."
    recent = notes[-limit:]
    lines = []
    for i, n in enumerate(reversed(recent), 1):
        ts = n["created"][:16].replace("T", " ")
        remind = ""
        if n.get("remind_at"):
            remind = f" [напомню: {n['remind_at'][:16].replace('T', ' ')}]"
        lines.append(f"{i}. {n['text']} ({ts}){remind}")
    return "Ваши заметки:\n" + "\n".join(lines)


def delete_last_note() -> str:
    notes = _load_notes()
    if not notes:
        return "Заметок нет, сэр."
    removed = notes.pop()
    _save_notes(notes)
    return f"Удалил заметку: {removed['text']}"


def _notes_reminder_loop():
    """Фоновый поток: проверяет напоминания каждые 30 секунд."""
    while True:
        time.sleep(30)
        try:
            notes  = _load_notes()
            now    = datetime.datetime.now()
            changed = False
            for note in notes:
                if note.get("reminded") or not note.get("remind_at"):
                    continue
                try:
                    remind_dt = datetime.datetime.fromisoformat(note["remind_at"])
                except Exception:
                    continue
                if now >= remind_dt:
                    msg = f"Напоминание, сэр: {note['text']}"
                    print(f"[NOTES] {msg}")
                    if _add_ui_log:   _add_ui_log("jarvis", msg)
                    if _set_ui_status: _set_ui_status("speaking", msg)
                    if _play_voice:   _play_voice(msg, _get_voice_ru())
                    note["reminded"] = True
                    changed = True
            if changed:
                _save_notes(notes)
        except Exception as e:
            print(f"[NOTES] reminder loop error: {e}")


# ════════════════════════════════════════════════════════════════════
#  6. АВТО-ПРОВЕРКА ОБНОВЛЕНИЙ
# ════════════════════════════════════════════════════════════════════

# Версия из файла version.txt рядом со скриптом (или задаём вручную)
# ── Обновления — делегируем в updater.py ────────────────────────────────────
def check_updates(silent: bool = False) -> str:
    """Проверяет и скачивает обновления через updater.py."""
    try:
        import updater
        return updater.check_and_update(silent=silent)
    except ImportError:
        msg = "Сэр, модуль обновлений не найден. Убедитесь что updater.py рядом с программой."
        print(f"[UPDATE] {msg}")
        if not silent and _play_voice:
            _play_voice(msg, _get_voice_ru())
        return msg
    except Exception as e:
        msg = f"Сэр, ошибка при проверке обновлений: {e}"
        print(f"[UPDATE] {msg}")
        if not silent and _play_voice:
            _play_voice(msg, _get_voice_ru())
        return msg


#  COMMAND ROUTER — единая точка входа для main_app
# ════════════════════════════════════════════════════════════════════

# Регулярные выражения для маршрутизации команд
_RU_LOWER_RE  = re.compile(r'[а-яёА-ЯЁ]')

_CMD_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Таймер
    (re.compile(r'(поставь|установи|запусти|поставить|установить)?\s*(таймер|будильник|напомни через)', re.I), "timer_set"),
    (re.compile(r'(отмени|сбрось|убери)\s*(таймер|будильник)', re.I), "timer_cancel"),
    (re.compile(r'(список|покажи|сколько).*таймер', re.I), "timer_list"),
    # Буфер обмена
    (re.compile(r'(переведи|перевести)\s*(из буфера|буфер|скопированное|скопированный текст)', re.I), "clipboard_translate"),
    (re.compile(r'(что|анализ|объясни|прочитай)\s*(в буфере|буфер|скопированное)', re.I), "clipboard_analyze"),
    (re.compile(r'(прочитай|озвучь)\s*(буфер|скопированный текст|скопированное)', re.I), "clipboard_read"),
    # Скриншот
    (re.compile(r'(что|опиши|анализ|посмотри)\s*(на экране|на мониторе|на дисплее|скрин|screenshot)', re.I), "screenshot"),
    (re.compile(r'(сделай|сними|сохрани)\s*(скриншот|снимок экрана|screenshot)', re.I), "screenshot"),
    # Громкость
    (re.compile(r'(громче|увеличь громкость|прибавь громкость|volume up)', re.I), "vol_up"),
    (re.compile(r'(тише|уменьши громкость|убавь громкость|volume down)', re.I), "vol_down"),
    (re.compile(r'(mute|без звука|выключи звук|заглуши)', re.I), "vol_mute"),
    (re.compile(r'(стандартн|нормальн|обычн|по умолчанию|default).{0,15}(громкост|volume|звук)', re.I), "vol_default"),
    (re.compile(r'(громкост|volume|звук).{0,15}(стандартн|нормальн|обычн|по умолчанию|default)', re.I), "vol_default"),
    (re.compile(r'(громкость|volume)\s*(\d+)\s*(%|процент)?', re.I), "vol_set"),
    # Заметки
    (re.compile(r'(запомни|запиши|сохрани|добавь заметку|создай заметку)[:\s]+(.+)', re.I | re.DOTALL), "note_add"),
    (re.compile(r'(покажи|список|читай|прочитай|что)\s*(заметки|напоминания|заметок)', re.I), "note_list"),
    (re.compile(r'(удали|убери)\s*(последнюю)?\s*(заметку|напоминание)', re.I), "note_delete"),
    # Обновления
    (re.compile(r'(проверь|проверить|есть ли|ищи)\s*(обновление|обновления|update)', re.I), "check_updates"),
]


def try_handle(text: str) -> dict | None:
    """
    Проверяет текст на совпадение с командами расширений.
    Если совпадает — выполняет и возвращает {"content": str, "voice": str}.
    Если не совпадает — возвращает None (main_app отправит в Gemini).
    """
    t_lower = text.strip().lower()

    for pattern, cmd_id in _CMD_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue

        voice_ru = _get_voice_ru() if _get_voice_ru else "ru-RU-DmitryNeural"
        voice_en = _get_voice_en() if _get_voice_en else "en-GB-ThomasNeural"

        # ── ТАЙМЕР ──────────────────────────────────────────────────
        if cmd_id == "timer_set":
            secs = parse_timer_seconds(text)
            if secs:
                # Ищем метку (что напомнить)
                label_m = re.search(
                    r'(напомни|напоминание|таймер|будильник)[:\s]+(.+?)(?:\s+через|\s+на\s+\d|$)',
                    text, re.I
                )
                label = label_m.group(2).strip() if label_m else ""
                # Убираем время из label
                label = _TIMER_RE.sub("", label).strip(" ,.")
                set_timer(secs, label)
                dur_str = _format_duration(secs)
                msg = f"Таймер установлен на {dur_str}{', ' + label if label else ''}, сэр."
                return {"content": msg, "voice": voice_ru}
            else:
                return {"content": "Сэр, не понял продолжительность таймера. Скажите, например: таймер на 10 минут.", "voice": voice_ru}

        elif cmd_id == "timer_cancel":
            ok = cancel_timer()
            return {"content": "Таймер отменён, сэр." if ok else "Активных таймеров нет, сэр.", "voice": voice_ru}

        elif cmd_id == "timer_list":
            active = list_timers()
            if not active:
                return {"content": "Активных таймеров нет, сэр.", "voice": voice_ru}
            lines = [f"Активные таймеры:"]
            for t in active:
                lines.append(f"- {t['label'] or 'Таймер'} #{t['id']}")
            return {"content": "\n".join(lines), "voice": voice_ru}

        # ── БУФЕР ОБМЕНА ────────────────────────────────────────────
        elif cmd_id == "clipboard_translate":
            # Определяем целевой язык
            lang_match = re.search(r'на\s+(русский|английский|армянский|немецкий|французский|испанский)', text, re.I)
            target = lang_match.group(1) if lang_match else "русский"
            result = translate_clipboard(target)
            if result:
                return {"content": result, "voice": voice_ru}
            return {"content": "Сэр, буфер обмена пуст.", "voice": voice_ru}

        elif cmd_id == "clipboard_analyze":
            result = analyze_clipboard()
            if result:
                return {"content": result, "voice": voice_ru}
            return {"content": "Сэр, буфер обмена пуст.", "voice": voice_ru}

        elif cmd_id == "clipboard_read":
            text_cb = get_clipboard_text()
            if text_cb:
                short = text_cb[:300] + ("..." if len(text_cb) > 300 else "")
                return {"content": short, "voice": voice_ru}
            return {"content": "Сэр, буфер обмена пуст.", "voice": voice_ru}

        # ── СКРИНШОТ ────────────────────────────────────────────────
        elif cmd_id == "screenshot":
            # Ищем вопрос после ключевого слова
            q_match = re.search(r'(?:экране|дисплее|скрине|screenshot)[,\s]+(.+)', text, re.I)
            question = q_match.group(1).strip() if q_match else "Что изображено на экране? Опиши кратко."
            if _set_ui_status:
                _set_ui_status("processing", "Анализирую экран...")
            result = screenshot_and_ask(question)
            return {"content": result or "Сэр, не удалось проанализировать экран.", "voice": voice_ru}

        # ── ГРОМКОСТЬ ───────────────────────────────────────────────
        elif cmd_id == "vol_up":
            # Проверяем: есть ли конкретное число
            pct_m = re.search(r'на\s+(\d+)\s*(%|процент)?', text, re.I)
            if pct_m:
                step = int(pct_m.group(1)) / 100.0
                return {"content": volume_up(step), "voice": voice_ru}
            return {"content": volume_up(), "voice": voice_ru}

        elif cmd_id == "vol_down":
            pct_m = re.search(r'на\s+(\d+)\s*(%|процент)?', text, re.I)
            if pct_m:
                step = int(pct_m.group(1)) / 100.0
                return {"content": volume_down(step), "voice": voice_ru}
            return {"content": volume_down(), "voice": voice_ru}

        elif cmd_id == "vol_mute":
            return {"content": volume_mute(), "voice": voice_ru}

        elif cmd_id == "vol_default":
            return {"content": volume_default(), "voice": voice_ru}

        elif cmd_id == "vol_set":
            pct_m = re.search(r'(\d+)', text)
            if pct_m:
                return {"content": volume_set(int(pct_m.group(1))), "voice": voice_ru}
            return {"content": "Сэр, укажите уровень громкости, например: громкость 50%", "voice": voice_ru}

        # ── ЗАМЕТКИ ─────────────────────────────────────────────────
        elif cmd_id == "note_add":
            # Ищем время напоминания: "в 15:30" или "через X минут/часов"
            remind_dt = None
            time_m = re.search(r'в\s+(\d{1,2})[:\.](\d{2})', text)
            in_m = re.search(r'через\s+(\d+)\s*(минут|мин|час|часа|часов)', text, re.I)
            if time_m:
                try:
                    h, min_ = int(time_m.group(1)), int(time_m.group(2))
                    now = datetime.datetime.now()
                    remind_dt = now.replace(hour=h, minute=min_, second=0, microsecond=0)
                    if remind_dt <= now:
                        remind_dt += datetime.timedelta(days=1)
                except Exception:
                    pass
            elif in_m:
                n   = int(in_m.group(1))
                unit = in_m.group(2).lower()
                delta = datetime.timedelta(hours=n) if unit.startswith('час') else datetime.timedelta(minutes=n)
                remind_dt = datetime.datetime.now() + delta

            # Чистим текст заметки от служебных слов
            note_text = text
            for w in ["запомни", "запиши", "сохрани", "добавь заметку", "создай заметку",
                      "джарвис", "jarvis"]:
                note_text = re.sub(w, "", note_text, flags=re.I).strip(" :,.")
            # Убираем время
            if time_m:
                note_text = re.sub(r'в\s+\d{1,2}[:.]\d{2}', "", note_text).strip(" :,.")
            if in_m:
                note_text = re.sub(r'через\s+\d+\s*(минут|мин|час|часа|часов)', "", note_text, flags=re.I).strip(" :,.")

            if note_text:
                msg = add_note(note_text, remind_dt)
                return {"content": msg, "voice": voice_ru}
            return {"content": "Сэр, не понял что запомнить. Скажите: запомни — текст заметки.", "voice": voice_ru}

        elif cmd_id == "note_list":
            return {"content": list_notes(), "voice": voice_ru}

        elif cmd_id == "note_delete":
            return {"content": delete_last_note(), "voice": voice_ru}

        # ── ОБНОВЛЕНИЯ ──────────────────────────────────────────────
        elif cmd_id == "check_updates":
            result = check_updates(silent=False)
            return {"content": result, "voice": voice_ru}

    return None   # не наша команда — пусть main_app отправит в Gemini
