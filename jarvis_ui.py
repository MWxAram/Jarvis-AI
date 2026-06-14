"""
JARVIS UI — sci-fi overlay  v0.7
Changes: resize handles, close button, no expand button,
         ring uses setMaximumSize (no overlap), improved settings (lang + API key),
         persistent dialog log, day separators + timestamps in history, fix clear button
"""

import sys, math, threading, random, json, os
import time as _time
import hashlib, base64, socket

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QCheckBox, QScrollArea, QFrame, QComboBox,
    QSizeGrip, QStackedWidget, QListWidget, QListWidgetItem,
    QAbstractItemView, QSpinBox, QDialog, QTextEdit, QSizePolicy,
    QSystemTrayIcon, QMenu, QAction, QColorDialog
)
import uuid
from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal, QSize
from PyQt5.QtGui  import (
    QPainter, QColor, QPen, QFont, QLinearGradient,
    QRadialGradient, QBrush, QPainterPath, QCursor, QRegExpValidator, QIcon, QPixmap
)
from PyQt5.QtCore import QRegExp

# ══════════════════════ UI SOUND SYSTEM ═════════════════════════════
# 6 звуковых паков. Активный пак читается из конфига (ключ "sfx_pack").
# Все звуки генерируются синтетически через numpy и воспроизводятся
# через sounddevice — не зависит от состояния pygame.mixer.

import numpy as _np_sfx

_SFX_ARRAYS: dict = {}   # кэш: (pack, name) → ndarray
_SFX_PACK_ACTIVE = "tactical"  # будет перезаписан при загрузке конфига


# ─── описания паков (для UI) ────────────────────────────────────────
SFX_PACKS = [
    ("tactical",   "⬛  Tactical",     "Короткие сухие тики — строго, по-деловому"),
    ("military",   "🎖  Military",     "Жёсткие однотонные бипы, без украшений"),
    ("deep",       "🔵  Deep",         "Низкие глубокие щелчки, тяжёлый характер"),
    ("minimal",    "◽  Minimal",      "Еле слышные тихие касания"),
    ("sharp",      "⚡  Sharp",        "Резкие высокие импульсы, чёткость"),
    ("soft",       "🟢  Soft Confirm", "Мягкие округлые подтверждения"),
]

def _sfx_load_pack():
    """Читает активный пак из конфига при старте."""
    global _SFX_PACK_ACTIVE
    try:
        cfg = _load_config() if "_load_config" in dir() else {}
        _SFX_PACK_ACTIVE = cfg.get("sfx_pack", "tactical")
    except Exception:
        pass

def _sfx_set_pack(pack_id: str):
    """Меняет активный пак и сбрасывает кэш."""
    global _SFX_PACK_ACTIVE
    _SFX_PACK_ACTIVE = pack_id
    _SFX_ARRAYS.clear()
    try:
        import json, os
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
        cfg = {}
        if os.path.exists(_p):
            cfg = json.load(open(_p, encoding="utf-8"))
        cfg["sfx_pack"] = pack_id
        json.dump(cfg, open(_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SFX] pack save error: {e}")
    print(f"[SFX] Pack → {pack_id}")


def _mk(freq, dur, vol=0.20, fade=30, shape="sine"):
    """Возвращает float32 1-D массив."""
    sr = 44100
    n  = int(sr * dur)
    t  = _np_sfx.linspace(0, dur, n, endpoint=False)
    if shape == "square":
        w = _np_sfx.sign(_np_sfx.sin(2*_np_sfx.pi*freq*t)).astype(_np_sfx.float32)
    elif shape == "tri":
        w = (2*_np_sfx.abs(2*(t*freq - _np_sfx.floor(t*freq+0.5)))-1).astype(_np_sfx.float32)
    elif shape == "saw":
        w = (2*(t*freq - _np_sfx.floor(t*freq+0.5))).astype(_np_sfx.float32)
    else:
        w = _np_sfx.sin(2*_np_sfx.pi*freq*t).astype(_np_sfx.float32)
    fn = min(int(sr*fade/1000), n)
    env = _np_sfx.ones(n, dtype=_np_sfx.float32)
    env[-fn:] = _np_sfx.linspace(1.0, 0.0, fn, dtype=_np_sfx.float32)
    # Атака 2 мс — убирает щелчок при старте
    an = min(int(sr*0.002), n)
    env[:an] = _np_sfx.linspace(0.0, 1.0, an, dtype=_np_sfx.float32)
    return w * env * float(vol)

def _seq(*parts):
    """Склеивает несколько float32 массивов в один."""
    return _np_sfx.concatenate(parts).astype(_np_sfx.float32)


# ─── паки ───────────────────────────────────────────────────────────
def _build_pack_tactical(name):
    """Строгие короткие сухие тики. Основной звук — одиночный импульс 1800 Hz."""
    sr = 44100
    if name == "click":    return _mk(1800, 0.018, vol=0.22, fade=12)
    if name == "tab":      return _mk(1600, 0.022, vol=0.19, fade=16)
    if name == "toggle":   return _mk(1400, 0.020, vol=0.17, fade=14, shape="square")
    if name == "open":
        return _seq(_mk(1200,0.018,vol=0.18,fade=10), _np_sfx.zeros(int(sr*0.018),dtype=_np_sfx.float32),
                    _mk(1800,0.018,vol=0.22,fade=10))
    if name == "close":    return _mk(1200, 0.022, vol=0.19, fade=16)
    if name == "stop":     return _mk(800,  0.030, vol=0.24, fade=20, shape="square")
    if name == "save":
        return _seq(_mk(1400,0.018,vol=0.18,fade=10), _np_sfx.zeros(int(sr*0.012),dtype=_np_sfx.float32),
                    _mk(1800,0.018,vol=0.22,fade=10), _np_sfx.zeros(int(sr*0.012),dtype=_np_sfx.float32),
                    _mk(2200,0.022,vol=0.20,fade=14))
    if name == "add":      return _mk(2000, 0.018, vol=0.19, fade=12)
    if name == "delete":   return _mk(900,  0.022, vol=0.19, fade=16)
    if name == "nav":      return _mk(1600, 0.014, vol=0.16, fade=10)


def _build_pack_military(name):
    """Жёсткие однотонные бипы без украшений. Прямоугольные импульсы."""
    sr = 44100
    if name == "click":    return _mk(880,  0.025, vol=0.20, fade=8,  shape="square")
    if name == "tab":      return _mk(760,  0.030, vol=0.18, fade=10, shape="square")
    if name == "toggle":   return _mk(660,  0.028, vol=0.16, fade=10, shape="square")
    if name == "open":
        return _seq(_mk(660,0.025,vol=0.17,fade=8,shape="square"),
                    _np_sfx.zeros(int(sr*0.020),dtype=_np_sfx.float32),
                    _mk(880,0.025,vol=0.20,fade=8,shape="square"))
    if name == "close":    return _mk(550,  0.030, vol=0.18, fade=10, shape="square")
    if name == "stop":
        return _seq(_mk(880,0.020,vol=0.22,fade=6,shape="square"),
                    _np_sfx.zeros(int(sr*0.010),dtype=_np_sfx.float32),
                    _mk(660,0.020,vol=0.22,fade=6,shape="square"),
                    _np_sfx.zeros(int(sr*0.010),dtype=_np_sfx.float32),
                    _mk(440,0.025,vol=0.22,fade=8,shape="square"))
    if name == "save":
        return _seq(_mk(660,0.020,vol=0.18,fade=6,shape="square"),
                    _np_sfx.zeros(int(sr*0.015),dtype=_np_sfx.float32),
                    _mk(880,0.028,vol=0.21,fade=8,shape="square"))
    if name == "add":      return _mk(1100, 0.022, vol=0.19, fade=8,  shape="square")
    if name == "delete":   return _mk(440,  0.028, vol=0.19, fade=10, shape="square")
    if name == "nav":      return _mk(760,  0.018, vol=0.16, fade=8,  shape="square")


def _build_pack_deep(name):
    """Низкие глубокие щелчки. Частоты 80–400 Hz, синус с быстрым затуханием."""
    sr = 44100
    if name == "click":    return _mk(220,  0.040, vol=0.28, fade=30)
    if name == "tab":      return _mk(180,  0.045, vol=0.26, fade=35)
    if name == "toggle":   return _mk(150,  0.042, vol=0.24, fade=32)
    if name == "open":
        return _seq(_mk(120,0.035,vol=0.22,fade=25),
                    _np_sfx.zeros(int(sr*0.025),dtype=_np_sfx.float32),
                    _mk(220,0.045,vol=0.28,fade=35))
    if name == "close":    return _mk(110,  0.045, vol=0.26, fade=38)
    if name == "stop":     return _mk(80,   0.060, vol=0.30, fade=45)
    if name == "save":
        n = int(sr * 0.12)
        t = _np_sfx.linspace(0, 0.12, n, endpoint=False)
        w = (_np_sfx.sin(2*_np_sfx.pi*180*t) + _np_sfx.sin(2*_np_sfx.pi*270*t)*0.5).astype(_np_sfx.float32)
        env = _np_sfx.ones(n,dtype=_np_sfx.float32); env[-int(sr*0.05):] = _np_sfx.linspace(1,0,int(sr*0.05))
        env[:int(sr*0.002)] = _np_sfx.linspace(0,1,int(sr*0.002))
        return (w / 1.5 * env * 0.28).astype(_np_sfx.float32)
    if name == "add":      return _mk(260,  0.038, vol=0.26, fade=28)
    if name == "delete":   return _mk(100,  0.048, vol=0.26, fade=40)
    if name == "nav":      return _mk(200,  0.030, vol=0.22, fade=22)


def _build_pack_minimal(name):
    """Едва слышные тихие прикосновения. Volume 0.07–0.10."""
    sr = 44100
    if name == "click":    return _mk(2400, 0.012, vol=0.08, fade=10)
    if name == "tab":      return _mk(2000, 0.015, vol=0.07, fade=12)
    if name == "toggle":   return _mk(1800, 0.012, vol=0.07, fade=10)
    if name == "open":     return _mk(2200, 0.020, vol=0.09, fade=16)
    if name == "close":    return _mk(1600, 0.015, vol=0.08, fade=12)
    if name == "stop":     return _mk(1200, 0.020, vol=0.10, fade=15)
    if name == "save":     return _mk(2400, 0.025, vol=0.09, fade=20)
    if name == "add":      return _mk(2600, 0.012, vol=0.08, fade=10)
    if name == "delete":   return _mk(1400, 0.015, vol=0.08, fade=12)
    if name == "nav":      return _mk(2200, 0.010, vol=0.07, fade=8)


def _build_pack_sharp(name):
    """Резкие высокие импульсы. Пилообразная волна, атака мгновенная."""
    sr = 44100
    if name == "click":    return _mk(3200, 0.015, vol=0.16, fade=10, shape="saw")
    if name == "tab":      return _mk(2800, 0.018, vol=0.15, fade=12, shape="saw")
    if name == "toggle":   return _mk(2400, 0.016, vol=0.14, fade=11, shape="saw")
    if name == "open":
        return _seq(_mk(2000,0.014,vol=0.14,fade=9,shape="saw"),
                    _np_sfx.zeros(int(sr*0.010),dtype=_np_sfx.float32),
                    _mk(3200,0.016,vol=0.17,fade=10,shape="saw"))
    if name == "close":    return _mk(2000, 0.018, vol=0.15, fade=12, shape="saw")
    if name == "stop":
        return _seq(_mk(3200,0.014,vol=0.18,fade=8,shape="saw"),
                    _np_sfx.zeros(int(sr*0.008),dtype=_np_sfx.float32),
                    _mk(2000,0.018,vol=0.18,fade=10,shape="saw"))
    if name == "save":
        return _seq(_mk(2400,0.013,vol=0.15,fade=8,shape="saw"),
                    _np_sfx.zeros(int(sr*0.008),dtype=_np_sfx.float32),
                    _mk(3200,0.016,vol=0.17,fade=10,shape="saw"))
    if name == "add":      return _mk(3600, 0.013, vol=0.15, fade=9,  shape="saw")
    if name == "delete":   return _mk(1800, 0.018, vol=0.15, fade=12, shape="saw")
    if name == "nav":      return _mk(3000, 0.012, vol=0.13, fade=8,  shape="saw")


def _build_pack_soft(name):
    """Мягкие округлые подтверждения. Синус, плавные конверты."""
    sr = 44100
    if name == "click":    return _mk(880,  0.055, vol=0.16, fade=45)
    if name == "tab":      return _mk(740,  0.060, vol=0.15, fade=50)
    if name == "toggle":   return _mk(660,  0.058, vol=0.14, fade=48)
    if name == "open":
        n = int(sr*0.14)
        t = _np_sfx.linspace(0,0.14,n,endpoint=False)
        w = sum(_np_sfx.sin(2*_np_sfx.pi*f*t) for f in [440,550,660]).astype(_np_sfx.float32)
        env = _np_sfx.ones(n,dtype=_np_sfx.float32); env[-int(sr*0.07):]=_np_sfx.linspace(1,0,int(sr*0.07))
        env[:int(sr*0.006)]=_np_sfx.linspace(0,1,int(sr*0.006))
        return (w/3*env*0.18).astype(_np_sfx.float32)
    if name == "close":    return _mk(520,  0.060, vol=0.15, fade=50)
    if name == "stop":     return _mk(440,  0.070, vol=0.17, fade=60)
    if name == "save":
        n = int(sr*0.18)
        t = _np_sfx.linspace(0,0.18,n,endpoint=False)
        w = sum(_np_sfx.sin(2*_np_sfx.pi*f*t) for f in [523,659,784]).astype(_np_sfx.float32)
        env = _np_sfx.ones(n,dtype=_np_sfx.float32); env[-int(sr*0.08):]=_np_sfx.linspace(1,0,int(sr*0.08))
        env[:int(sr*0.006)]=_np_sfx.linspace(0,1,int(sr*0.006))
        return (w/3*env*0.18).astype(_np_sfx.float32)
    if name == "add":      return _mk(1000, 0.050, vol=0.15, fade=42)
    if name == "delete":   return _mk(400,  0.062, vol=0.15, fade=52)
    if name == "nav":      return _mk(800,  0.045, vol=0.13, fade=38)


_PACK_BUILDERS = {
    "tactical": _build_pack_tactical,
    "military": _build_pack_military,
    "deep":     _build_pack_deep,
    "minimal":  _build_pack_minimal,
    "sharp":    _build_pack_sharp,
    "soft":     _build_pack_soft,
}


def _sfx_build(name: str):
    builder = _PACK_BUILDERS.get(_SFX_PACK_ACTIVE, _build_pack_tactical)
    try:
        arr = builder(name)
        if arr is not None:
            return arr.astype(_np_sfx.float32)
    except Exception as e:
        print(f"[SFX] build error pack='{_SFX_PACK_ACTIVE}' name='{name}': {e}")
    return None


def _sfx(name: str):
    """Воспроизводит UI-звук активного пака. Неблокирующий."""
    try:
        import sounddevice as _sd
        if name not in _SFX_ARRAYS:
            _SFX_ARRAYS[name] = _sfx_build(name)
        arr = _SFX_ARRAYS.get(name)
        if arr is not None:
            _sd.play(arr, samplerate=44100)
    except Exception as e:
        print(f"[SFX] play error '{name}': {e}")

# ══════════════════════ API KEY ENCRYPTION ══════════════════════════
# Ключ шифрования привязан к машине: username + hostname.
# Это не абсолютная защита от целевой атаки на вашу систему,
# но надёжно защищает от утечки файла (скрин, облако, случайный доступ).

def _machine_fernet():
    """Возвращает Fernet-объект с ключом, привязанным к этой машине."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    seed = (os.getenv("USERNAME", "") + socket.gethostname()).encode()
    raw  = hashlib.sha256(seed).digest()          # 32 bytes
    key  = base64.urlsafe_b64encode(raw)          # Fernet требует 32-byte urlsafe-base64
    return Fernet(key)

def _encrypt_key(plain: str) -> str:
    """Шифрует API-ключ. Возвращает зашифрованную строку или '' при ошибке."""
    if not plain:
        return ""
    f = _machine_fernet()
    if f is None:
        return ""
    try:
        return f.encrypt(plain.encode()).decode()
    except Exception:
        return ""

def _decrypt_key(enc: str) -> str:
    """Расшифровывает API-ключ. Возвращает plain string или '' при ошибке."""
    if not enc:
        return ""
    f = _machine_fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(enc.encode()).decode()
    except Exception:
        return ""

# ═══════════════════════════ PALETTE ════════════════════════════════
C_BG0   = QColor(4, 7, 14)
C_BG1   = QColor(8, 13, 26)
C_LINE  = QColor(0, 180, 255, 35)

_DEFAULT_STATE_RGB = {
    "idle":          (0, 180, 255),
    "listening":     (0, 255, 160),
    "user_speaking": (255, 215, 0),
    "processing":    (255, 110, 0),
    "speaking":      (0, 255, 150),
}

def _load_state_rgb_from_cfg():
    """Reads saved status colors from config; falls back to defaults."""
    try:
        import json as _json
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
        if os.path.exists(_p):
            _sc = _json.load(open(_p, encoding="utf-8")).get("status_colors", {})
            result = dict(_DEFAULT_STATE_RGB)
            for _k in result:
                if _k in _sc:
                    try: result[_k] = tuple(int(x) for x in _sc[_k])
                    except Exception: pass
            return result
    except Exception:
        pass
    return dict(_DEFAULT_STATE_RGB)

STATE_RGB = _load_state_rgb_from_cfg()
def get_state_label(state: str) -> str:
    """Returns UI-translated state label (uses in-memory cache — no disk I/O)."""
    key_map = {
        "idle":          "state_idle",
        "listening":     "state_listening",
        "user_speaking": "state_user_speak",
        "processing":    "state_processing",
        "speaking":      "state_speaking",
    }
    key = key_map.get(state, "state_idle")
    return _TRANSLATIONS.get(_UI_LANG_CACHE, _TRANSLATIONS["Русский"]).get(key, state.upper())

# Keep for backward compat (used in a few places below, will be replaced)
STATE_LABELS = {
    "idle":          "ОЖИДАНИЕ",
    "listening":     "СЛУШАЮ",
    "user_speaking": "ГОЛОС",
    "processing":    "ОБРАБОТКА",
    "speaking":      "JARVIS",
}

def _qc(r, g, b, a=255):
    return QColor(int(r), int(g), int(b), max(0, min(255, int(a))))

def state_color(state, alpha=255):
    rgb = STATE_RGB.get(state, (0, 180, 255))
    return _qc(*rgb, alpha)


# ═══════════════════════════ RING ════════════════════════════════════
class RingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # No fixed size — adapts to available space, but capped so it never overflows
        self.setMinimumSize(120, 120)
        self.setMaximumSize(240, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._state  = "idle"
        self._angle  = 0.0
        self._pulse  = 0.5
        self._pdir   = 1
        self._bars   = [0.0] * 32
        self._volume = 0.0

        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(28)

    def set_state(self, s):
        self._state = s

    def set_volume(self, v: float):
        self._volume = max(0.0, min(1.0, v))

    def _tick(self):
        self._angle = (self._angle + 1.4) % 360

        spd = {"idle": .010, "listening": .038, "user_speaking": .055,
               "processing": .060, "speaking": .048}
        self._pulse += spd.get(self._state, .02) * self._pdir
        if self._pulse >= 1.0:  self._pdir = -1
        elif self._pulse <= 0.0: self._pdir =  1

        if self._state == "user_speaking":
            a = 0.08 + self._volume * 0.92
        else:
            amp = {"idle": .06, "listening": .20, "processing": .38, "speaking": .80}
            a = amp.get(self._state, .08)

        for i in range(len(self._bars)):
            self._bars[i] = max(0.0, min(1.0,
                self._bars[i] + (random.random()*a - self._bars[i]*.28)*.45))

        self._volume = max(0.0, self._volume - 0.04)
        self.update()

    def paintEvent(self, _):
        w, h = self.width(), self.height()
        # r is constrained to actual widget size so ring never clips neighbours
        r    = min(w, h) / 2 - 6
        rgb  = STATE_RGB.get(self._state, (0, 180, 255))
        R, G, B = rgb

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(w/2, h/2)

        # Outer ring
        p.setPen(QPen(_qc(R,G,B,25), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0,0), r, r)

        # HUD arcs
        for start, span, al in [
            (0,75,210),(95,45,130),(148,100,170),(258,55,110),(320,28,80)
        ]:
            pen = QPen(_qc(R,G,B,al), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(-r,-r,r*2,r*2),
                      int((start+self._angle)*16), int(span*16))

        # Volume bars
        br0 = r*0.56; br1 = r*0.90
        n = len(self._bars)
        for i, v in enumerate(self._bars):
            ang = math.radians(i*360/n - 90)
            r1  = br0 + (br1-br0)*v
            al  = max(0, min(255, int(70 + 180*v)))
            p.setPen(QPen(_qc(R,G,B,al), 1.6, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(math.cos(ang)*br0, math.sin(ang)*br0),
                       QPointF(math.cos(ang)*r1,  math.sin(ang)*r1))

        # Inner ring
        ir = r*0.48
        p.setPen(QPen(_qc(R,G,B,45), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0,0), ir, ir)

        # Pulsing centre
        pr = ir*0.62*(0.72 + 0.28*self._pulse)
        gr = QRadialGradient(QPointF(0,0), pr)
        gr.setColorAt(0,   _qc(R,G,B, max(0,min(255,int(160*self._pulse)))))
        gr.setColorAt(0.5, _qc(R,G,B, 30))
        gr.setColorAt(1,   _qc(0,0,0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(gr))
        p.drawEllipse(QPointF(0,0), pr, pr)

        # Centre letter
        p.setPen(_qc(R,G,B, max(0,min(255,int(180+70*self._pulse)))))
        f = QFont("Courier New", int(ir*0.56), QFont.Bold)
        p.setFont(f)
        p.drawText(QRectF(-ir,-ir,ir*2,ir*2), Qt.AlignCenter, "J")
        p.end()


# ═══════════════════════════ SCAN LINE ══════════════════════════════
# ScanLine теперь только хранит позицию Y.
# Рисование встроено прямо в paintEvent JarvisOverlay,
# чтобы избежать CompositionMode_Clear артефактов на Windows.
class ScanLine(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self._y = 0
        t = QTimer(self); t.timeout.connect(self._move); t.start(16)

    def _move(self):
        self._y = (self._y + 1) % max(self.height(), 1)
        # Вместо обновления себя — просим родителя перерисовать всё окно.
        # Так scan line рисуется поверх фона без артефактов.
        if self.parent():
            self.parent().update()

    def paintEvent(self, _):
        pass   # Рисование делегировано родителю


# ═══════════════════════════ THEME FX ═══════════════════════════════
# Уникальные фоновые анимации для каждой темы оформления.
# Все эффекты обновляются по таймеру JarvisOverlay._fx_timer (33 fps)
# и рисуются в JarvisOverlay.paintEvent поверх сетки, под/над сканлинией.

# Символы для "цифрового дождя" темы Матрица (катакана + цифры — классика)
_MATRIX_CHARS = "01アイウエオカキクケコサシスセソタチツテト0123456789"

def _hsv_qc(h, s, v, a=255):
    """QColor из HSV (h: 0-359, s/v: 0-255). Используется для радужных эффектов."""
    c = QColor()
    c.setHsv(int(h) % 360, int(s), int(v), int(a))
    return c


# ═══════════════════════════ LOG WIDGET ═════════════════════════════
class LogWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(110)
        self._lines = []

    def add(self, role, text):
        if len(text) > 110: text = text[:107]+"…"
        self._lines.append((role, text))
        if len(self._lines) > 5: self._lines.pop(0)
        self.update()

    def paintEvent(self, _):
        if not self._lines: return
        p  = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h  = self.height()
        lh = h / len(self._lines)
        f  = QFont("Courier New", 9, QFont.Bold)
        p.setFont(f)
        for i, (role, txt) in enumerate(self._lines):
            fade = int(90 + 165*(i+1)/len(self._lines))
            if role == "user":
                col = _qc(0, 229, 255, fade); pre = "▶ USER: "
            else:
                col = _qc(0, 255, 150, fade); pre = "◀ JARVIS: "
            p.setPen(col)
            p.drawText(8, int(i*lh + lh*0.72), pre + txt)
        p.end()


# ═══════════════════════════ RESIZE MIXIN ═══════════════════════════
class ResizableMixin:
    """
    Миксин: добавляет ресайз за все края/углы и перетаскивание.
    Подключается как первый базовый класс перед QWidget.
    Вызывать _resize_init() из __init__ после super().__init__().
    """
    _RM = 4  # px — зона ресайза от края

    def _resize_init(self):
        self._drag          = None
        self._resizing      = False
        self._resize_dir    = None
        self._resize_start_pos  = None
        self._resize_start_geom = None
        self.setMouseTracking(True)
        # Перехватываем события мыши у дочерних виджетов у краёв окна
        self.installEventFilter(self)

    def _install_child_tracking(self):
        """Рекурсивно включает mouseTracking и eventFilter на все дочерние виджеты."""
        for child in self.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def showEvent(self, event):
        super().showEvent(event)
        # Вызываем после show чтобы все дочерние виджеты уже были созданы
        self._install_child_tracking()

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if event.type() == QEvent.MouseMove:
            gpos = event.globalPos()
            lpos = self.mapFromGlobal(gpos)
            d = self._get_resize_dir(lpos)
            if self._resizing:
                self._do_resize(gpos)
                return True
            if d:
                self.setCursor(QCursor(self._cursor_for_dir(d)))
            else:
                self.unsetCursor()
        elif event.type() == QEvent.MouseButtonPress:
            gpos = event.globalPos()
            lpos = self.mapFromGlobal(gpos)
            d = self._get_resize_dir(lpos)
            if d and event.button() == Qt.LeftButton:
                self._resizing          = True
                self._resize_dir        = d
                self._resize_start_pos  = gpos
                self._resize_start_geom = self.geometry()
                return True
        elif event.type() == QEvent.MouseButtonRelease:
            if self._resizing:
                self._drag       = None
                self._resizing   = False
                self._resize_dir = None
                self.unsetCursor()
                return True
        return False

    # ── helpers ──────────────────────────────────────────────────────
    def _get_resize_dir(self, pos):
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        m = self._RM
        L = x < m;  R = x > w - m
        T = y < m;  B = y > h - m
        if T and L: return "tl"
        if T and R: return "tr"
        if B and L: return "bl"
        if B and R: return "br"
        if L:       return "l"
        if R:       return "r"
        if T:       return "t"
        if B:       return "b"
        return None

    def _cursor_for_dir(self, d):
        return {
            "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
            "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
            "l":  Qt.SizeHorCursor,   "r":  Qt.SizeHorCursor,
            "t":  Qt.SizeVerCursor,   "b":  Qt.SizeVerCursor,
        }.get(d, Qt.ArrowCursor)

    def _do_resize(self, gpos):
        # Always compute delta from the original press position — never re-anchor.
        # This lets the window shrink freely when the cursor moves inward.
        dx = gpos.x() - self._resize_start_pos.x()
        dy = gpos.y() - self._resize_start_pos.y()
        g  = self._resize_start_geom
        mn_w = max(self.minimumWidth(), 1)
        mn_h = max(self.minimumHeight(), 1)
        d = self._resize_dir

        x, y, w, h = g.x(), g.y(), g.width(), g.height()

        if "r" in d:
            w = max(mn_w, g.width() + dx)
        if "b" in d:
            h = max(mn_h, g.height() + dy)
        if "l" in d:
            nw = max(mn_w, g.width() - dx)
            x  = g.x() + g.width() - nw
            w  = nw
        if "t" in d:
            nh = max(mn_h, g.height() - dy)
            y  = g.y() + g.height() - nh
            h  = nh

        cur = self.geometry()
        if cur.x() == x and cur.y() == y and cur.width() == w and cur.height() == h:
            return
        self.setGeometry(x, y, w, h)

    # ── Qt events ────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            d = self._get_resize_dir(e.pos())
            if d:
                self._resizing          = True
                self._resize_dir        = d
                self._resize_start_pos  = e.globalPos()
                self._resize_start_geom = self.geometry()
            else:
                self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._resizing and e.buttons() == Qt.LeftButton:
            self._do_resize(e.globalPos())
            # Do NOT re-anchor here — delta must stay relative to original press point
            return
        if e.buttons() == Qt.LeftButton and self._drag:
            self.move(e.globalPos() - self._drag)
            return
        d = self._get_resize_dir(e.pos())
        self.setCursor(QCursor(self._cursor_for_dir(d) if d else Qt.ArrowCursor))

    def mouseReleaseEvent(self, _):
        self._drag       = None
        self._resizing   = False
        self._resize_dir = None
        self.setCursor(QCursor(Qt.ArrowCursor))


# ═══════════════════════════ SETTINGS WINDOW ════════════════════════
_FIELD_SS = """
    QLineEdit, QComboBox {
        background: #03080f;
        border: 1px solid rgba(0,200,255,140);
        border-radius: 4px;
        color: #00eeff;
        padding: 3px 8px;
        font-family: 'Courier New';
        font-size: 13px;
    }
    QLineEdit:focus, QComboBox:focus { border: 1px solid #00ffff; }
    QComboBox QAbstractItemView {
        background: #03080f;
        color: #00eeff;
        font-size: 13px;
        selection-background-color: rgba(0,180,255,80);
    }
    QComboBox::drop-down { border: none; }
    QComboBox::down-arrow { width: 14px; }
"""

_LABEL_SS  = "color: #c8eeff; font-family: 'Courier New'; font-size: 13px; font-weight: bold;"
_SEC_SS    = "color: #00ffff; font-family: 'Courier New'; font-size: 13px; font-weight: bold; letter-spacing: 1px; margin-top: 6px;"
_CHECK_SS  = """
    QCheckBox { color: #c8eeff; font-family: 'Courier New'; font-size: 13px; }
    QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #00ffff;
        border-radius: 4px; background: #050c1c; }
    QCheckBox::indicator:checked { background: #00ffff; }
"""


# ── path for persistent commands storage ────────────────────────────
_COMMANDS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_commands.json")
_CONFIG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
_CHAT_LOG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_chat_log.json")

def _load_config():
    if os.path.exists(_CONFIG_FILE):
        try:
            return json.load(open(_CONFIG_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_config(data: dict):
    try:
        existing = _load_config()
        existing.update(data)
        json.dump(existing, open(_CONFIG_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[UI] config save error: {e}")


def _load_commands():
    if os.path.exists(_COMMANDS_FILE):
        try:
            return json.load(open(_COMMANDS_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"custom_wake_word": None, "commands": []}

def _save_commands(data):
    try:
        json.dump(data, open(_COMMANDS_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[UI] commands save error: {e}")


# ── Chat log: persistent storage of ALL messages ─────────────────────────
import datetime as _dt
_chat_log_lock = threading.Lock()

def _append_chat_log(role: str, text: str):
    """
    Дописывает запись в jarvis_chat_log.json.
    Файл содержит список словарей: [{role, text, timestamp}, ...]
    Максимум 2000 записей — старые автоматически обрезаются.
    """
    entry = {
        "role":      role,
        "text":      text,
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _chat_log_lock:
        try:
            if os.path.exists(_CHAT_LOG_FILE):
                with open(_CHAT_LOG_FILE, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                try:
                    log = json.loads(raw) if raw else []
                except Exception:
                    log = []  # файл повреждён — начинаем заново
                if not isinstance(log, list):
                    log = []
            else:
                log = []
            log.append(entry)
            # Ограничиваем размер
            if len(log) > 2000:
                log = log[-2000:]
            with open(_CHAT_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[UI] chat log save error: {e}")


def _get_jarvis_dir() -> str:
    """Возвращает путь к папке Jarvis (рядом со скриптом). Создаёт если нет."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Jarvis")
    os.makedirs(folder, exist_ok=True)
    return folder

_DIALOGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Jarvis", "jarvis_dialogs.txt")

def _save_session_txt():
    """
    Дописывает переписку ТЕКУЩЕЙ сессии в конец jarvis_dialogs.txt.
    Новые записи добавляются начиная с той, что ещё не была записана.
    При каждом запуске добавляем только новые сообщения (сравниваем по числу уже записанных).
    """
    try:
        if not os.path.exists(_CHAT_LOG_FILE):
            return
        with open(_CHAT_LOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
        if not isinstance(log, list) or not log:
            return

        # Считаем сколько записей уже есть в txt (по маркерам строк «[timestamp]»)
        _get_jarvis_dir()  # создаём папку если нет
        already = 0
        if os.path.exists(_DIALOGS_FILE):
            with open(_DIALOGS_FILE, "r", encoding="utf-8") as f:
                already = sum(1 for line in f if line.startswith("[20"))

        new_entries = log[already:]
        if not new_entries:
            return

        with open(_DIALOGS_FILE, "a", encoding="utf-8") as f:
            # Разделитель сессии — только если это первая запись этого запуска
            if already == 0:
                f.write("=" * 60 + "\n")
                f.write(f"  JARVIS DIALOGS LOG\n")
                f.write("=" * 60 + "\n\n")
            else:
                f.write(f"\n── Сессия {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} ──\n\n")

            for entry in new_entries:
                role  = entry.get("role", "?")
                text  = entry.get("text", "")
                ts_e  = entry.get("timestamp", "")
                prefix = "▶ ВЫ" if role == "user" else "◀ JARVIS"
                f.write(f"[{ts_e}]  {prefix}\n")
                f.write(f"  {text}\n\n")

        print(f"[UI] Диалоги сохранены: {_DIALOGS_FILE} (+{len(new_entries)} записей)")
    except Exception as e:
        print(f"[UI] Ошибка сохранения диалогов: {e}")


def _load_history_from_txt() -> list:
    """
    Читает jarvis_chat_log.json и возвращает список (role, text, timestamp).
    Берём только реальные диалоги пользователь/jarvis (не системные строки).
    """
    result = []
    try:
        if not os.path.exists(_CHAT_LOG_FILE):
            return result
        with open(_CHAT_LOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
        if not isinstance(log, list):
            return result
        for entry in log:
            role = entry.get("role", "")
            text = entry.get("text", "")
            ts   = entry.get("timestamp", "")
            if role in ("user", "jarvis") and text:
                result.append((role, text, ts))
    except Exception:
        pass
    return result


# ═══════════════════════════ UI TRANSLATIONS ════════════════════════
_UI_LANG = "Русский"  # текущий язык интерфейса (глобальный)

_TRANSLATIONS = {
    "Русский": {
        # SettingsWindow
        "settings_title":    "⚙  JARVIS  CONFIG",
        "tab_settings":      "⚙  НАСТРОЙКИ",
        "tab_commands":      "🎮  КОМАНДЫ",
        "tab_custom":        "🎨  КАСТОМИЗАЦИЯ",
        "custom_locked":     "🔒  КАСТОМИЗАЦИЯ",
        "lbl_access_code":   "Код доступа к кастомизации",
        "ph_access_code":    "Введите код...",
        "custom_wrong_code": "Неверный код доступа.",
        "custom_unlocked":   "✅ Кастомизация разблокирована!",
        "sec_custom_ui":     "🎨  ВНЕШНИЙ ВИД",
        "lbl_accent_color":  "Акцентный цвет",
        "lbl_window_opacity":"Прозрачность окна (%)",
        "lbl_ring_size":     "Размер кольца",
        "lbl_font_size":     "Размер шрифта HUD",
        "save_btn":          "  СОХРАНИТЬ КОНФИГ  ",
        # Section headers
        "sec_ai_lang":       "🌐  AI & LANGUAGE",
        "sec_voice":         "🎙  VOICE & SYNTHESIS",
        "sec_audio":         "🔊  AUDIO & CAPTURE",
        "sec_neural":        "🤖  NEURAL NETWORK",
        "sec_hud":           "🖥  HUD DISPLAY",
        # Labels
        "lbl_api_key":       "API ключ Gemini",
        "lbl_api_ph":        "Вставьте ваш Gemini API key...",
        "lbl_ai_lang":       "Язык ИИ",
        "lbl_ui_lang":       "Язык интерфейса",
        "lbl_voice_rate":    "Скорость речи",
        "lbl_voice_pitch":   "Тональность голоса",
        "lbl_voice_en":      "Голос EN",
        "lbl_voice_ru":      "Голос RU",
        "lbl_mic_index":     "Индекс микрофона",
        "lbl_threshold":     "Порог громкости",
        "lbl_silence":       "Лимит тишины (чанки)",
        "lbl_model":         "Gemini Model",
        "lbl_temp":          "Температура (0..1)",
        "lbl_history":       "История (сообщений)",
        "lbl_always_top":    "Всегда поверх окон",
        "lbl_show_log":      "Показывать лог HUD",
        # Commands page
        "sec_wakeword":      "🎙  КОМАНДА ПРОБУЖДЕНИЯ",
        "btn_record":        "⏺ Запись",
        "btn_save_ww":       "💾 Сохранить",
        "btn_reset_ww":      "⟲  По умолчанию  (hey jarvis)",
        "hint_ww":           "Введите текст ИЛИ нажмите ⏺ и произнесите фразу (3 сек)",
        "ww_placeholder":    "hey jarvis  (текст или запись голоса)",
        "sec_custom_cmds":   "🎮  ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ",
        "btn_add_cmd":       "  ＋  ДОБАВИТЬ КОМАНДУ",
        "lbl_cmd_name":      "Имя команды:",
        "lbl_cmd_trigger":   "Триггер фраза:",
        "lbl_cmd_actions":   "Действия  (путь к программе / папке / URL  +  задержка мс):",
        "btn_add_action":    "  ＋  добавить действие",
        "btn_save_cmd":      "  💾  СОХРАНИТЬ КОМАНДУ",
        "lbl_delay":         "задержка:",
        "lbl_ms":            "мс",
        "path_placeholder":  "C:\\Program.exe  или  https://...",
        "trigger_placeholder": "текст или голосовая запись...",
        "new_cmd_name":      "Новая команда",
        # State labels
        "state_idle":        "ОЖИДАНИЕ",
        "state_listening":   "СЛУШАЮ",
        "state_user_speak":  "ГОВОРИТЕ",
        "state_processing":  "ОБРАБОТКА",
        "state_speaking":    "JARVIS",
        "txt_idle":          "Ожидание команды",
        "txt_greeting":      "Слушаю...",
        "txt_listening":     "Говорите, я слушаю...",
        "txt_processing":    "Обрабатываю запрос...",
        "txt_clarifying":    "Уточняю...",
        "txt_speak_cmd":     "Говорите: вкладку / команду...",
        # Overlay tooltips
        "tip_settings":      "Настройки",
        "tip_history":       "История разговора",
        "tip_collapse":      "Свернуть",
        "tip_close":         "Закрыть",
        # Recording
        "recording_3sec":    "⏺ Запись 3 сек...",
        "error_prefix":      "Ошибка: ",
    },
    "English": {
        "settings_title":    "⚙  JARVIS  CONFIG",
        "tab_settings":      "⚙  SETTINGS",
        "tab_commands":      "🎮  COMMANDS",
        "tab_custom":        "🎨  CUSTOMIZATION",
        "custom_locked":     "🔒  CUSTOMIZATION",
        "lbl_access_code":   "Customization access code",
        "ph_access_code":    "Enter code...",
        "custom_wrong_code": "Wrong access code.",
        "custom_unlocked":   "✅ Customization unlocked!",
        "sec_custom_ui":     "🎨  APPEARANCE",
        "lbl_accent_color":  "Accent color",
        "lbl_window_opacity":"Window opacity (%)",
        "lbl_ring_size":     "Ring size",
        "lbl_font_size":     "HUD font size",
        "save_btn":          "  SAVE CONFIG  ",
        "sec_ai_lang":       "🌐  AI & LANGUAGE",
        "sec_voice":         "🎙  VOICE & SYNTHESIS",
        "sec_audio":         "🔊  AUDIO & CAPTURE",
        "sec_neural":        "🤖  NEURAL NETWORK",
        "sec_hud":           "🖥  HUD DISPLAY",
        "lbl_api_key":       "Gemini API Key",
        "lbl_api_ph":        "Paste your Gemini API key...",
        "lbl_ai_lang":       "AI Language",
        "lbl_ui_lang":       "Interface Language",
        "lbl_voice_rate":    "Speech Rate",
        "lbl_voice_pitch":   "Voice Pitch",
        "lbl_voice_en":      "EN Voice",
        "lbl_voice_ru":      "RU Voice",
        "lbl_mic_index":     "Microphone Index",
        "lbl_threshold":     "Volume Threshold",
        "lbl_silence":       "Silence Limit (chunks)",
        "lbl_model":         "Gemini Model",
        "lbl_temp":          "Temperature (0..1)",
        "lbl_history":       "History (messages)",
        "lbl_always_top":    "Always on Top",
        "lbl_show_log":      "Show HUD Log",
        "sec_wakeword":      "🎙  WAKE WORD",
        "btn_record":        "⏺ Record",
        "btn_save_ww":       "💾 Save",
        "btn_reset_ww":      "⟲  Default  (hey jarvis)",
        "hint_ww":           "Type text OR press ⏺ and say a phrase (3 sec)",
        "ww_placeholder":    "hey jarvis  (text or voice recording)",
        "sec_custom_cmds":   "🎮  CUSTOM COMMANDS",
        "btn_add_cmd":       "  ＋  ADD COMMAND",
        "lbl_cmd_name":      "Command Name:",
        "lbl_cmd_trigger":   "Trigger Phrase:",
        "lbl_cmd_actions":   "Actions  (path to program / folder / URL  +  delay ms):",
        "btn_add_action":    "  ＋  add action",
        "btn_save_cmd":      "  💾  SAVE COMMAND",
        "lbl_delay":         "delay:",
        "lbl_ms":            "ms",
        "path_placeholder":  "C:\\Program.exe  or  https://...",
        "trigger_placeholder": "text or voice recording...",
        "new_cmd_name":      "New Command",
        "state_idle":        "IDLE",
        "state_listening":   "LISTENING",
        "state_user_speak":  "SPEAK",
        "state_processing":  "PROCESSING",
        "state_speaking":    "JARVIS",
        "txt_idle":          "Awaiting command",
        "txt_greeting":      "Listening...",
        "txt_listening":     "Speak, I'm listening...",
        "txt_processing":    "Processing request...",
        "txt_clarifying":    "Clarifying...",
        "txt_speak_cmd":     "Speak: tab / command...",
        "tip_settings":      "Settings",
        "tip_history":       "Conversation history",
        "tip_collapse":      "Minimize",
        "tip_close":         "Close",
        "recording_3sec":    "⏺ Recording 3 sec...",
        "error_prefix":      "Error: ",
    },
    "Հայերեն": {
        "settings_title":    "⚙  JARVIS  ԿԱՐԳԱՎՈՐՈՒՄ",
        "tab_settings":      "⚙  ԿԱՐԳԱՎՈՐՈՒՄ",
        "tab_commands":      "🎮  ՀՐԱՄԱՆՆԵՐ",
        "tab_custom":        "🎨  ՀԱՐՄԱՐԵՑՈՒՄ",
        "custom_locked":     "🔒  ՀԱՐՄԱՐԵՑՈՒՄ",
        "lbl_access_code":   "Մուտքի կոդ",
        "ph_access_code":    "Մուտքագրեք կոդ...",
        "custom_wrong_code": "Սխալ մուտքի կոդ։",
        "custom_unlocked":   "✅ Հարմարեցումը բացված է!",
        "sec_custom_ui":     "🎨  ՏԵՍՔ",
        "lbl_accent_color":  "Շեշտի գույն",
        "lbl_window_opacity":"Պատուհանի թափանցիկություն (%)",
        "lbl_ring_size":     "Օղակի չափ",
        "lbl_font_size":     "HUD տառաչափ",
        "save_btn":          "  ՊԱՀԵԼ ԿՈՆՖԻԳ  ",
        "sec_ai_lang":       "🌐  AI & ԼԵԶՈՒ",
        "sec_voice":         "🎙  ՁԱՅՆ & ՍԻՆԹԵԶ",
        "sec_audio":         "🔊  ԱՈՒԴԻՈ & ՖԱՅԼ",
        "sec_neural":        "🤖  ՆԵՅՐՈՆ ՑԱՆՑ",
        "sec_hud":           "🖥  HUD ՑՈՒՑԱԴՐՈՒՄ",
        "lbl_api_key":       "Gemini API բանալի",
        "lbl_api_ph":        "Տեղադրեք Gemini API բանալին...",
        "lbl_ai_lang":       "ԱԻ Լեզու",
        "lbl_ui_lang":       "Ինտերֆեյսի Լեզու",
        "lbl_voice_rate":    "Խոսքի արագություն",
        "lbl_voice_pitch":   "Ձայնի բարձրություն",
        "lbl_voice_en":      "EN Ձայն",
        "lbl_voice_ru":      "RU Ձայն",
        "lbl_mic_index":     "Մանրախտ ինդեքս",
        "lbl_threshold":     "Ձայնի շեմ",
        "lbl_silence":       "Լռության սահման (chunk)",
        "lbl_model":         "Gemini Մոդել",
        "lbl_temp":          "Ջերմաստիճան (0..1)",
        "lbl_history":       "Պատմություն (հաղ.)",
        "lbl_always_top":    "Միշտ վերևում",
        "lbl_show_log":      "Ցույց տալ HUD գրանցամատյան",
        "sec_wakeword":      "🎙  ԱՐԹՆԱՑՄԱՆ ՀՐԱՄԱՆ",
        "btn_record":        "⏺ Ձայնագրել",
        "btn_save_ww":       "💾 Պահել",
        "btn_reset_ww":      "⟲  Կանխադրված  (hey jarvis)",
        "hint_ww":           "Մուտքագրեք տեքստ ԿԱՄ սեղմեք ⏺ և ասեք արտահայտություն (3 վ.)",
        "ww_placeholder":    "hey jarvis  (տեքստ կամ ձայնագրություն)",
        "sec_custom_cmds":   "🎮  ՀԱՏՈՒԿ ՀՐԱՄԱՆՆԵՐ",
        "btn_add_cmd":       "  ＋  ԱՎԵԼԱՑՆԵԼ ՀՐԱՄԱՆ",
        "lbl_cmd_name":      "Հրամանի անուն:",
        "lbl_cmd_trigger":   "Ակտիվացնող արտահայտություն:",
        "lbl_cmd_actions":   "Գործողություններ (ծրագրի ճանապարհ / URL + ուշացում մ/վ):",
        "btn_add_action":    "  ＋  ավելացնել գործողություն",
        "btn_save_cmd":      "  💾  ՊԱՀԵԼ ՀՐԱՄԱՆԸ",
        "lbl_delay":         "ուշացում:",
        "lbl_ms":            "մ/վ",
        "path_placeholder":  "C:\\Program.exe  կամ  https://...",
        "trigger_placeholder": "տեքստ կամ ձայնագրություն...",
        "new_cmd_name":      "Նոր հրաման",
        "state_idle":        "ՍՊԱՍՈՒՄ",
        "state_listening":   "ԼՍՈՒՄ ԵՄ",
        "state_user_speak":  "ՁԱՅՆ",
        "state_processing":  "ՄՇԱԿՈՒՄ",
        "state_speaking":    "JARVIS",
        "txt_idle":          "Սպասում հրամանի",
        "txt_greeting":      "Լսում եմ...",
        "txt_listening":     "Խոսեք, ես լսում եմ...",
        "txt_processing":    "Մшакum em...",
        "txt_clarifying":    "Парзнелу em...",
        "txt_speak_cmd":     "Асек. Клах / храмане...",
        "tip_settings":      "Կարգավորումներ",
        "tip_history":       "Զրույցի պատմություն",
        "tip_collapse":      "Ծալել",
        "tip_close":         "Փակել",
        "recording_3sec":    "⏺ Ձայնագրություն 3 վ...",
        "error_prefix":      "Սխալ: ",
    },
}


_UI_LANG_CACHE = "Русский"   # in-memory cache — updated only on save/recreate, no disk I/O

def _t(key: str) -> str:
    """Get translated string for current UI language (uses in-memory cache — no disk I/O)."""
    return _TRANSLATIONS.get(_UI_LANG_CACHE, _TRANSLATIONS["Русский"]).get(
        key, _TRANSLATIONS["Русский"].get(key, key)
    )

def _set_ui_lang_cache(lang: str):
    """Update the in-memory UI language cache."""
    global _UI_LANG_CACHE
    _UI_LANG_CACHE = lang if lang in _TRANSLATIONS else "Русский"

# Initialise cache from saved config on module load (one disk read, then no more)
_set_ui_lang_cache(_load_config().get("ui_language", "Русский"))

# Загружаем активный звуковой пак из конфига
_SFX_PACK_ACTIVE = _load_config().get("sfx_pack", "tactical")

# ── Settings-saved callback registry ──────────────────────────────────────
# main_app.py calls jarvis_ui.register_settings_callback(fn) once at startup.
# fn(cfg: dict) is called every time the user clicks "Сохранить" in settings.
# This lets main_app hot-reload all globals without a restart.
_settings_callbacks: list = []

def register_settings_callback(fn):
    """Register a function to be called with the full cfg dict on each save."""
    if fn not in _settings_callbacks:
        _settings_callbacks.append(fn)

def t(key: str) -> str:
    """Public accessor for the translation table. Safe to call from any thread."""
    return _t(key)


class NoScrollComboBox(QComboBox):
    """ComboBox that ignores scroll wheel unless it has keyboard focus."""
    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class SettingsWindow(ResizableMixin, QWidget):
    sig_saved = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.resize(480, 520)
        self.setMinimumSize(320, 260)
        self._resize_init()          # ResizableMixin setup
        self._fields = {}
        self._build()
        self._load_saved_values()

    def _build(self):
        # ── Outer structure ──────────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────
        hdr_widget = QWidget()
        hdr_widget.setStyleSheet("background: transparent;")
        hdr_outer = QVBoxLayout(hdr_widget)
        hdr_outer.setContentsMargins(20, 16, 20, 8)
        hdr_outer.setSpacing(0)

        hdr = QHBoxLayout()
        t = QLabel(_t("settings_title"))
        t.setFont(QFont("Courier New", 16, QFont.Bold))
        t.setStyleSheet("color: #00ffff; letter-spacing: 3px;")
        hdr.addWidget(t); hdr.addStretch()

        bc = QPushButton("✕")
        bc.setFixedSize(28, 28)
        bc.setStyleSheet("""
            QPushButton { background: rgba(255,60,80,25); border: 1px solid rgba(255,60,80,90);
                border-radius: 4px; color: #ff4455; font-family:'Courier New';
                font-weight:bold; font-size:18px; }
            QPushButton:hover { background: #ff3344; color: white; }
        """)
        bc.clicked.connect(lambda: _sfx("close"))
        bc.clicked.connect(self.hide)
        hdr.addWidget(bc)
        hdr_outer.addLayout(hdr)
        hdr_outer.addWidget(self._div())
        outer.addWidget(hdr_widget)

        # ── Tab bar ────────────────────────────────────────────────────
        TAB_SS = """
            QPushButton {
                background: rgba(0,150,220,15); border: none;
                border-bottom: 2px solid rgba(0,180,255,40);
                color: #7ab8d4; font-family: 'Courier New'; font-size: 14px;
                font-weight: bold; padding: 8px 24px; letter-spacing: 2px;
            }
            QPushButton:checked {
                background: rgba(0,180,255,20); border-bottom: 2px solid #00ffff;
                color: #00ffff;
            }
            QPushButton:hover:!checked { color: #c8eeff; background: rgba(0,150,220,25); }
            QPushButton:disabled { color: rgba(100,120,140,160); }
        """
        tab_bar = QHBoxLayout()
        tab_bar.setContentsMargins(16, 0, 16, 0)
        tab_bar.setSpacing(0)
        self._tab1_btn = QPushButton(_t("tab_settings"))
        self._tab2_btn = QPushButton(_t("tab_commands"))
        self._tab3_btn = QPushButton(_t("custom_locked"))   # locked by default
        for b in (self._tab1_btn, self._tab2_btn, self._tab3_btn):
            b.setCheckable(True)
            b.setStyleSheet(TAB_SS)
        self._tab3_btn.setEnabled(False)   # locked until correct code entered
        self._tab1_btn.setChecked(True)
        self._tab1_btn.clicked.connect(lambda: (_sfx("tab"), self._switch_tab(0)))
        self._tab2_btn.clicked.connect(lambda: (_sfx("tab"), self._switch_tab(1)))
        self._tab3_btn.clicked.connect(lambda: (_sfx("tab"), self._switch_tab(2)))
        tab_bar.addWidget(self._tab1_btn)
        tab_bar.addWidget(self._tab2_btn)
        tab_bar.addWidget(self._tab3_btn)
        tab_bar.addStretch()
        tab_widget = QWidget()
        tab_widget.setLayout(tab_bar)
        tab_widget.setStyleSheet("background: transparent;")
        outer.addWidget(tab_widget)
        outer.addWidget(self._div())

        # ── Stacked pages ─────────────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        outer.addWidget(self._stack)

        # PAGE 0 — Settings
        self._stack.addWidget(self._build_settings_page())
        # PAGE 1 — Commands
        self._stack.addWidget(self._build_commands_page())
        # PAGE 2 — Customization (locked)
        self._stack.addWidget(self._build_custom_page())

    def _switch_tab(self, idx):
        self._stack.setCurrentIndex(idx)
        self._tab1_btn.setChecked(idx == 0)
        self._tab2_btn.setChecked(idx == 1)
        self._tab3_btn.setChecked(idx == 2)

    def _build_settings_page(self):
        page = QWidget(); page.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(page); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        # scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background:#03080f; width:8px; border-radius:4px; }
            QScrollBar::handle:vertical { background:rgba(0,180,255,120); border-radius:4px; min-height:20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """)
        content = QWidget(); content.setStyleSheet("background: transparent;")
        root = QVBoxLayout(content); root.setContentsMargins(20,10,20,16); root.setSpacing(6)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        
        # ── AI & Language ─────────────────────────────────────────
        root.addWidget(self._sec(_t("sec_ai_lang")))

        root.addWidget(self._lbl(_t("lbl_api_key")))
        api_field = QLineEdit()
        api_field.setPlaceholderText(_t("lbl_api_ph"))
        api_field.setEchoMode(QLineEdit.Password)
        api_field.setStyleSheet(_FIELD_SS)
        api_field.setMinimumHeight(28)
        root.addWidget(api_field)
        self._fields["api_key"] = api_field

        row_ai = QHBoxLayout(); row_ai.setSpacing(12)
        row_ai.addWidget(self._lbl(_t("lbl_ai_lang")), 2)
        ai_lang = NoScrollComboBox()
        ai_lang.addItems(["Русский", "English"])
        ai_lang.setStyleSheet(_FIELD_SS)
        row_ai.addWidget(ai_lang, 3)
        self._fields["ai_language"] = ai_lang
        root.addLayout(row_ai)

        row_ui = QHBoxLayout(); row_ui.setSpacing(12)
        row_ui.addWidget(self._lbl(_t("lbl_ui_lang")), 2)
        ui_lang = NoScrollComboBox()
        ui_lang.addItems(["Русский", "English", "Հայերեն"])
        ui_lang.setStyleSheet(_FIELD_SS)
        # Connect UI language change to immediate rebuild
        ui_lang.currentTextChanged.connect(self._on_ui_lang_changed)
        row_ui.addWidget(ui_lang, 3)
        self._fields["ui_language"] = ui_lang
        root.addLayout(row_ui)
        root.addWidget(self._div())

        # ── Voice & Synthesis ────────────────────────────────────
        root.addWidget(self._sec(_t("sec_voice")))
        self._rows(root, [
            ("voice_rate",    _t("lbl_voice_rate"),    ("+15%", ["+0%", "+5%", "+10%", "+15%", "+20%", "+30%", "+50%"])),
            ("voice_pitch",   _t("lbl_voice_pitch"),   ("0.0", ["0.0", "0.5", "1.0", "1.5", "-0.5", "-1.0"])),
        ])
        root.addWidget(self._div())

        # ── Audio & Capture ──────────────────────────────────────
        root.addWidget(self._sec(_t("sec_audio")))
        self._rows(root, [
            ("mic_index",     _t("lbl_mic_index"),     ("0",    ["0", "1", "2", "3", "4"])),
            ("threshold",     _t("lbl_threshold"),      ("12.0", ["8.0", "10.0", "12.0", "15.0", "20.0", "25.0"])),
            ("silence_limit", _t("lbl_silence"),       ("40",   ["20", "30", "40", "50", "60", "80", "100"])),
        ])
        root.addWidget(self._div())

        # ── Neural Network ───────────────────────────────────────
        root.addWidget(self._sec(_t("sec_neural")))
        self._rows(root, [
            ("model_id", _t("lbl_model"), [
                "gemini-2.5-flash-lite",
                "gemini-2.5-flash",
                "gemini-2.5-flash-preview-05-20",
                "gemini-2.5-pro",
                "gemini-2.5-pro-preview-06-05",
            ]),
            ("temperature", _t("lbl_temp"),    ("0.7", ["0.0", "0.3", "0.5", "0.7", "0.9", "1.0"])),
            ("max_history", _t("lbl_history"), ("20",  ["5", "10", "15", "20", "30", "50"])),
        ])
        root.addWidget(self._div())

        # ── HUD Display ──────────────────────────────────────────
        root.addWidget(self._sec(_t("sec_hud")))
        self._rows(root, [
            ("always_on_top", _t("lbl_always_top"), True),
            ("show_log",      _t("lbl_show_log"),   True),
        ])
        root.addWidget(self._div())

        # ── Код доступа к кастомизации ────────────────────────────
        root.addWidget(self._sec(_t("lbl_access_code")))
        acc_row = QHBoxLayout(); acc_row.setSpacing(8)
        self._access_code_field = QLineEdit()
        self._access_code_field.setPlaceholderText(_t("ph_access_code"))
        self._access_code_field.setEchoMode(QLineEdit.Password)
        self._access_code_field.setStyleSheet(_FIELD_SS)
        self._access_code_field.setMinimumHeight(28)
        acc_row.addWidget(self._access_code_field)
        self._access_code_hint = QLabel("")
        self._access_code_hint.setStyleSheet(
            "color: #00ff88; font-family:'Courier New'; font-size:11px;")
        acc_row.addWidget(self._access_code_hint)
        root.addLayout(acc_row)
        root.addWidget(self._div())

        br = QHBoxLayout(); br.addStretch()

        # ── Сброс по умолчанию ───────────────────────────────────────
        bd = QPushButton("⟲  ПО УМОЛЧАНИЮ")
        bd.setFixedHeight(34)
        bd.setStyleSheet("""
            QPushButton { background: rgba(255,100,0,20); border: 1px solid rgba(255,140,0,120);
                border-radius: 4px; color: #ff8800; font-family:'Courier New';
                font-size: 12px; font-weight: bold; padding: 0 14px; }
            QPushButton:hover { background: rgba(255,140,0,180); color: #04070e; }
        """)
        bd.clicked.connect(lambda: _sfx("nav"))
        bd.clicked.connect(self._reset_settings)
        br.addWidget(bd)
        br.addSpacing(8)

        bs = QPushButton(_t("save_btn"))
        bs.setFixedHeight(34)
        bs.setStyleSheet("""
            QPushButton { background: rgba(0,150,220,30); border: 1px solid #00ffff;
                border-radius: 4px; color: #ffffff; font-family:'Courier New';
                font-size: 13px; font-weight: bold; padding: 0 16px; }
            QPushButton:hover { background: #00ffff; color: #04070e; }
        """)
        bs.clicked.connect(lambda: _sfx("save"))
        bs.clicked.connect(self._save)
        br.addWidget(bs)
        root.addLayout(br)
        return page

    # ── COMMANDS PAGE ─────────────────────────────────────────────────
    def _build_commands_page(self):
        page = QWidget(); page.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(page); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background:#03080f; width:8px; border-radius:4px; }
            QScrollBar::handle:vertical { background:rgba(0,180,255,120); border-radius:4px; min-height:20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """)
        content = QWidget(); content.setStyleSheet("background: transparent;")
        root = QVBoxLayout(content); root.setContentsMargins(20,10,20,16); root.setSpacing(8)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # ── Wake word section ─────────────────────────────────────────
        root.addWidget(self._sec(_t("sec_custom_cmds")))

        add_btn = QPushButton(_t("btn_add_cmd"))
        add_btn.setFixedHeight(40)
        add_btn.setStyleSheet(self._btn_ss("#00ffff", "rgba(0,150,220,20)"))
        add_btn.clicked.connect(lambda: _sfx("add"))
        add_btn.clicked.connect(self._add_command)
        root.addWidget(add_btn)

        # Container for command cards
        self._cmd_container = QWidget(); self._cmd_container.setStyleSheet("background: transparent;")
        self._cmd_layout = QVBoxLayout(self._cmd_container)
        self._cmd_layout.setContentsMargins(0,6,0,0)
        self._cmd_layout.setSpacing(10)
        root.addWidget(self._cmd_container)
        root.addStretch()

        # Load existing commands
        ww_data = _load_commands()
        self._cmd_cards = []
        for cmd in ww_data.get("commands", []):
            self._add_command_card(cmd)

        return page

    # ── Command card helpers ──────────────────────────────────────────
    def _btn_ss(self, fg, bg):
        return (f"QPushButton {{ background:{bg}; border:1px solid {fg}; border-radius:4px; "
                f"color:{fg}; font-family:'Courier New'; font-size:13px; font-weight:bold; padding:2px 10px; }}"
                f"QPushButton:hover {{ background:{fg}; color:#04070e; }}")

    def _add_command(self):
        cmd = {"id": str(uuid.uuid4()), "name": _t("new_cmd_name"),
               "trigger": "", "trigger_type": "text", "actions": []}
        self._add_command_card(cmd)

    def _add_command_card(self, cmd):
        card = QFrame()
        card.setStyleSheet("""
            QFrame { background: rgba(0,30,60,180); border: 1px solid rgba(0,180,255,80);
                border-radius: 6px; }
        """)
        vl = QVBoxLayout(card); vl.setContentsMargins(12,10,12,10); vl.setSpacing(6)

        # Row 1: name + delete
        r1 = QHBoxLayout(); r1.setSpacing(8)
        name_lbl = QLabel(_t("lbl_cmd_name"))
        name_lbl.setStyleSheet("color:#7ab8d4; font-family:'Courier New'; font-size:13px;")
        r1.addWidget(name_lbl)
        name_f = QLineEdit(cmd.get("name",""))
        name_f.setStyleSheet(_FIELD_SS)
        name_f.setFixedHeight(34)
        r1.addWidget(name_f, 1)
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(30, 30)
        del_btn.setStyleSheet(self._btn_ss("#ff4455", "rgba(255,60,80,20)"))
        del_btn.clicked.connect(lambda _, c=card: (_sfx("delete"), self._delete_card(c)))
        r1.addWidget(del_btn)
        vl.addLayout(r1)

        # Row 2: trigger phrase + record
        r2 = QHBoxLayout(); r2.setSpacing(8)
        trig_lbl = QLabel(_t("lbl_cmd_trigger"))
        trig_lbl.setStyleSheet("color:#7ab8d4; font-family:'Courier New'; font-size:13px;")
        r2.addWidget(trig_lbl)
        trig_f = QLineEdit(cmd.get("trigger",""))
        trig_f.setPlaceholderText(_t("trigger_placeholder"))
        trig_f.setStyleSheet(_FIELD_SS)
        trig_f.setFixedHeight(34)
        r2.addWidget(trig_f, 1)
        rec_btn = QPushButton("⏺")
        rec_btn.setFixedSize(34, 34)
        rec_btn.setToolTip(_t("btn_record"))
        rec_btn.setStyleSheet(self._btn_ss("#ff4455", "rgba(255,60,80,20)"))
        rec_btn.clicked.connect(lambda _, f=trig_f: (_sfx("toggle"), self._record_trigger(f)))
        r2.addWidget(rec_btn)
        vl.addLayout(r2)

        # Actions label
        act_lbl = QLabel(_t("lbl_cmd_actions"))
        act_lbl.setStyleSheet("color:#7ab8d4; font-family:'Courier New'; font-size:12px; margin-top:4px;")
        vl.addWidget(act_lbl)

        # Actions container
        act_container = QWidget(); act_container.setStyleSheet("background: transparent;")
        act_layout = QVBoxLayout(act_container)
        act_layout.setContentsMargins(0,0,0,0); act_layout.setSpacing(4)
        vl.addWidget(act_container)

        # Restore existing actions
        for action in cmd.get("actions", []):
            self._add_action_row(act_layout, action.get("path",""), action.get("delay_ms", 0))

        # Add action button
        add_act = QPushButton(_t("btn_add_action"))
        add_act.setFixedHeight(30)
        add_act.setStyleSheet(self._btn_ss("rgba(0,200,255,160)", "rgba(0,100,180,15)"))
        add_act.clicked.connect(lambda _, al=act_layout: (_sfx("add"), self._add_action_row(al)))
        vl.addWidget(add_act)

        # Save card button
        save_btn = QPushButton(_t("btn_save_cmd"))
        save_btn.setFixedHeight(36)
        save_btn.setStyleSheet(self._btn_ss("#00ffff", "rgba(0,150,220,20)"))
        save_btn.clicked.connect(lambda: _sfx("save"))
        save_btn.clicked.connect(lambda _, cid=cmd["id"], nf=name_f, tf=trig_f,
                                  al=act_layout: self._save_card(cid, nf, tf, al))
        vl.addWidget(save_btn)

        self._cmd_layout.addWidget(card)
        card._cmd_id = cmd["id"]
        self._cmd_cards.append(card)

    def _add_action_row(self, act_layout, path="", delay_ms=0):
        row = QWidget(); row.setStyleSheet("background:transparent;")
        rl = QHBoxLayout(row); rl.setContentsMargins(0,0,0,0); rl.setSpacing(6)

        path_f = QLineEdit(path)
        path_f.setPlaceholderText(_t("path_placeholder"))
        path_f.setStyleSheet(_FIELD_SS)
        path_f.setFixedHeight(32)
        rl.addWidget(path_f, 3)

        delay_lbl = QLabel(_t("lbl_delay"))
        delay_lbl.setStyleSheet("color:#7ab8d4; font-family:'Courier New'; font-size:12px;")
        rl.addWidget(delay_lbl)

        delay_f = QLineEdit(str(delay_ms))
        delay_f.setStyleSheet(_FIELD_SS)
        delay_f.setFixedWidth(80); delay_f.setFixedHeight(32)
        rx = QRegExp(r"[0-9]*")
        delay_f.setValidator(QRegExpValidator(rx, delay_f))
        rl.addWidget(delay_f)

        ms_lbl = QLabel(_t("lbl_ms"))
        ms_lbl.setStyleSheet("color:#7ab8d4; font-family:'Courier New'; font-size:12px;")
        rl.addWidget(ms_lbl)

        del_r = QPushButton("✕")
        del_r.setFixedSize(28, 28)
        del_r.setStyleSheet(self._btn_ss("#ff4455", "rgba(255,60,80,20)"))
        del_r.clicked.connect(lambda _, r=row: (_sfx("delete"), r.setParent(None), r.deleteLater()))
        rl.addWidget(del_r)

        act_layout.addWidget(row)

    def _delete_card(self, card):
        data = _load_commands()
        data["commands"] = [c for c in data.get("commands", []) if c.get("id") != card._cmd_id]
        _save_commands(data)
        self._cmd_cards.remove(card)
        card.setParent(None); card.deleteLater()

    def _save_card(self, cid, name_f, trig_f, act_layout):
        actions = []
        for i in range(act_layout.count()):
            row_w = act_layout.itemAt(i).widget()
            if row_w is None: continue
            rl = row_w.layout()
            if rl is None or rl.count() < 4: continue
            path_w = rl.itemAt(0).widget()
            delay_w = rl.itemAt(2).widget()
            if not isinstance(path_w, QLineEdit): continue
            path_v = path_w.text().strip()
            if not path_v: continue
            try: delay_v = int(delay_w.text().strip() or "0")
            except: delay_v = 0
            actions.append({"path": path_v, "delay_ms": delay_v})

        new_cmd = {"id": cid, "name": name_f.text().strip(),
                   "trigger": trig_f.text().strip().lower(), "actions": actions}

        data = _load_commands()
        cmds = data.get("commands", [])
        for i, c in enumerate(cmds):
            if c.get("id") == cid:
                cmds[i] = new_cmd; break
        else:
            cmds.append(new_cmd)
        data["commands"] = cmds
        _save_commands(data)
        print(f"[UI] Команда сохранена: {new_cmd['name']} → {len(actions)} действий")

    def _record_trigger(self, field):
        self._record_and_fill(field)

    def _record_and_fill(self, field):
        import sounddevice as _sd
        import speech_recognition as _sr
        FS_REC = 16000; DUR = 3
        old_text = field.placeholderText()
        field.setPlaceholderText(_t("recording_3sec"))
        field.setReadOnly(True)

        def _do():
            try:
                audio = _sd.rec(int(DUR * FS_REC), samplerate=FS_REC,
                                 channels=1, dtype='int16')
                _sd.wait()
                import io, wave, numpy as np
                buf = io.BytesIO()
                with wave.open(buf, 'wb') as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(FS_REC)
                    wf.writeframes(audio.tobytes())
                buf.seek(0)
                r = _sr.Recognizer()
                with _sr.AudioFile(buf) as src:
                    recorded = r.record(src)
                text = r.recognize_google(recorded, language="ru-RU")
                field.setText(text.lower())
                field.setPlaceholderText(old_text)
            except Exception as e:
                field.setPlaceholderText(_t("error_prefix") + str(e))
            finally:
                field.setReadOnly(False)

        threading.Thread(target=_do, daemon=True).start()

    # helpers ─────────────────────────────────────────────────────────
    _recreate_pending = False   # class-level guard against re-entrant recreation

    def _on_ui_lang_changed(self, lang: str):
        """Called immediately when the UI language combo changes."""
        if SettingsWindow._recreate_pending:
            return   # ignore signal fired during restoration of values
        _set_ui_lang_cache(lang)
        _save_config({"ui_language": lang})
        QTimer.singleShot(0, self._request_recreate)

    def _request_recreate(self):
        """Ask the parent JarvisOverlay to recreate this settings window."""
        if self.parent() and hasattr(self.parent(), "_recreate_settings"):
            self.parent()._recreate_settings()

    def _rebuild_ui(self):
        """Kept for compatibility."""
        self._request_recreate()
    # ── CUSTOMIZATION PAGE ────────────────────────────────────────────
    _CUSTOM_ACCESS_CODE = "1111"

    def _build_custom_page(self):
        page = QWidget(); page.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(page); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background:#03080f; width:8px; border-radius:4px; }
            QScrollBar::handle:vertical { background:rgba(0,180,255,120); border-radius:4px; min-height:20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """)
        content = QWidget(); content.setStyleSheet("background: transparent;")
        root = QVBoxLayout(content); root.setContentsMargins(20,14,20,16); root.setSpacing(10)
        scroll.setWidget(content); outer.addWidget(scroll)

        # ── ТЕМЫ ─────────────────────────────────────────────────────
        root.addWidget(self._sec("\U0001f3a8  ТЕМЫ ОФОРМЛЕНИЯ"))
        themes = [
            ("default",   "\U0001f535  По умолчанию",   "Стандартный синий JARVIS",          "#04070e","#000d1a","#00b4ff","#00eeff",(0,180,255)),
            ("cyberpunk", "\u26a1  Киберпанк",           "Неоновый пурпурный, кислотный акцент","#0a0010","#1a0030","#ff00ff","#00ffcc",(255,0,255)),
            ("gold_rain", "\U0001f4b0  Золотой дождь",  "Падающие золотые монеты, роскошь",  "#0d0900","#1a1000","#ffd700","#ffaa00",(255,215,0)),
            ("matrix",    "\U0001f7e9  Матрица",         "Зелёный на чёрном, цифровой дождь", "#000400","#001400","#00ff41","#39ff14",(0,255,65)),
            ("blood",     "\U0001f534  Кровавый закат",  "Тёмно-красный, агрессивный стиль",  "#0d0000","#1a0000","#ff1a1a","#ff6600",(255,26,26)),
        ]
        self._theme_btns = []
        current_theme = _load_config().get("theme", "default")
        for tid, name, desc, bg0, bg1, acc, acc2, rgb in themes:
            card = QFrame()
            is_active = (tid == current_theme)
            card.setStyleSheet(f"""QFrame{{background:rgba(0,20,50,{"200" if is_active else "120"});
                border:{"2" if is_active else "1"}px solid {acc};border-radius:8px;}}""")
            cl = QHBoxLayout(card); cl.setContentsMargins(12,8,12,8); cl.setSpacing(10)
            preview = QLabel(); preview.setFixedSize(44,44)
            preview.setStyleSheet(f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                                   f"stop:0 {bg0},stop:0.5 {bg1},stop:1 {acc});"
                                   f"border:2px solid {acc2};border-radius:6px;")
            cl.addWidget(preview)
            tl = QVBoxLayout(); tl.setSpacing(1)
            nm = QLabel(name)
            nm.setStyleSheet(f"color:{acc};font-family:\'Courier New\';font-size:13px;font-weight:bold;")
            ds = QLabel(desc)
            ds.setStyleSheet("color:rgba(180,220,255,160);font-family:\'Courier New\';font-size:10px;")
            ds.setWordWrap(True)
            tl.addWidget(nm); tl.addWidget(ds); cl.addLayout(tl,1)
            apply_btn = QPushButton("\u2713 Активна" if is_active else "Применить")
            apply_btn.setFixedSize(90,30)
            _acc_=acc; _active_=is_active
            apply_btn.setStyleSheet(f"""QPushButton{{background:{"rgba(0,180,100,40)" if is_active else "rgba(0,0,0,0)"};
                border:1px solid {acc if not is_active else "#00ff88"};border-radius:4px;
                color:{"#00ff88" if is_active else acc};font-family:\'Courier New\';font-size:11px;font-weight:bold;}}
                QPushButton:hover{{background:{acc};color:#000;}}""")
            apply_btn.clicked.connect(lambda _,t=tid: (_sfx("toggle"), self._apply_theme(t)))
            cl.addWidget(apply_btn)
            root.addWidget(card)
            self._theme_btns.append((tid, card, apply_btn, acc))

        root.addWidget(self._div())

        # ── Прозрачность ─────────────────────────────────────────────
        root.addWidget(self._sec("\U0001fa9f  ПРОЗРАЧНОСТЬ ОКОН"))
        root.addWidget(self._lbl("Прозрачность (%) — применяется ко всем окнам"))
        op_row = QHBoxLayout(); op_row.setSpacing(8)
        self._opacity_combo = NoScrollComboBox(); self._opacity_combo.setEditable(True)
        for v in ["100","95","90","85","80","75","70","60","50"]:
            self._opacity_combo.addItem(v)
        saved_op = str(_load_config().get("window_opacity", 100))
        self._opacity_combo.setCurrentText(saved_op)
        self._opacity_combo.setStyleSheet(_FIELD_SS)
        self._opacity_combo.currentTextChanged.connect(self._apply_opacity)
        op_row.addWidget(self._opacity_combo); op_row.addStretch()
        root.addLayout(op_row); root.addWidget(self._div())

        # ── Голоса Джарвиса ──────────────────────────────────────────
        root.addWidget(self._sec("\U0001f3a4  ГОЛОС ДЖАРВИСА"))

        en_row = QHBoxLayout(); en_row.setSpacing(10)
        en_lbl = QLabel("EN голос:")
        en_lbl.setStyleSheet("color:rgba(180,220,255,200);font-family:\'Courier New\';font-size:12px;min-width:70px;")
        self._voice_en_combo = NoScrollComboBox()
        _EN_VOICES = [
            ("en-GB-ThomasNeural",       "Thomas — EN-GB"),
            ("en-US-GuyNeural",          "Guy — EN-US"),
            ("en-US-ChristopherNeural",  "Christopher — EN-US"),
            ("en-GB-RyanNeural",         "Ryan — EN-GB"),
            ("en-AU-WilliamNeural",      "William — EN-AU"),
        ]
        for val, label in _EN_VOICES:
            self._voice_en_combo.addItem(label, val)
        saved_en = _load_config().get("voice_jarvis", "en-GB-ThomasNeural")
        for i in range(self._voice_en_combo.count()):
            if self._voice_en_combo.itemData(i) == saved_en:
                self._voice_en_combo.setCurrentIndex(i); break
        self._voice_en_combo.setStyleSheet(_FIELD_SS)
        self._voice_en_combo.currentIndexChanged.connect(self._on_voice_en_changed)
        en_row.addWidget(en_lbl); en_row.addWidget(self._voice_en_combo, 1)
        root.addLayout(en_row)

        ru_row = QHBoxLayout(); ru_row.setSpacing(10)
        ru_lbl = QLabel("RU голос:")
        ru_lbl.setStyleSheet("color:rgba(180,220,255,200);font-family:\'Courier New\';font-size:12px;min-width:70px;")
        self._voice_ru_combo = NoScrollComboBox()
        _RU_VOICES = [
            ("ru-RU-DmitryNeural",   "Дмитрий — RU"),
            ("ru-RU-SergeyNeural",   "Сергей — RU"),
            ("ru-RU-DariyaNeural",   "Дарья — RU"),
            ("ru-RU-SvetlanaNeural", "Светлана — RU"),
            ("ru-RU-MaximNeural",    "Максим — RU"),
        ]
        for val, label in _RU_VOICES:
            self._voice_ru_combo.addItem(label, val)
        saved_ru = _load_config().get("voice_russian", "ru-RU-DmitryNeural")
        for i in range(self._voice_ru_combo.count()):
            if self._voice_ru_combo.itemData(i) == saved_ru:
                self._voice_ru_combo.setCurrentIndex(i); break
        self._voice_ru_combo.setStyleSheet(_FIELD_SS)
        self._voice_ru_combo.currentIndexChanged.connect(self._on_voice_ru_changed)
        ru_row.addWidget(ru_lbl); ru_row.addWidget(self._voice_ru_combo, 1)
        root.addLayout(ru_row)

        root.addWidget(self._div())

        # ── Цвета статусов ─────────────────────────────────────────────────────
        root.addWidget(self._sec("\U0001f7e1  ЦВЕТА СТАТУСОВ"))
        root.addWidget(self._lbl("Цвет интерфейса, рамки и иконки трея для каждого состояния.\nНажмите ●  чтобы открыть полный RGB-выбор цвета."))

        _STATUS_DEFS = [
            ("idle",          "\u23f8  Ожидание",          "idle"),
            ("listening",     "\U0001f3a4  Слушаю",        "listening"),
            ("user_speaking", "\U0001f7e1  Голос польз.",  "user_speaking"),
            ("processing",    "\u26a1  Обработка",         "processing"),
            ("speaking",      "\U0001f7e2  Джарвис говорит","speaking"),
        ]
        self._status_color_btns  = {}   # state → (list[(hex,btn)], swatch_lbl)
        self._status_swatch_btns = {}   # state → custom-circle QPushButton

        _PALETTE = [
            ("#00b4ff","Синий"),    ("#00ffcc","Циан"),     ("#00ff99","Зелёный"),
            ("#39ff14","Лайм"),     ("#ffdd00","Жёлтый"),   ("#ff6e00","Оранжевый"),
            ("#ff3355","Красный"),  ("#cc44ff","Фиолетовый"),("#ff00ff","Пурпурный"),
            ("#ffffff","Белый"),
        ]
        cfg_sc = _load_config().get("status_colors", {})

        for state_key, state_label, _ in _STATUS_DEFS:
            # label
            lbl = QLabel(state_label)
            lbl.setStyleSheet(
                "color:rgba(180,220,255,220);font-family:\'Courier New\';"
                "font-size:11px;font-weight:bold;margin-top:6px;"
            )
            root.addWidget(lbl)

            # saved colour
            saved_rgb  = cfg_sc.get(state_key, list(_DEFAULT_STATE_RGB[state_key]))
            saved_r,saved_g,saved_b = (int(x) for x in saved_rgb)
            saved_hex  = "#{:02x}{:02x}{:02x}".format(saved_r, saved_g, saved_b)

            btn_row = QHBoxLayout(); btn_row.setSpacing(5); btn_row.setContentsMargins(0,2,0,2)
            palette_btns = []

            # ── 10 preset colour buttons ──────────────────────────────
            for hex_col, tip in _PALETTE:
                b = QPushButton(); b.setFixedSize(24, 24); b.setToolTip(tip)
                active = (hex_col.lower() == saved_hex.lower())
                b.setStyleSheet(
                    f"QPushButton{{background:{hex_col};"
                    f"border:{('3px solid white' if active else '2px solid rgba(255,255,255,40)')};"
                    f"border-radius:12px;}}"
                    "QPushButton:hover{border:2px solid white;}"
                )
                b.setCheckable(True); b.setChecked(active)
                b.clicked.connect(
                    lambda _, sk=state_key, hc=hex_col: (_sfx("toggle"), self._apply_status_color(sk, hc))
                )
                btn_row.addWidget(b)
                palette_btns.append((hex_col, b))

            # ── RGB circle button ─────────────────────────────────────
            rgb_btn = QPushButton("●"); rgb_btn.setFixedSize(24, 24)
            rgb_btn.setToolTip("Выбрать любой цвет (RGB-круг)")
            # Draw a rainbow-gradient look via stylesheet
            rgb_btn.setStyleSheet("""
                QPushButton {
                    background: qconicalgradient(
                        cx:0.5, cy:0.5, angle:0,
                        stop:0.000 #ff0000, stop:0.167 #ffff00,
                        stop:0.333 #00ff00, stop:0.500 #00ffff,
                        stop:0.667 #0000ff, stop:0.833 #ff00ff,
                        stop:1.000 #ff0000
                    );
                    border: 2px solid rgba(255,255,255,60);
                    border-radius: 12px;
                    color: transparent;
                    font-size: 1px;
                }
                QPushButton:hover { border: 2px solid white; }
            """)
            rgb_btn.clicked.connect(
                lambda _, sk=state_key: (_sfx("click"), self._pick_status_color_rgb(sk))
            )
            btn_row.addWidget(rgb_btn)
            self._status_swatch_btns[state_key] = rgb_btn

            # ── Swatch strip ──────────────────────────────────────────
            swatch = QLabel()
            swatch.setFixedSize(48, 16)
            swatch.setStyleSheet(
                f"background:{saved_hex};border-radius:8px;"
                f"border:1px solid rgba(255,255,255,30);"
            )
            btn_row.addWidget(swatch)
            btn_row.addStretch()

            self._status_color_btns[state_key] = (palette_btns, swatch)
            root.addLayout(btn_row)

        root.addWidget(self._div())

        # ── Звуковой пак ──────────────────────────────────────────────
        root.addWidget(self._sec("🔊  ЗВУКОВОЙ ПАК"))
        root.addWidget(self._lbl("Выберите стиль звуков интерфейса. Нажмите ▶ чтобы послушать."))

        self._sfx_pack_btns = {}
        current_pack = _load_config().get("sfx_pack", "tactical")

        PACK_BTN_SS = """
            QPushButton {{
                background: {bg}; border: {bord}; border-radius: 5px;
                color: {col}; font-family: 'Courier New'; font-size: 11px;
                font-weight: bold; padding: 6px 10px; text-align: left;
            }}
            QPushButton:hover {{ background: rgba(0,180,255,35); color: #00eeff; }}
        """

        for pack_id, pack_name, pack_desc in SFX_PACKS:
            is_active = (pack_id == current_pack)
            row = QHBoxLayout(); row.setSpacing(6)

            # Кнопка выбора
            sel_btn = QPushButton(f"{'✓  ' if is_active else '    '}{pack_name}")
            sel_btn.setFixedHeight(32)
            sel_btn.setStyleSheet(PACK_BTN_SS.format(
                bg  = "rgba(0,180,100,30)" if is_active else "rgba(0,0,0,0)",
                bord= "1px solid #00ff88"  if is_active else "1px solid rgba(0,180,255,40)",
                col = "#00ff88"            if is_active else "rgba(160,210,255,200)",
            ))
            sel_btn.clicked.connect(
                lambda _, pid=pack_id: self._apply_sfx_pack(pid)
            )
            row.addWidget(sel_btn, 1)

            # Кнопка предпрослушивания ▶
            prev_btn = QPushButton("▶")
            prev_btn.setFixedSize(28, 32)
            prev_btn.setToolTip(pack_desc)
            prev_btn.setStyleSheet("""
                QPushButton { background: rgba(0,150,220,20);
                    border: 1px solid rgba(0,180,255,60); border-radius: 4px;
                    color: #00ccff; font-size: 11px; }
                QPushButton:hover { background: rgba(0,180,255,120); color: #fff; }
            """)
            prev_btn.clicked.connect(
                lambda _, pid=pack_id: self._preview_sfx_pack(pid)
            )
            row.addWidget(prev_btn)
            root.addLayout(row)
            self._sfx_pack_btns[pack_id] = sel_btn

        # ── Сброс кастомизации по умолчанию ──────────────────────────
        br_c = QHBoxLayout(); br_c.addStretch()
        bd_c = QPushButton("⟲  СБРОСИТЬ КАСТОМИЗАЦИЮ")
        bd_c.setFixedHeight(34)
        bd_c.setStyleSheet("""
            QPushButton { background: rgba(255,100,0,20); border: 1px solid rgba(255,140,0,120);
                border-radius: 4px; color: #ff8800; font-family:'Courier New';
                font-size: 12px; font-weight: bold; padding: 0 14px; }
            QPushButton:hover { background: rgba(255,140,0,180); color: #04070e; }
        """)
        bd_c.clicked.connect(lambda: _sfx("nav"))
        bd_c.clicked.connect(self._reset_customization)
        br_c.addWidget(bd_c)
        root.addLayout(br_c)

        root.addStretch()
        return page

    # ── Status color helpers ──────────────────────────────────────────────────

    def _pick_status_color_rgb(self, state_key: str):
        """Opens Qt colour dialog and applies the chosen colour."""
        cur_rgb = STATE_RGB.get(state_key, (0, 180, 255))
        initial = QColor(*cur_rgb)
        color = QColorDialog.getColor(
            initial, self,
            f"Цвет статуса: {state_key}",
            QColorDialog.ShowAlphaChannel
        )
        if color.isValid():
            hex_color = color.name()   # e.g. "#3af2c0"
            self._apply_status_color(state_key, hex_color)

    def _apply_status_color(self, state_key: str, hex_color: str):
        """Applies a new RGB colour to a status state — instantly everywhere."""
        hx = hex_color.lstrip("#")
        r, g, b = int(hx[0:2],16), int(hx[2:4],16), int(hx[4:6],16)

        # Update global dict — paintEvent / _on_status / tray all read it
        STATE_RGB[state_key] = (r, g, b)

        # Persist
        cfg_sc = _load_config().get("status_colors", {})
        cfg_sc[state_key] = [r, g, b]
        _save_config({"status_colors": cfg_sc})

        # Refresh preset buttons + swatch
        if state_key in self._status_color_btns:
            palette_btns, swatch = self._status_color_btns[state_key]
            for hc, btn in palette_btns:
                active = (hc.lower() == hex_color.lower())
                btn.setChecked(active)
                btn.setStyleSheet(
                    f"QPushButton{{background:{hc};"
                    f"border:{('3px solid white' if active else '2px solid rgba(255,255,255,40)')};"
                    f"border-radius:12px;}}"
                    "QPushButton:hover{border:2px solid white;}"
                )
            swatch.setStyleSheet(
                f"background:{hex_color};border-radius:8px;"
                f"border:1px solid rgba(255,255,255,30);"
            )

        # Redraw window + tray
        if _window:
            _window.update()
            if hasattr(_window, "_tray"):
                _window._tray.setIcon(_make_tray_icon(_window._state))

        print(f"[CUSTOM] Status color {state_key} → {hex_color}")

    def _apply_theme(self, theme_id: str):
        themes_data = {
            "default":   ("#04070e","#000d1a","#00b4ff","#00eeff",(0,180,255)),
            "cyberpunk": ("#0a0010","#1a0030","#ff00ff","#00ffcc",(255,0,255)),
            "gold_rain": ("#0d0900","#1a1000","#ffd700","#ffaa00",(255,215,0)),
            "matrix":    ("#000400","#001400","#00ff41","#39ff14",(0,255,65)),
            "blood":     ("#0d0000","#1a0000","#ff1a1a","#ff6600",(255,26,26)),
        }
        td = themes_data.get(theme_id, themes_data["default"])
        bg0,bg1,acc,acc2,rgb = td
        _save_config({"theme":theme_id,"theme_bg0":bg0,"theme_bg1":bg1,
                      "theme_acc":acc,"theme_acc2":acc2,"theme_rgb":list(rgb)})
        if _window is not None:
            _window._apply_theme_colors(bg0,bg1,acc,acc2,rgb,theme_id=theme_id)
        for tid,card,btn,card_acc in self._theme_btns:
            active = (tid == theme_id)
            card.setStyleSheet(f"QFrame{{background:rgba(0,20,50,{'200' if active else '120'});"
                                f"border:{'2' if active else '1'}px solid {card_acc};border-radius:8px;}}")
            btn.setText("\u2713 Активна" if active else "Применить")
            btn.setStyleSheet(f"QPushButton{{background:{'rgba(0,180,100,40)' if active else 'rgba(0,0,0,0)'};"
                               f"border:1px solid {card_acc if not active else '#00ff88'};border-radius:4px;"
                               f"color:{'#00ff88' if active else card_acc};"
                               f"font-family:\'Courier New\';font-size:11px;font-weight:bold;}}"
                               f"QPushButton:hover{{background:{card_acc};color:#000;}}")
        print(f"[CUSTOM] Theme \u2192 {theme_id}")

    def _apply_accent_color(self, hex_color: str):
        # kept for compatibility — accent buttons removed from UI
        _save_config({"accent_color": hex_color})
        print(f"[CUSTOM] Accent color → {hex_color}")

    def _apply_sfx_pack(self, pack_id: str):
        """Применяет звуковой пак и обновляет кнопки выбора."""
        _sfx_set_pack(pack_id)
        PACK_BTN_SS = """
            QPushButton {{
                background: {bg}; border: {bord}; border-radius: 5px;
                color: {col}; font-family: 'Courier New'; font-size: 11px;
                font-weight: bold; padding: 6px 10px; text-align: left;
            }}
            QPushButton:hover {{ background: rgba(0,180,255,35); color: #00eeff; }}
        """
        for pid, btn in self._sfx_pack_btns.items():
            active = (pid == pack_id)
            btn.setText(f"{'✓  ' if active else '    '}{next(n for i,n,_ in SFX_PACKS if i==pid)}")
            btn.setStyleSheet(PACK_BTN_SS.format(
                bg  = "rgba(0,180,100,30)" if active else "rgba(0,0,0,0)",
                bord= "1px solid #00ff88"  if active else "1px solid rgba(0,180,255,40)",
                col = "#00ff88"            if active else "rgba(160,210,255,200)",
            ))
        _sfx("click")

    def _preview_sfx_pack(self, pack_id: str):
        """Временно переключает пак для предпрослушивания, затем возвращает текущий."""
        import threading
        prev = _SFX_PACK_ACTIVE
        _sfx_set_pack(pack_id)
        _sfx("click")
        def _restore():
            import time; time.sleep(0.3)
            _sfx_set_pack(prev)
        threading.Thread(target=_restore, daemon=True).start()

    def _on_voice_en_changed(self, idx: int):
        """Мгновенно применяет EN-голос — Джарвис начнёт отвечать им сразу."""
        val = self._voice_en_combo.itemData(idx)
        if not val:
            return
        _save_config({"voice_jarvis": val})
        self.sig_saved.emit({"voice_jarvis": val})
        print(f"[CUSTOM] EN voice \u2192 {val}")

    def _on_voice_ru_changed(self, idx: int):
        """Мгновенно применяет RU-голос — Джарвис начнёт отвечать им сразу."""
        val = self._voice_ru_combo.itemData(idx)
        if not val:
            return
        _save_config({"voice_russian": val})
        self.sig_saved.emit({"voice_russian": val})
        print(f"[CUSTOM] RU voice \u2192 {val}")

    def _apply_opacity(self, val_str: str):
        try:
            pct = max(40, min(100, int(val_str.strip())))
            _apply_opacity_all(pct / 100.0)
            _save_config({"window_opacity": pct})
            print(f"[CUSTOM] Opacity \u2192 {pct}%")
        except ValueError:
            pass

    def _check_access_code(self) -> bool:
        if not hasattr(self, "_access_code_field"): return False
        return self._access_code_field.text().strip() == self._CUSTOM_ACCESS_CODE

    def _unlock_customization(self):
        self._tab3_btn.setEnabled(True); self._tab3_btn.setText(_t("tab_custom"))
        if hasattr(self, "_access_code_hint"): self._access_code_hint.setText(_t("custom_unlocked"))
        self._access_code_field.clear()
        print("[CUSTOM] Вкладка кастомизации разблокирована.")

    def _sec(self, txt):
        l = QLabel(txt); l.setStyleSheet(_SEC_SS); return l

    def _lbl(self, txt):
        l = QLabel(txt)
        l.setStyleSheet(_LABEL_SS)
        l.setWordWrap(True)
        return l

    # field-type tokens recognised in dflt:
    #   bool              → checkbox
    #   list              → plain QComboBox (editable=False, first item is default)
    #   tuple(val,[...])  → editable QComboBox, only digits/dot allowed
    #   str               → plain QLineEdit

    def _rows(self, root, fields):
        for key, lbl, dflt in fields:
            # ── label (word-wrap so nothing is cut off) ──────────────
            row = QHBoxLayout(); row.setSpacing(12)
            ll = QLabel(lbl)
            ll.setStyleSheet(_LABEL_SS)
            ll.setWordWrap(True)
            ll.setMinimumWidth(10)
            row.addWidget(ll, 2)

            # ── widget by type ───────────────────────────────────────
            if isinstance(dflt, bool):
                cb = QCheckBox(); cb.setChecked(dflt)
                cb.setStyleSheet(_CHECK_SS)
                row.addWidget(cb)
                row.addStretch(3)
                self._fields[key] = cb

            elif isinstance(dflt, list):
                # plain dropdown (non-editable)
                cb = NoScrollComboBox()
                cb.addItems(dflt)
                cb.setStyleSheet(_FIELD_SS)
                row.addWidget(cb, 3)
                self._fields[key] = cb

            elif isinstance(dflt, tuple):
                # (current_value, [presets]) — editable, digits+dot only
                val, presets = dflt
                cb = NoScrollComboBox()
                cb.setEditable(True)
                cb.addItems([str(p) for p in presets])
                cb.setCurrentText(str(val))
                cb.setStyleSheet(_FIELD_SS)
                # allow digits and optional single dot (for floats)
                rx = QRegExp(r"[0-9]*\.?[0-9]*")
                cb.lineEdit().setValidator(QRegExpValidator(rx, cb))
                cb.lineEdit().setStyleSheet(
                    "background: transparent; border: none; color: #00eeff;"
                    "font-family: 'Courier New'; font-size: 22px; padding: 0 4px;"
                )
                row.addWidget(cb, 3)
                self._fields[key] = cb

            else:
                le = QLineEdit(str(dflt))
                le.setStyleSheet(_FIELD_SS)
                row.addWidget(le, 3)
                self._fields[key] = le

            root.addLayout(row)

    def _load_saved_values(self):
        """Restore previously saved settings into the UI fields."""
        cfg = _load_config()
        if not cfg:
            return
        for key, w in self._fields.items():
            # API key: если в конфиге зашифрован — показываем placeholder,
            # не расшифровываем в UI (безопаснее). Поле можно оставить пустым
            # при сохранении — тогда зашифрованный ключ останется нетронутым.
            if key == "api_key":
                if cfg.get("api_key_enc"):
                    w.blockSignals(True)
                    try:
                        w.setPlaceholderText("●●●●●●●●●●●● (сохранён зашифрованным)")
                        w.setText("")
                    finally:
                        w.blockSignals(False)
                elif cfg.get("api_key"):
                    # Остался plain — показываем, при сохранении зашифруется
                    w.blockSignals(True)
                    try:
                        w.setText(str(cfg["api_key"]))
                    finally:
                        w.blockSignals(False)
                continue
            if key not in cfg:
                continue
            val = cfg[key]
            w.blockSignals(True)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                elif isinstance(w, QComboBox):
                    w.setCurrentText(str(val))
                else:
                    w.setText(str(val))
            finally:
                w.blockSignals(False)

    def _save(self):
        out = {}
        for k, w in self._fields.items():
            if isinstance(w, QCheckBox):
                out[k] = w.isChecked()
            elif isinstance(w, QComboBox):
                out[k] = w.currentText().strip() if w.isEditable() else w.currentText()
            else:
                out[k] = w.text().strip()

        # ── Проверяем код доступа к кастомизации ─────────────────────
        if hasattr(self, "_access_code_field") and self._access_code_field.text().strip():
            if self._check_access_code():
                self._unlock_customization()
            else:
                if hasattr(self, "_access_code_hint"):
                    self._access_code_hint.setStyleSheet(
                        "color: #ff4455; font-family:'Courier New'; font-size:11px;")
                    self._access_code_hint.setText(_t("custom_wrong_code"))
            # Не сохраняем сам код доступа в конфиг
            out.pop("access_code_field", None)

        # ── Шифруем API ключ перед записью на диск ───────────────────
        plain_key = out.pop("api_key", "").strip()
        if plain_key:
            enc = _encrypt_key(plain_key)
            if enc:
                out["api_key_enc"] = enc
                # plain текст не сохраняем — убираем из существующего конфига
                existing = _load_config()
                existing.pop("api_key", None)
                existing.update(out)
                try:
                    json.dump(existing, open(_CONFIG_FILE, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                except Exception as e:
                    print(f"[UI] config save error: {e}")
            else:
                # cryptography не установлена — сохраняем plain, предупреждаем
                print("[UI][WARN] cryptography не установлена — ключ сохранён незашифрованным. "
                      "Установите: pip install cryptography")
                out["api_key"] = plain_key
                _save_config(out)
        else:
            # Ключ не менялся (поле пустое) — сохраняем остальные поля
            out.pop("api_key_enc", None)  # не трогаем enc в файле
            _save_config(out)

        self.sig_saved.emit(out)    # apply to overlay immediately
        self.hide()

    # ── Значения по умолчанию для настроек ───────────────────────────────────
    _SETTINGS_DEFAULTS = {
        "ai_language":   "Русский",
        "ui_language":   "Русский",
        "voice_rate":    "+15%",
        "voice_pitch":   "0.0",
        "mic_index":     "0",
        "threshold":     "12.0",
        "silence_limit": "40",
        "model_id":      "gemini-2.5-flash-lite",
        "temperature":   "0.7",
        "max_history":   "20",
        "always_on_top": True,
        "show_log":      True,
    }

    def _reset_settings(self):
        """Сбрасывает все поля вкладки Настройки на заводские значения и сохраняет."""
        for key, val in self._SETTINGS_DEFAULTS.items():
            w = self._fields.get(key)
            if w is None:
                continue
            w.blockSignals(True)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                elif isinstance(w, QComboBox):
                    w.setCurrentText(str(val))
                else:
                    w.setText(str(val))
            finally:
                w.blockSignals(False)
        # Сохраняем дефолты (API ключ не трогаем)
        defaults_to_save = dict(self._SETTINGS_DEFAULTS)
        _save_config(defaults_to_save)
        self.sig_saved.emit(defaults_to_save)
        print("[UI] Настройки сброшены по умолчанию.")

    def _reset_customization(self):
        """Сбрасывает тему, прозрачность, голоса и цвета статусов на заводские."""
        # Тема → default
        self._apply_theme("default")

        # Прозрачность → 100%
        if hasattr(self, "_opacity_combo"):
            self._opacity_combo.blockSignals(True)
            self._opacity_combo.setCurrentText("100")
            self._opacity_combo.blockSignals(False)
        _apply_opacity_all(1.0)
        _save_config({"window_opacity": 100})

        # Голоса → по умолчанию
        _default_en = "en-GB-ThomasNeural"
        _default_ru = "ru-RU-DmitryNeural"
        if hasattr(self, "_voice_en_combo"):
            for i in range(self._voice_en_combo.count()):
                if self._voice_en_combo.itemData(i) == _default_en:
                    self._voice_en_combo.blockSignals(True)
                    self._voice_en_combo.setCurrentIndex(i)
                    self._voice_en_combo.blockSignals(False)
                    break
        if hasattr(self, "_voice_ru_combo"):
            for i in range(self._voice_ru_combo.count()):
                if self._voice_ru_combo.itemData(i) == _default_ru:
                    self._voice_ru_combo.blockSignals(True)
                    self._voice_ru_combo.setCurrentIndex(i)
                    self._voice_ru_combo.blockSignals(False)
                    break
        _save_config({"voice_jarvis": _default_en, "voice_russian": _default_ru})

        # Цвета статусов → заводские
        global STATE_RGB
        STATE_RGB.update(dict(_DEFAULT_STATE_RGB))
        _save_config({"status_colors": {k: list(v) for k, v in _DEFAULT_STATE_RGB.items()}})

        # Обновляем кнопки палитры и свотчи
        for state_key, (palette_btns, swatch) in self._status_color_btns.items():
            r, g, b = _DEFAULT_STATE_RGB[state_key]
            hex_color = "#{:02x}{:02x}{:02x}".format(r, g, b)
            for hc, btn in palette_btns:
                active = (hc.lower() == hex_color.lower())
                btn.setChecked(active)
                btn.setStyleSheet(
                    f"QPushButton{{background:{hc};"
                    f"border:{('3px solid white' if active else '2px solid rgba(255,255,255,40)')};"
                    f"border-radius:12px;}}"
                    "QPushButton:hover{border:2px solid white;}"
                )
            swatch.setStyleSheet(
                f"background:{hex_color};border-radius:8px;"
                f"border:1px solid rgba(255,255,255,30);"
            )

        # Перерисовываем окно и трей
        if _window:
            _window.update()
            if hasattr(_window, "_tray"):
                _window._tray.setIcon(_make_tray_icon(_window._state))

        self.sig_saved.emit({"voice_jarvis": _default_en, "voice_russian": _default_ru})
        print("[UI] Кастомизация сброшена по умолчанию.")

    def paintEvent(self, _):
        p = QPainter(self); w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, C_BG1)
        p.setPen(QPen(_qc(0,180,255,12), 1))
        for i in range(0, w, 15): p.drawLine(i, 0, i, h)
        for i in range(0, h, 15): p.drawLine(0, i, w, i)
        p.setPen(QPen(_qc(0,160,220,150), 1.2))
        p.setBrush(Qt.NoBrush); p.drawRect(0, 0, w-1, h-1)
        pen = QPen(_qc(0,255,255), 1.8); p.setPen(pen); L = 14
        for x, y, dx, dy in [
            (1,1,L,0),(1,1,0,L),(w-1,1,-L,0),(w-1,1,0,L),
            (1,h-1,L,0),(1,h-1,0,-L),(w-1,h-1,-L,0),(w-1,h-1,0,-L)
        ]:
            p.drawLine(QPointF(x,y), QPointF(x+dx,y+dy))
        p.end()

    def _div(self):
        l = QWidget(); l.setFixedHeight(1)
        l.setStyleSheet("background: rgba(0,160,220,50);"); return l


# ═══════════════════════════ RESIZE HANDLE ══════════════════════════
RESIZE_MARGIN = 8   # px от края для зоны ресайза

class JarvisOverlay(QWidget):
    sig_set_status = pyqtSignal(str, str)
    sig_add_log    = pyqtSignal(str, str)
    sig_set_volume = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self._state    = "idle"
        self._txt      = "ОЖИДАНИЕ КОМАНДЫ"
        self._drag     = None
        self._blink    = True

        # Theme color overrides (None = use defaults)
        self._theme_bg0 = None
        self._theme_bg1 = None
        self._theme_acc = None
        self._theme_acc2 = None
        self._theme_rgb = None

        # ── Theme FX state ───────────────────────────────────────────
        # Текущая активная тема и её частицы/анимационные параметры.
        # Инициализируются в _init_theme_fx(), которая вызывается
        # из _apply_theme_colors() при применении темы.
        self._theme_id    = "default"
        self._fx_particles = []
        self._fx_hue       = 0.0   # для радужной обводки Киберпанка

        # resize state
        self._resizing   = False
        self._resize_dir = None
        self._resize_start_pos  = None
        self._resize_start_geom = None

        self._setup_window()
        self._build_ui()

        self._settings = SettingsWindow(self)
        self._settings.sig_saved.connect(self._apply_settings)

        # ── История разговора ────────────────────────────────────────
        self._history_win = HistoryWindow()

        self.sig_set_status.connect(self._on_status)
        self.sig_add_log.connect(self._on_log)
        self.sig_set_volume.connect(self._on_volume)

        bt = QTimer(self); bt.timeout.connect(self._do_blink); bt.start(550)

        # ── Theme FX timer (30 fps) ─────────────────────────────────
        # Обновляет позиции частиц (монеты/дождь/искры/лучи) и hue для
        # радужной рамки. Лёгкий — не более 25 частиц на тему.
        fxt = QTimer(self); fxt.timeout.connect(self._tick_theme_fx); fxt.start(33)

        # ── Иконка в трее ────────────────────────────────────────────
        self._setup_tray()

        # ── Применяем сохранённые настройки прозрачности и темы ─────
        self._apply_saved_theme()

    def _setup_window(self):
        # Qt.Tool скрывает окно из панели задач — используем Qt.Window
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(260, 480)
        self.resize(290, 480)
        self.setMouseTracking(True)
        screen = QApplication.instance().primaryScreen().availableGeometry()
        self.move(screen.right()-310, screen.bottom()-480)

        # ── Заголовок и иконка в панели задач ───────────────────────
        # Название — Windows показывает его как подпись кнопки в taskbar
        self.setWindowTitle("Jarvis")

        # Иконка — ищем jarvis.ico рядом с исполняемым файлом / скриптом
        import os as _os
        _base = _os.path.dirname(_os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__
        ))
        _ico_path = _os.path.join(_base, "jarvis.ico")
        if _os.path.isfile(_ico_path):
            _app_icon = QIcon(_ico_path)
            self.setWindowIcon(_app_icon)
            QApplication.instance().setWindowIcon(_app_icon)

        # ── AppUserModelID — привязывает окно Qt к кнопке launcher.exe ─
        # Должен совпадать со значением в launcher.cs
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "mycompany.jarvis.system.v07"
            )
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        # Re-apply saved opacity after the window is actually shown —
        # this fixes opacity being reset on every restart
        try:
            pct = _load_config().get("window_opacity", 100)
            from PyQt5.QtCore import QTimer
            op = max(40, min(100, int(pct))) / 100.0
            QTimer.singleShot(0,   lambda: self.setWindowOpacity(op))
            QTimer.singleShot(200, lambda: self.setWindowOpacity(op))
        except Exception:
            pass

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(4)

        badge = QLabel("0.8")
        badge.setFont(QFont("Courier New", 8, QFont.Bold))
        badge.setStyleSheet("""
            color: #00ffff; border: 1px solid #00ffff;
            background: rgba(0,180,255,20);
            border-radius: 3px; padding: 1px 5px;
        """)
        hdr.addWidget(badge)

        title = QLabel("JARVIS")
        title.setFont(QFont("Courier New", 14, QFont.Bold))
        title.setStyleSheet("color: #ffffff; letter-spacing:5px; padding-left:8px;")
        hdr.addWidget(title)
        hdr.addStretch()

        # ⚙ Settings | 📋 History | ─ Collapse | ⏹ Stop (hidden) | ✕ Close
        for sym, tip_key, slot, close, sfx_name in [
            ("⚙", "tip_settings",  self._open_settings,  False, "open"),
            ("☰", "tip_history",   self._open_history,   False, "open"),
            ("─", "tip_collapse",  self._collapse,        False, "close"),
            ("✕", "tip_close",     self._close_app,       True,  "close"),
        ]:
            b = QPushButton(sym); b.setFixedSize(22, 22)
            b.setToolTip(_t(tip_key))
            if close:
                b.setStyleSheet("""
                    QPushButton{background:rgba(255,60,80,20);
                      border:1px solid rgba(255,60,80,70);border-radius:3px;
                      color:#ff4455;font-size:11px;font-weight:bold;}
                    QPushButton:hover{background:#ff3344; color:#fff;}
                """)
            else:
                b.setStyleSheet("""
                    QPushButton{background:rgba(0,150,220,20);
                      border:1px solid rgba(0,180,255,80);border-radius:3px;
                      color:#00ffff;font-size:12px;font-weight:bold;}
                    QPushButton:hover{background:#00ffff; color:#04070e;}
                """)
            _n = sfx_name
            b.clicked.connect(lambda checked=False, s=_n: _sfx(s))
            b.clicked.connect(slot)
            # Insert stop button before the close (✕) button
            if close:
                self._stop_btn = QPushButton("⏹")
                self._stop_btn.setFixedSize(22, 22)
                self._stop_btn.setToolTip("Остановить речь")
                self._stop_btn.setStyleSheet("""
                    QPushButton{background:rgba(255,60,80,20);
                      border:1px solid rgba(255,80,80,150);border-radius:3px;
                      color:#ff5555;font-size:11px;font-weight:bold;}
                    QPushButton:hover{background:#ff3333; color:#fff;}
                """)
                self._stop_btn.clicked.connect(lambda: _sfx("stop"))
                self._stop_btn.clicked.connect(self._on_stop_clicked)
                self._stop_btn.hide()
                hdr.addWidget(self._stop_btn)
            hdr.addWidget(b)

        root.addLayout(hdr)
        root.addSpacing(8)
        root.addWidget(self._div())
        root.addSpacing(6)

        # ── Ring — fixed-height container prevents overflow ──────
        ring_container = QWidget()
        ring_container.setFixedHeight(210)   # hard cap: ring stays inside this box
        ring_container.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        rc_layout = QHBoxLayout(ring_container)
        rc_layout.setContentsMargins(0, 5, 0, 5)
        self._ring = RingWidget()
        rc_layout.addStretch()
        rc_layout.addWidget(self._ring)
        rc_layout.addStretch()
        root.addWidget(ring_container)

        root.addSpacing(6)

        # ── Status badge ─────────────────────────────────────────
        self._lbl_state = QLabel(get_state_label("idle"))
        self._lbl_state.setFont(QFont("Courier New", 13, QFont.Bold))
        self._lbl_state.setAlignment(Qt.AlignCenter)
        rgb = STATE_RGB.get(self._state, (0,180,255))
        self._lbl_state.setStyleSheet(
            f"color: rgb({rgb[0]},{rgb[1]},{rgb[2]}); letter-spacing:7px;"
            f"background: rgba({rgb[0]},{rgb[1]},{rgb[2]},15);"
            f"border: 1px solid rgba({rgb[0]},{rgb[1]},{rgb[2]},50);"
            "border-radius: 4px; padding: 4px 0;"
        )
        root.addWidget(self._lbl_state)
        root.addSpacing(6)

        # ── Sub text ──────────────────────────────────────────────
        self._lbl_txt = QLabel(_t("txt_idle"))
        self._lbl_txt.setFont(QFont("Courier New", 9, QFont.Bold))
        self._lbl_txt.setAlignment(Qt.AlignCenter)
        self._lbl_txt.setWordWrap(True)
        self._lbl_txt.setStyleSheet("color: #ffffff;")
        root.addWidget(self._lbl_txt)
        root.addSpacing(8)



        root.addWidget(self._div())
        root.addSpacing(4)

        # ── Log ───────────────────────────────────────────────────
        self._log = LogWidget()
        root.addWidget(self._log)

        # ── Size grip (bottom-right corner) ──────────────────────
        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(grip)
        root.addLayout(grip_row)

        self._scan = ScanLine(self)

    # ── resize logic (all edges + corners) ──────────────────────────
    def _get_resize_dir(self, pos):
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        m = RESIZE_MARGIN
        left  = x < m;   right  = x > w-m
        top   = y < m;   bottom = y > h-m
        if top and left:    return "tl"
        if top and right:   return "tr"
        if bottom and left: return "bl"
        if bottom and right:return "br"
        if left:   return "l"
        if right:  return "r"
        if top:    return "t"
        if bottom: return "b"
        return None

    def _cursor_for_dir(self, d):
        map_ = {
            "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
            "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
            "l":  Qt.SizeHorCursor,   "r":  Qt.SizeHorCursor,
            "t":  Qt.SizeVerCursor,   "b":  Qt.SizeVerCursor,
        }
        return map_.get(d, Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            d = self._get_resize_dir(e.pos())
            if d:
                self._resizing = True
                self._resize_dir = d
                self._resize_start_pos  = e.globalPos()
                self._resize_start_geom = self.geometry()
            else:
                self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._resizing and e.buttons() == Qt.LeftButton:
            self._do_resize(e.globalPos())
            return
        if e.buttons() == Qt.LeftButton and self._drag:
            self.move(e.globalPos() - self._drag)
            return
        d = self._get_resize_dir(e.pos())
        self.setCursor(QCursor(self._cursor_for_dir(d) if d else Qt.ArrowCursor))

    def mouseReleaseEvent(self, _):
        self._drag = None
        self._resizing = False
        self._resize_dir = None
        self.setCursor(QCursor(Qt.ArrowCursor))

    def _do_resize(self, gpos):
        dx = gpos.x() - self._resize_start_pos.x()
        dy = gpos.y() - self._resize_start_pos.y()
        g  = self._resize_start_geom
        x, y, w, h = g.x(), g.y(), g.width(), g.height()
        mn_w, mn_h = self.minimumWidth(), self.minimumHeight()
        d = self._resize_dir

        if "r" in d: w = max(mn_w, g.width()  + dx)
        if "b" in d: h = max(mn_h, g.height() + dy)
        if "l" in d:
            nw = max(mn_w, g.width() - dx)
            x  = g.x() + (g.width() - nw)
            w  = nw
        if "t" in d:
            nh = max(mn_h, g.height() - dy)
            y  = g.y() + (g.height() - nh)
            h  = nh

        self.setGeometry(x, y, w, h)

    # ── painting ─────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Use theme colors if a theme is active, otherwise fall back to defaults
        bg0 = QColor(getattr(self, "_theme_bg0", None) or C_BG0)
        bg1 = QColor(getattr(self, "_theme_bg1", None) or C_BG1)
        th_rgb = getattr(self, "_theme_rgb", None)

        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, bg0); g.setColorAt(1, bg1)
        p.fillRect(0, 0, w, h, QBrush(g))

        grid_r, grid_g, grid_b = (th_rgb or (0, 150, 220))
        p.setPen(QPen(_qc(grid_r, grid_g, grid_b, 12), 1))
        step = 16
        for y in range(0, h, step): p.drawLine(0, y, w, y)
        for x in range(0, w, step): p.drawLine(x, 0, x, h)

        rgb = th_rgb or STATE_RGB.get(self._state, (0,180,255))

        # ── Киберпанк: рамка и углы переливаются радугой ────────────
        if self._theme_id == "cyberpunk":
            border_col = _hsv_qc(self._fx_hue, 255, 255, 130)
            corner_col = _hsv_qc(self._fx_hue, 255, 255, 240)
        else:
            border_col = _qc(*rgb, 120)
            corner_col = _qc(*rgb, 230)

        p.setPen(QPen(border_col, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, w-1, h-1))

        p.setPen(QPen(corner_col, 2)); L = 16
        for x, y, dx, dy in [
            (1,1,L,0),(1,1,0,L),(w-1,1,-L,0),(w-1,1,0,L),
            (1,h-1,L,0),(1,h-1,0,-L),(w-1,h-1,-L,0),(w-1,h-1,0,-L),
        ]:
            if self._theme_id == "cyberpunk":
                # У каждого уголка свой оттенок радуги — "переливание"
                _idx = [(1,1),(1,1),(w-1,1),(w-1,1),(1,h-1),(1,h-1),(w-1,h-1),(w-1,h-1)].index((x,y))                     if (x,y) in [(1,1),(w-1,1),(1,h-1),(w-1,h-1)] else 0
                p.setPen(QPen(_hsv_qc(self._fx_hue + _idx*45, 255, 255, 240), 2))
            p.drawLine(QPointF(x,y), QPointF(x+dx,y+dy))

        if self._theme_id == "cyberpunk":
            tg = QLinearGradient(0,0,w,0)
            tg.setColorAt(0.0, _hsv_qc(self._fx_hue,        255, 255, 0))
            tg.setColorAt(0.5, _hsv_qc(self._fx_hue + 60,   255, 255, 110))
            tg.setColorAt(1.0, _hsv_qc(self._fx_hue + 120,  255, 255, 0))
        else:
            tg = QLinearGradient(0,0,w,0)
            tg.setColorAt(0, _qc(*rgb, 0)); tg.setColorAt(0.5, _qc(*rgb, 90))
            tg.setColorAt(1, _qc(*rgb, 0))
        p.fillRect(1, 1, w-2, 2, QBrush(tg))

        # ── Уникальная анимация темы (монеты / дождь / лучи / искры) ──
        self._draw_theme_fx(p, w, h)

        # ── Scan line drawn here (no child-widget compositing issues) ──
        sy = self._scan._y
        sg = QLinearGradient(0, sy-10, 0, sy+10)
        sg.setColorAt(0,   _qc(*rgb,  0))
        sg.setColorAt(0.5, _qc(*rgb, 18))
        sg.setColorAt(1,   _qc(*rgb,  0))
        p.fillRect(1, max(0, sy-10), w-2, 20, QBrush(sg))
        p.end()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._scan.resize(self.size())
        # Пересоздаём частицы темы под новый размер окна
        # (matrix: число колонок зависит от ширины; остальные просто
        # переразбрасываются в новых границах)
        if self._theme_id != "default":
            self._init_theme_fx(self._theme_id)
        self.update()

    def _setup_tray(self):
        """Создаёт иконку в системном трее."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[UI] Системный трей недоступен.")
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_tray_icon("idle"))
        self._tray.setToolTip("JARVIS — ожидание")

        tray_menu = QMenu()
        tray_menu.setStyleSheet("""
            QMenu { background:#06101e; color:#00eeff;
                    border:1px solid rgba(0,180,255,80); font-family:'Courier New'; font-size:13px; }
            QMenu::item:selected { background:rgba(0,180,255,40); }
        """)
        act_show = QAction("◈  Показать JARVIS", self)
        act_show.triggered.connect(self._tray_show)
        tray_menu.addAction(act_show)

        act_hist = QAction("☰  История разговора", self)
        act_hist.triggered.connect(self._open_history)
        tray_menu.addAction(act_hist)

        act_settings = QAction("⚙  Настройки", self)
        act_settings.triggered.connect(self._open_settings)
        tray_menu.addAction(act_settings)

        tray_menu.addSeparator()

        act_quit = QAction("✕  Выключить JARVIS", self)
        act_quit.triggered.connect(self._close_app)
        tray_menu.addAction(act_quit)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._tray_show()

    def _tray_show(self):
        """Восстанавливает окно из трея."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_history(self):
        """Открывает/скрывает окно истории разговора."""
        if self._history_win.isVisible():
            self._history_win.hide()
            return
        g = self.geometry()
        hw = self._history_win
        x = g.right() + 8
        screen = QApplication.instance().primaryScreen().availableGeometry()
        if x + hw.width() > screen.right():
            x = g.left() - hw.width() - 8
        hw.move(x, g.top())
        hw.show()
        hw.raise_()

    # ── actions ──────────────────────────────────────────────────────
    def _close_app(self):
        if hasattr(self, "_tray"):
            self._tray.hide()
        _save_session_txt()   # ← сохраняем переписку в .txt на Рабочий стол
        import os as _os
        _os._exit(0)

    def _collapse(self):
        self.hide()   # прячем в трей вместо ресайза
        if hasattr(self, "_tray"):
            self._tray.showMessage("JARVIS", "Свёрнут в трей. Двойной клик — открыть.",
                                   QSystemTrayIcon.Information, 2000)

    def _open_settings(self):
        if self._settings.isVisible():
            self._settings.hide(); return
        g  = self.geometry()
        sw = self._settings
        x  = g.left() - sw.width() - 6
        if x < 0: x = g.right() + 6
        sw.move(x, g.top()); sw.show(); sw.raise_()

    def _recreate_settings(self):
        """Destroy and recreate the SettingsWindow (called when UI language changes)."""
        # Guard: prevent re-entry
        if SettingsWindow._recreate_pending:
            return
        SettingsWindow._recreate_pending = True

        try:
            # Remember position and visibility
            was_visible = self._settings.isVisible()
            old_pos     = self._settings.pos()

            # Snapshot current field values before destroying
            snapshot = {}
            for k, w in self._settings._fields.items():
                if isinstance(w, QCheckBox):
                    snapshot[k] = w.isChecked()
                elif isinstance(w, QComboBox):
                    snapshot[k] = w.currentText().strip() if w.isEditable() else w.currentText()
                else:
                    snapshot[k] = w.text().strip()

            # Destroy old window
            self._settings.hide()
            self._settings.sig_saved.disconnect()
            self._settings.deleteLater()

            # Create fresh window with new language
            self._settings = SettingsWindow(self)
            self._settings.sig_saved.connect(self._apply_settings)

            # Restore snapshotted values — block all signals to prevent re-trigger
            cfg = _load_config()
            cfg.update(snapshot)
            for key, w in self._settings._fields.items():
                val = cfg.get(key)
                if val is None:
                    continue
                w.blockSignals(True)
                try:
                    if isinstance(w, QCheckBox):
                        w.setChecked(bool(val))
                    elif isinstance(w, QComboBox):
                        w.setCurrentText(str(val))
                    else:
                        w.setText(str(val))
                finally:
                    w.blockSignals(False)

            if was_visible:
                self._settings.move(old_pos)
                self._settings.show()
                self._settings.raise_()
        finally:
            SettingsWindow._recreate_pending = False

    def _apply_saved_theme(self):
        """Called on startup — restores opacity, theme and status colors from config."""
        global STATE_RGB
        STATE_RGB = _load_state_rgb_from_cfg()   # restore custom status colours
        cfg = _load_config()
        # Прозрачность
        pct = cfg.get("window_opacity", 100)
        try:
            _apply_opacity_all(max(40, min(100, int(pct))) / 100.0)
        except Exception:
            pass
        # Тема
        if cfg.get("theme_bg0"):
            try:
                bg0  = cfg["theme_bg0"];  bg1  = cfg["theme_bg1"]
                acc  = cfg["theme_acc"];   acc2 = cfg["theme_acc2"]
                rgb  = tuple(cfg.get("theme_rgb", [0,180,255]))
                tid  = cfg.get("theme", "default")
                self._apply_theme_colors(bg0, bg1, acc, acc2, rgb, theme_id=tid)
            except Exception as e:
                print(f"[CUSTOM] theme restore error: {e}")

    def _apply_theme_colors(self, bg0, bg1, acc, acc2, rgb, theme_id=None):
        """Updates overlay ring/scan colors. Full repaint needed."""
        # Store theme colors so paintEvent uses them
        self._theme_bg0  = bg0
        self._theme_bg1  = bg1
        self._theme_acc  = acc
        self._theme_acc2 = acc2
        self._theme_rgb  = tuple(rgb)
        if theme_id:
            self._init_theme_fx(theme_id)
        self.update()
        print(f"[CUSTOM] Theme colors applied: acc={acc}")

    # ── Theme FX: init / update / draw ──────────────────────────────────
    def _init_theme_fx(self, theme_id: str):
        """Создаёт частицы для уникальной анимации новой темы."""
        self._theme_id = theme_id
        self._fx_particles = []
        self._fx_hue = 0.0
        w = max(self.width(), 100)
        h = max(self.height(), 100)

        if theme_id == "gold_rain":
            # ── Падающие золотые монеты (40 шт., скорость +10%) ──────
            for _ in range(40):
                self._fx_particles.append({
                    "x":     random.uniform(0, w),
                    "y":     random.uniform(-h, 0),
                    "speed": random.uniform(1.0, 2.8) * 1.1,
                    "size":  random.uniform(5, 10),
                    "rot":   random.uniform(0, 360),
                    "rspd":  random.uniform(-5, 5),
                    "sway":  random.uniform(0, 6.28),
                })

        elif theme_id == "matrix":
            # ── Цифровой дождь (падающие символы по колонкам) ───────
            col_w = 14
            cols = max(1, w // col_w)
            for i in range(cols):
                trail_len = random.randint(5, 12)
                self._fx_particles.append({
                    "col":    i,
                    "y":      random.uniform(-h, 0),
                    "speed":  random.uniform(2.0, 5.5),
                    "trail":  trail_len,
                    "chars":  [random.choice(_MATRIX_CHARS) for _ in range(trail_len)],
                    "next_swap": random.randint(2, 8),
                })

        elif theme_id == "cyberpunk":
            # ── Хаотичные неоновые лучи, летящие с краёв интерфейса ──
            # Каждый луч "закреплён" одним концом на краю окна (как будто
            # прилетел снаружи) и направлен внутрь под вращающимся углом.
            for _ in range(10):
                edge = random.choice(("top", "bottom", "left", "right"))
                if edge == "top":
                    cx, cy = random.uniform(0, w), 0.0
                    base_angle = 90    # вниз, внутрь окна
                elif edge == "bottom":
                    cx, cy = random.uniform(0, w), float(h)
                    base_angle = 270   # вверх, внутрь окна
                elif edge == "left":
                    cx, cy = 0.0, random.uniform(0, h)
                    base_angle = 0     # вправо, внутрь окна
                else:  # right
                    cx, cy = float(w), random.uniform(0, h)
                    base_angle = 180   # влево, внутрь окна

                self._fx_particles.append({
                    "cx":    cx,
                    "cy":    cy,
                    "angle": (base_angle + random.uniform(-35, 35)) % 360,
                    "aspd":  random.uniform(-3.5, 3.5) or 1.5,
                    "len":   random.uniform(0.45, 0.95),
                    "hue":   random.uniform(0, 360),
                })

        elif theme_id == "blood":
            # ── Поднимающиеся искры/угли ─────────────────────────────
            for _ in range(20):
                self._fx_particles.append({
                    "x":    random.uniform(0, w),
                    "y":    random.uniform(0, h),
                    "vx":   random.uniform(-0.4, 0.4),
                    "vy":   random.uniform(-2.4, -0.6),
                    "size": random.uniform(1.5, 4.0),
                    "life": random.uniform(0.3, 1.0),
                })
        # theme "default" → без доп. частиц (чистый интерфейс)

    def _tick_theme_fx(self):
        """30 fps: двигает частицы текущей темы и просит перерисовать окно."""
        tid = self._theme_id
        if tid == "default":
            return  # нет анимации — экономим CPU
        w, h = self.width(), self.height()

        if tid == "gold_rain":
            for c in self._fx_particles:
                c["y"]    += c["speed"]
                c["rot"]  += c["rspd"]
                c["sway"] += 0.05
                if c["y"] > h + 10:
                    c["y"] = random.uniform(-20, -5)
                    c["x"] = random.uniform(0, w)
                    c["speed"] = random.uniform(1.0, 2.8) * 1.1

        elif tid == "matrix":
            for col in self._fx_particles:
                col["y"] += col["speed"]
                col["next_swap"] -= 1
                if col["next_swap"] <= 0:
                    # Случайно меняем один символ в "хвосте" — мерцание
                    idx = random.randrange(len(col["chars"]))
                    col["chars"][idx] = random.choice(_MATRIX_CHARS)
                    col["next_swap"] = random.randint(2, 8)
                if col["y"] > h + col["trail"] * 16:
                    col["y"] = random.uniform(-h * 0.6, -10)
                    col["speed"] = random.uniform(2.0, 5.5)

        elif tid == "cyberpunk":
            # Радужная обводка — непрерывный сдвиг hue
            self._fx_hue = (self._fx_hue + 2.2) % 360
            for ray in self._fx_particles:
                ray["angle"] = (ray["angle"] + ray["aspd"]) % 360
                ray["hue"]   = (ray["hue"] + 1.6) % 360
                # Хаотичность: иногда резко меняем скорость/направление вращения
                if random.random() < 0.015:
                    ray["aspd"] = random.uniform(-4.5, 4.5) or 2.0

        elif tid == "blood":
            for s in self._fx_particles:
                s["x"]    += s["vx"]
                s["y"]    += s["vy"]
                s["life"] -= 0.012
                if s["y"] < -10 or s["life"] <= 0:
                    s["x"]    = random.uniform(0, w)
                    s["y"]    = h + random.uniform(0, 20)
                    s["vx"]   = random.uniform(-0.4, 0.4)
                    s["vy"]   = random.uniform(-2.4, -0.6)
                    s["life"] = 1.0
                    s["size"] = random.uniform(1.5, 4.0)

        self.update()

    def _draw_theme_fx(self, p, w, h):
        """Рисует анимацию текущей темы. Вызывается из paintEvent."""
        tid = self._theme_id
        if tid == "default" or not self._fx_particles:
            return

        if tid == "gold_rain":
            # ── Падающие золотые монеты (эллипс + внутреннее кольцо) ──
            for c in self._fx_particles:
                p.save()
                p.translate(c["x"] + math.sin(c["sway"]) * 6, c["y"])
                p.rotate(c["rot"])
                sz = c["size"]
                p.setPen(QPen(_qc(255, 215, 0, 200), 1))
                p.setBrush(QBrush(_qc(255, 200, 0, 160)))
                p.drawEllipse(QRectF(-sz, -sz*0.5, sz*2, sz))
                p.setPen(QPen(_qc(255, 245, 180, 220), 0.8))
                p.drawEllipse(QRectF(-sz*0.55, -sz*0.3, sz*1.1, sz*0.6))
                p.restore()

        elif tid == "matrix":
            # ── Цифровой дождь — НЕ затирает кольцо/аватар Джарвиса ───
            # Вырезаем из области рисования прямоугольник RingWidget,
            # чтобы цифры проходили строго ПОД ним, а не поверх.
            from PyQt5.QtCore import QPoint, QRect
            from PyQt5.QtGui  import QRegion

            p.save()
            ring = getattr(self, "_ring", None)
            if ring is not None and ring.isVisible():
                top_left = ring.mapTo(self, QPoint(0, 0))
                ring_rect = QRect(top_left, ring.size())
                clip = QRegion(0, 0, w, h) - QRegion(ring_rect)
                p.setClipRegion(clip)

            p.setFont(QFont("Courier New", 10, QFont.Bold))
            col_w = 14
            for col in self._fx_particles:
                x = col["col"] * col_w
                if x > w:
                    continue
                for i, ch in enumerate(col["chars"]):
                    y = col["y"] - i * 16
                    if -16 < y < h + 16:
                        # Голова (самый нижний символ хвоста) — ярче
                        if i == 0:
                            p.setPen(QPen(_qc(200, 255, 200, 255), 1))
                        else:
                            alpha = max(10, 200 - i * (190 // max(1, len(col["chars"]))))
                            p.setPen(QPen(_qc(0, 255, 70, alpha), 1))
                        p.drawText(QRectF(x, y, col_w, 16), Qt.AlignCenter, ch)

            p.restore()

        elif tid == "cyberpunk":
            # ── Хаотичные неоновые лучи, "летящие" с краёв окна ───────
            # Луч начинается в точке на краю (ray["cx"], ray["cy"]) и
            # тянется внутрь интерфейса — визуально как прилетающий извне.
            for ray in self._fx_particles:
                ang = math.radians(ray["angle"])
                length = ray["len"] * max(w, h)
                x1, y1 = ray["cx"], ray["cy"]
                x2 = x1 + math.cos(ang) * length
                y2 = y1 + math.sin(ang) * length
                col = _hsv_qc(ray["hue"], 255, 255, 50)
                p.setPen(QPen(col, 1.4))
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                # Лёгкое "свечение" — вторая линия пошире и прозрачнее
                glow = _hsv_qc(ray["hue"], 255, 255, 18)
                p.setPen(QPen(glow, 4))
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        elif tid == "blood":
            # ── Поднимающиеся искры/угли ───────────────────────────────
            for s in self._fx_particles:
                alpha = max(0, min(255, int(s["life"] * 220)))
                # Угли остывают от ярко-оранжевого к тёмно-красному
                col = _hsv_qc(18 - s["life"] * 8, 255, 200 + int(s["life"]*55), alpha)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(col))
                sz = s["size"]
                p.drawEllipse(QPointF(s["x"], s["y"]), sz, sz)

    def _apply_settings(self, cfg):
        if "always_on_top" in cfg:
            # Сохраняем Qt.Window, убираем Qt.Tool если был, управляем только OnTop
            f = self.windowFlags()
            f |= Qt.Window          # гарантируем присутствие в панели задач
            f &= ~Qt.Tool           # убираем Tool если случайно попал
            if cfg["always_on_top"]:
                f |= Qt.WindowStaysOnTopHint
            else:
                f &= ~Qt.WindowStaysOnTopHint
            self.setWindowFlags(f)
            self.show()
        if "show_log" in cfg:
            self._log.setVisible(cfg["show_log"])
        # Refresh state label and txt with new language immediately
        self._lbl_state.setText(get_state_label(self._state))
        # Re-translate the current status text for known states
        _state_txt_keys = {
            "idle":       "txt_idle",
            "processing": "txt_processing",
        }
        if self._state in _state_txt_keys:
            self._lbl_txt.setText(_t(_state_txt_keys[self._state]))
        print(f"[UI] Настройки применены: {list(cfg.keys())}")
        # ── Notify main_app (and any other subscriber) to hot-reload their globals ──
        for cb in _settings_callbacks:
            try:
                cb(cfg)
            except Exception as _e:
                print(f"[UI] settings callback error: {_e}")

    # ── Mapping: state → translation key for the subtitle text ──────────────────
    # When main_app calls set_status(state, text), these states ALWAYS show
    # the translated string — the hardcoded Russian text from main_app is ignored.
    # States not in this dict (e.g. 'speaking') use the provided text as-is.
    _STD_TXT = {
        "idle":       "txt_idle",
        "processing": "txt_processing",
        "listening":  "txt_greeting",
    }

    def _on_status(self, state, text):
        self._state = state
        self._ring.set_state(state)
        self._lbl_state.setText(get_state_label(state))
        rgb = STATE_RGB.get(state, (0,180,255))
        hx  = "#{:02x}{:02x}{:02x}".format(*rgb)
        self._lbl_state.setStyleSheet(
            f"color:{hx}; letter-spacing:7px;"
            f"background:rgba({rgb[0]},{rgb[1]},{rgb[2]},15);"
            f"border:1px solid rgba({rgb[0]},{rgb[1]},{rgb[2]},50);"
            "border-radius:4px; padding:4px 0;"
        )
        # For standard states always use the translated string.
        # For contextual states (speaking, user_speaking) keep the provided text.
        translated = _t(self._STD_TXT[state]) if state in self._STD_TXT else text
        self._lbl_txt.setText(translated)

        # Show stop button only while Jarvis is speaking
        self._stop_btn.setVisible(state == "speaking")

        self.update()

        # ── Обновляем иконку и тултип трея по состоянию ──────────────
        if hasattr(self, "_tray"):
            self._tray.setIcon(_make_tray_icon(state))
            _tray_tips = {
                "idle":          "JARVIS — ожидание",
                "listening":     "JARVIS — слушаю",
                "user_speaking": "JARVIS — пользователь говорит",
                "processing":    "JARVIS — обработка",
                "speaking":      "JARVIS — говорит",
            }
            self._tray.setToolTip(_tray_tips.get(state, "JARVIS"))

    def _on_stop_clicked(self):
        """Called when the user presses the stop button during speech."""
        if _stop_tts_callback:
            _stop_tts_callback()
        self._stop_btn.hide()

    def _on_log(self, role, text):
        self._log.add(role, text)
        # Feed the detachable history window with current timestamp
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._history_win.add_entry(role, text, ts)
        # ── Сохраняем в постоянный лог (jarvis_chat_log.json) ────────
        threading.Thread(
            target=_append_chat_log, args=(role, text), daemon=True
        ).start()

    def _on_volume(self, v: float):
        self._ring.set_volume(v)

    def _do_blink(self):
        if self._state in ["idle", "listening", "user_speaking"]:
            self._blink = not self._blink
            a = 240 if self._blink else 80
            rgb = STATE_RGB.get(self._state, (0,180,255))
            self._lbl_state.setStyleSheet(
                f"color: rgba({rgb[0]},{rgb[1]},{rgb[2]},{a}); letter-spacing:7px;"
                f"background: rgba({rgb[0]},{rgb[1]},{rgb[2]},15);"
                f"border: 1px solid rgba({rgb[0]},{rgb[1]},{rgb[2]},45);"
                "border-radius: 4px; padding: 4px 0;"
            )

    def _div(self):
        l = QWidget(); l.setFixedHeight(1)
        l.setStyleSheet("background:rgba(0,160,220,40);"); return l


# ═══════════════════════════ HISTORY WINDOW ══════════════════════════
class HistoryWindow(ResizableMixin, QWidget):
    """Отдельное окно с полной историей разговора с прокруткой."""
    sig_add = pyqtSignal(str, str, str)   # role, text, timestamp

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.resize(420, 480)
        self.setMinimumSize(260, 200)
        self._resize_init()
        self._entries: list[tuple[str, str, str]] = []
        self._last_date: str = ""          # отслеживаем смену дня
        self._day_sep_widget = None        # текущий разделитель дня (перемещаем вниз группы)
        self._build()
        self.sig_add.connect(self._on_add)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header
        hdr = QWidget(); hdr.setFixedHeight(48)
        hdr.setStyleSheet("background: rgba(0,20,50,220); border-bottom: 1px solid rgba(0,180,255,80);")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16, 0, 8, 0)
        title = QLabel("◈  HISTORY")
        title.setFont(QFont("Courier New", 13, QFont.Bold))
        title.setStyleSheet("color:#00ffff; letter-spacing:4px;")
        hl.addWidget(title); hl.addStretch()

        clr_btn = QPushButton("🗑")
        clr_btn.setFixedSize(32, 32)
        clr_btn.setToolTip("Очистить историю")
        clr_btn.setStyleSheet(
            "QPushButton{background:rgba(255,60,80,20);border:1px solid rgba(255,60,80,70);"
            "border-radius:4px;color:#ff4455;font-size:16px;}"
            "QPushButton:hover{background:#ff3344;color:#fff;}")
        clr_btn.clicked.connect(lambda: _sfx("delete"))
        clr_btn.clicked.connect(self._clear)
        hl.addWidget(clr_btn)

        cls_btn = QPushButton("✕")
        cls_btn.setFixedSize(32, 32)
        cls_btn.setStyleSheet(
            "QPushButton{background:rgba(255,60,80,20);border:1px solid rgba(255,60,80,70);"
            "border-radius:4px;color:#ff4455;font-size:14px;font-weight:bold;}"
            "QPushButton:hover{background:#ff3344;color:#fff;}")
        cls_btn.clicked.connect(lambda: _sfx("close"))
        cls_btn.clicked.connect(self.hide)
        hl.addWidget(cls_btn)
        outer.addWidget(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: rgba(0,10,25,180);
                width: 14px;
                border-radius: 7px;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(0,180,255,160);
                border-radius: 6px;
                min-height: 32px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(0,220,255,220);
            }
            QScrollBar::handle:vertical:pressed {
                background: rgba(0,255,255,255);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: rgba(0,60,100,40);
            }
        """)
        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._vbox = QVBoxLayout(self._content)
        self._vbox.setContentsMargins(12, 10, 12, 10)
        self._vbox.setSpacing(8)
        # Новые сообщения вставляются в позицию 0 (сверху).
        # Stretch держится в конце (внизу) — он «распирает» пустое место
        # когда сообщений мало, чтобы они не висели посередине.
        self._vbox.addStretch()
        scroll.setWidget(self._content)
        outer.addWidget(scroll)
        self._scroll = scroll

    def _add_day_separator(self, date_str: str):
        """Создаёт виджет разделителя с датой и вставляет его в позицию 0."""
        sep = QWidget()
        sep.setStyleSheet("background: transparent;")
        sl = QHBoxLayout(sep)
        sl.setContentsMargins(0, 4, 0, 4)
        sl.setSpacing(8)

        line_l = QWidget(); line_l.setFixedHeight(1)
        line_l.setStyleSheet("background: rgba(0,180,255,50);")
        sl.addWidget(line_l, 1)

        date_lbl = QLabel(f"  {date_str}  ")
        date_lbl.setFont(QFont("Courier New", 8, QFont.Bold))
        date_lbl.setStyleSheet(
            "color: rgba(0,220,255,160);"
            "background: rgba(0,30,60,180);"
            "border: 1px solid rgba(0,180,255,60);"
            "border-radius: 8px; padding: 2px 6px;")
        sl.addWidget(date_lbl)

        line_r = QWidget(); line_r.setFixedHeight(1)
        line_r.setStyleSheet("background: rgba(0,180,255,50);")
        sl.addWidget(line_r, 1)

        self._vbox.insertWidget(0, sep)
        return sep

    # Порог символов: если сообщение длиннее — показываем кнопку
    _PREVIEW_LEN = 120

    def _on_add(self, role: str, text: str, timestamp: str):
        self._entries.append((role, text, timestamp))

        # Время из timestamp "2026-06-08 14:35:22" → "14:35"
        time_part = ""
        if len(timestamp) >= 16:
            time_part = timestamp[11:16]

        is_long = len(text) > self._PREVIEW_LEN
        preview  = text[:self._PREVIEW_LEN].rstrip() + "…" if is_long else text

        # ── Bubble ───────────────────────────────────────────────────
        bubble = QFrame()
        if role == "user":
            bubble.setStyleSheet(
                "QFrame{background:rgba(0,60,120,160);border:1px solid rgba(0,180,255,100);"
                "border-radius:8px;}")
        else:
            bubble.setStyleSheet(
                "QFrame{background:rgba(0,40,20,160);border:1px solid rgba(0,255,150,80);"
                "border-radius:8px;}")
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(2)

        # Role + time row
        role_row = QHBoxLayout()
        role_lbl = QLabel("▶ ВЫ" if role == "user" else "◀ JARVIS")
        role_lbl.setFont(QFont("Courier New", 9, QFont.Bold))
        role_lbl.setStyleSheet(
            "color:#00eeff;" if role == "user" else "color:#00ff96;")
        role_row.addWidget(role_lbl)
        role_row.addStretch()
        if time_part:
            time_lbl = QLabel(time_part)
            time_lbl.setFont(QFont("Courier New", 8))
            time_lbl.setStyleSheet("color: rgba(0,200,255,120);")
            role_row.addWidget(time_lbl)
        bl.addLayout(role_row)

        # Text label
        txt_lbl = QLabel(preview)
        txt_lbl.setFont(QFont("Courier New", 10))
        txt_lbl.setStyleSheet("color:#c8eeff;" if role == "user" else "color:#d0fff0;")
        txt_lbl.setWordWrap(True)
        bl.addWidget(txt_lbl)

        # Expand button for long messages
        if is_long:
            _expanded = [False]
            btn_color = "#00b8ff" if role == "user" else "#00e080"
            expand_btn = QPushButton("▼  показать полностью")
            expand_btn.setFont(QFont("Courier New", 8))
            expand_btn.setStyleSheet(
                f"QPushButton{{background:transparent;border:none;"
                f"color:{btn_color};text-align:left;padding:2px 0;}}"
                f"QPushButton:hover{{color:#ffffff;}}")
            expand_btn.setCursor(QCursor(Qt.PointingHandCursor))

            def _toggle(checked=False, lbl=txt_lbl, btn=expand_btn,
                        full=text, prev=preview, state=_expanded, bbl=bubble):
                if not state[0]:
                    lbl.setText(full)
                    btn.setText("▲  свернуть")
                    state[0] = True
                else:
                    lbl.setText(prev)
                    btn.setText("▼  показать полностью")
                    state[0] = False
                lbl.adjustSize()
                bbl.adjustSize()
                bbl.updateGeometry()
                self._content.adjustSize()

            def _toggle_with_sfx():
                _sfx("nav")
                _toggle()
            expand_btn.clicked.connect(_toggle_with_sfx)
            bl.addWidget(expand_btn)

        # ── Вставляем bubble в позицию 0 (самый верх) ───────────────
        self._vbox.insertWidget(0, bubble)

        # ── Разделитель дня ──────────────────────────────────────────
        date_part = timestamp[:10] if timestamp else ""
        if date_part and date_part != self._last_date:
            # Новый день: создаём разделитель ПОСЛЕ bubble.
            # bubble уже в pos 0 → разделитель вставляется в pos 0 → bubble уходит в pos 1.
            # Итог: [разделитель, bubble, ...старые сообщения...]
            self._last_date = date_part
            try:
                dt = _dt.datetime.strptime(date_part, "%Y-%m-%d")
                months = ["января","февраля","марта","апреля","мая","июня",
                          "июля","августа","сентября","октября","ноября","декабря"]
                label = f"{dt.day:02d} {months[dt.month-1]} {dt.year}"
            except Exception:
                label = date_part
            self._day_sep_widget = self._add_day_separator(label)
        elif self._day_sep_widget is not None:
            # Тот же день: bubble вставился в pos 0, двигаем разделитель в pos 0
            # чтобы он снова оказался выше bubble.
            self._vbox.removeWidget(self._day_sep_widget)
            self._vbox.insertWidget(0, self._day_sep_widget)

        # Прокручиваем наверх чтобы показать новое сообщение
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(0))

    def _clear(self):
        """Очищает UI, _entries, сбрасывает трекинг даты и стирает jarvis_chat_log.json."""
        self._entries.clear()
        self._last_date = ""
        self._day_sep_widget = None
        # Удаляем все виджеты кроме последнего stretch
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # Очищаем файл лога
        try:
            with open(_CHAT_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
            print("[UI] История очищена: jarvis_chat_log.json")
        except Exception as e:
            print(f"[UI] Ошибка очистки лога: {e}")

    def add_entry(self, role: str, text: str, timestamp: str = ""):
        """Thread-safe: emit signal."""
        if not timestamp:
            timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.sig_add.emit(role, text, timestamp)

    def paintEvent(self, _):
        p = QPainter(self); w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, C_BG0); g.setColorAt(1, C_BG1)
        p.fillRect(0, 0, w, h, QBrush(g))
        p.setPen(QPen(_qc(0, 160, 220, 150), 1.2))
        p.setBrush(Qt.NoBrush); p.drawRect(0, 0, w-1, h-1)
        p.end()



# ════════════════════════════════════════════════════════════════════
#  Helper: create a colored J-icon pixmap for tray
# ════════════════════════════════════════════════════════════════════
def _make_tray_icon(state: str = "idle") -> QIcon:
    """
    Рисует иконку трея с цветом, соответствующим текущему состоянию:
      idle          → синий   (0, 180, 255)
      listening     → мятный  (0, 255, 160)
      user_speaking → жёлтый  (255, 215, 0)
      processing    → оранжевый (255, 110, 0)
      speaking      → зелёный (0, 255, 150)
    """
    rgb = STATE_RGB.get(state, (0, 180, 255))
    R, G, B = rgb

    px = QPixmap(64, 64)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)

    # Тёмный фон круга
    p.setBrush(QBrush(_qc(4, 10, 25, 235)))
    p.setPen(QPen(_qc(R, G, B, 200), 3))
    p.drawEllipse(2, 2, 60, 60)

    # Буква J
    p.setPen(_qc(R, G, B, 255))
    f = QFont("Courier New", 32, QFont.Bold)
    p.setFont(f)
    from PyQt5.QtCore import QRect
    p.drawText(QRect(0, 2, 64, 64), Qt.AlignCenter, "J")

    # Маленькая цветная точка-индикатор (нижний правый угол)
    dot_r = 9
    p.setBrush(QBrush(_qc(R, G, B, 255)))
    p.setPen(QPen(_qc(4, 10, 25, 200), 1.5))
    p.drawEllipse(64 - dot_r*2 - 1, 64 - dot_r*2 - 1, dot_r*2, dot_r*2)

    p.end()
    return QIcon(px)


# ═══════════════════════════ PUBLIC API ═════════════════════════════
def _apply_opacity_all(opacity: float):
    """Applies window opacity to ALL open windows (overlay, settings, history)."""
    opacity = max(0.4, min(1.0, opacity))
    targets = [_window,
               _window._settings    if _window else None,
               _window._history_win if _window else None]
    def _do():
        for win in targets:
            if win is not None:
                try:
                    win.setWindowOpacity(opacity)
                except Exception:
                    pass
    _do()
    try:
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(50,  _do)
        QTimer.singleShot(300, _do)
    except Exception:
        pass


_app    = None
_window = None
_history_window = None
_stop_tts_callback = None   # set by main_app via register_stop_tts_callback


def register_stop_tts_callback(fn):
    """Register a function to call when the user presses the stop button."""
    global _stop_tts_callback
    _stop_tts_callback = fn


# ── Update dialogs (thread-safe — schedule on Qt main thread) ────────────────

def ask_update_dialog(version: str, file_count: int, on_confirm, on_cancel=None):
    """
    Shows "Update available — install?" dialog.
    on_confirm() called if user says YES.
    on_cancel()  called if user says NO.
    Thread-safe: schedules on Qt main thread via QTimer.
    """
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(0, lambda: _show_update_confirm(version, file_count,
                                                       on_confirm, on_cancel))


def ask_restart_dialog(updated_count: int):
    """
    Shows "Update done — restart now?" dialog.
    Thread-safe.
    """
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(0, lambda: _show_restart_confirm(updated_count))


def _make_dialog(width=420, height=220):
    """Creates a styled frameless dialog parented to the overlay."""
    if _window is None:
        return None
    d = QWidget(_window, Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    d.setFixedSize(width, height)
    d.setAttribute(Qt.WA_TranslucentBackground)
    d.setStyleSheet("background: transparent;")
    g = _window.geometry()
    d.move(g.x() + (g.width()  - width)  // 2,
           g.y() + (g.height() - height) // 2)

    def _paint(e, self=d):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(4, 10, 25, 245)))
        p.setPen(QPen(QColor(0, 200, 255, 200), 1.5))
        p.drawRoundedRect(1, 1, self.width()-2, self.height()-2, 10, 10)
        # Corner accents
        p.setPen(QPen(QColor(0, 255, 255, 180), 2))
        L = 14
        for x, y, dx, dy in [
            (2,2,L,0),(2,2,0,L),(self.width()-3,2,-L,0),(self.width()-3,2,0,L),
            (2,self.height()-3,L,0),(2,self.height()-3,0,-L),
            (self.width()-3,self.height()-3,-L,0),(self.width()-3,self.height()-3,0,-L),
        ]:
            p.drawLine(x, y, x+dx, y+dy)
        p.end()
    d.paintEvent = _paint
    return d


def _make_btn(text, color_hex, hover_bg=None):
    btn = QPushButton(text)
    btn.setFixedHeight(38)
    hb = hover_bg or color_hex
    btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent;
            border: 1px solid {color_hex};
            border-radius: 5px;
            color: {color_hex};
            font-family: 'Courier New';
            font-size: 12px;
            font-weight: bold;
            letter-spacing: 1px;
        }}
        QPushButton:hover {{ background: {hb}; color: #000; }}
        QPushButton:pressed {{ background: {hb}; color: #000; }}
    """)
    return btn


def _show_update_confirm(version: str, file_count: int, on_confirm, on_cancel):
    d = _make_dialog(440, 210)
    if d is None:
        return

    vl = QVBoxLayout(d)
    vl.setContentsMargins(22, 18, 22, 18)
    vl.setSpacing(10)

    title = QLabel("⬆  ДОСТУПНО ОБНОВЛЕНИЕ")
    title.setFont(QFont("Courier New", 13, QFont.Bold))
    title.setStyleSheet("color:#00ffff; letter-spacing:3px; background:transparent;")
    title.setAlignment(Qt.AlignCenter)
    vl.addWidget(title)

    info = QLabel(f"Версия:  {version}\nИзменено файлов:  {file_count}\n\nУстановить обновление?")
    info.setFont(QFont("Courier New", 10))
    info.setStyleSheet("color:#a0d8ff; background:transparent;")
    info.setAlignment(Qt.AlignCenter)
    info.setWordWrap(True)
    vl.addWidget(info)

    row = QHBoxLayout(); row.setSpacing(12)
    btn_yes = _make_btn("✓  УСТАНОВИТЬ", "#00ff88")
    btn_no  = _make_btn("✕  ОТМЕНА",     "#ff5555")

    def _yes():
        d.close()
        if on_confirm: on_confirm()

    def _no():
        d.close()
        if on_cancel: on_cancel()

    btn_yes.clicked.connect(_yes)
    btn_no.clicked.connect(_no)
    row.addWidget(btn_yes); row.addWidget(btn_no)
    vl.addLayout(row)

    d.show(); d.raise_()


def _show_restart_confirm(updated_count: int):
    d = _make_dialog(440, 200)
    if d is None:
        return

    vl = QVBoxLayout(d)
    vl.setContentsMargins(22, 18, 22, 18)
    vl.setSpacing(10)

    title = QLabel("✓  ОБНОВЛЕНИЕ УСТАНОВЛЕНО")
    title.setFont(QFont("Courier New", 13, QFont.Bold))
    title.setStyleSheet("color:#00ff88; letter-spacing:3px; background:transparent;")
    title.setAlignment(Qt.AlignCenter)
    vl.addWidget(title)

    info = QLabel(f"Обновлено файлов: {updated_count}\n\nДля применения требуется перезапуск.\nПерезапустить JARVIS сейчас?")
    info.setFont(QFont("Courier New", 10))
    info.setStyleSheet("color:#a0d8ff; background:transparent;")
    info.setAlignment(Qt.AlignCenter)
    info.setWordWrap(True)
    vl.addWidget(info)

    row = QHBoxLayout(); row.setSpacing(12)
    btn_now   = _make_btn("⟳  ПЕРЕЗАПУСТИТЬ", "#00b4ff")
    btn_later = _make_btn("✕  ПОЗЖЕ",          "#ff5555")

    def _restart():
        d.close()
        import os, sys, subprocess
        from pathlib import Path
        base = Path(sys.argv[0]).parent
        for launcher in ["Run_AI.bat", "Run_AI.exe"]:
            p = base / launcher
            if p.exists():
                subprocess.Popen([str(p)], shell=True)
                break
        else:
            subprocess.Popen([sys.executable, str(base / "main_app.py")])
        os._exit(0)

    btn_now.clicked.connect(_restart)
    btn_later.clicked.connect(d.close)
    row.addWidget(btn_now); row.addWidget(btn_later)
    vl.addLayout(row)

    d.show(); d.raise_()


def start_ui():
    global _app, _window, _history_window

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    _app = QApplication.instance() or QApplication(sys.argv)
    _window = JarvisOverlay()
    _history_window = _window._history_win
    _window.show()

    # Загружаем историю прошлых сессий из jarvis_chat_log.json
    past = _load_history_from_txt()
    if past:
        def _load_past():
            for role, text, ts in past:
                _history_window.add_entry(role, text, ts)
        QTimer.singleShot(300, _load_past)


def run_ui_blocking():
    """Блокирующий вызов Qt event loop. Вызывать в конце main()."""
    if _app:
        _app.exec_()


def set_status(state: str, text: str = ""):
    """
    'idle'          — Ожидание (синий)
    'listening'     — Слушаю (мятный)
    'user_speaking' — Голос пользователя (жёлтый)
    'processing'    — Обработка (оранжевый)
    'speaking'      — Ответ Джарвиса (зелёный)
    """
    if _window:
        _window.sig_set_status.emit(state, text)


def add_log(role: str, text: str):
    """
    Добавляет сообщение в HUD-лог, окно истории и постоянный файл jarvis_chat_log.json.
    Безопасно вызывать из любого потока.
    """
    if _window:
        _window.sig_add_log.emit(role, text)
    else:
        # Окно ещё не готово — пишем в файл напрямую
        threading.Thread(
            target=_append_chat_log, args=(role, text), daemon=True
        ).start()


def save_log(role: str, text: str):
    """
    Сохраняет сообщение ТОЛЬКО в постоянный лог (без отображения в HUD).
    Используется для системных сообщений: «Слушаю», «Выполняю», ошибки и т.д.
    """
    threading.Thread(
        target=_append_chat_log, args=(role, text), daemon=True
    ).start()


def get_chat_log(limit: int = 100) -> list:
    """Возвращает последние N записей из постоянного лога. Безопасно из любого потока."""
    try:
        if os.path.exists(_CHAT_LOG_FILE):
            with open(_CHAT_LOG_FILE, "r", encoding="utf-8") as f:
                log = json.load(f)
            return log[-limit:] if isinstance(log, list) else []
    except Exception:
        pass
    return []


def set_volume(v: float):
    """Передаёт реальный уровень громкости микрофона [0.0 … 1.0]."""
    if _window:
        _window.sig_set_volume.emit(float(v))


def get_config() -> dict:
    """Returns current saved config. Safe to call from any thread."""
    return _load_config()


def get_custom_wake_word():
    """Returns the custom wake phrase or None (= use default 'hey jarvis')."""
    return _load_commands().get("custom_wake_word") or None
