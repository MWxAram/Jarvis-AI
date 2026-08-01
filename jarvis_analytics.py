"""
jarvis_analytics.py — приватный сборщик статистики использования JARVIS.

Что собирается:
  - ТОЛЬКО тип действия (какая функция была использована) + время (UTC).

Что НИКОГДА не собирается и не отправляется:
  - текст голосового запроса / ответа Джарвиса;
  - имена открытых/закрытых программ, сайтов, папок, заметок и т.д.;
  - какой-либо анонимный/уникальный ID установки или пользователя —
    события уходят единым потоком без привязки к конкретной машине.

Надёжная доставка ("пытаться, пока не получится"):
  - track(event_type) сразу дописывает событие в локальную очередь на
    диске (jarvis_analytics_queue.json рядом с программой) — событие
    переживает даже аварийное завершение процесса (os._exit).
  - Перед выключением JARVIS вызывается flush() — одним запросом
    пытается отправить всю накопленную очередь на сервер:
      • успех  → очередь на диске очищается;
      • сервер недоступен / ошибка сети → очередь остаётся на диске
        как есть, ничего не теряется.
  - При следующем запуске JARVIS снова вызывается flush() — досылает
    всё, что не ушло в прошлый раз, вместе с новыми событиями. Если
    опять не получилось — просто копится дальше и повторяется перед
    каждым следующим выключением/запуском, бесконечно, пока не будет
    успешной отправки.
"""
from __future__ import annotations
import os
import sys
import json
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Тот же бэкенд, что и jarvis_vip.py / updater.py (localhost:8000, тот же
# сервер, что использует сайт).
ANALYTICS_API_URL = "http://localhost:8000/api/analytics/events"

_SEND_TIMEOUT_SECONDS = 4     # короткий таймаут — не задерживаем выключение JARVIS
_MAX_QUEUE_EVENTS = 5000      # защита от бесконечного роста файла в долгом офлайне

if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_QUEUE_PATH = os.path.join(_BASE_DIR, "jarvis_analytics_queue.json")

_lock = threading.Lock()   # защищает файл очереди от гонок между потоками


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_queue() -> list:
    try:
        with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_queue(events: list):
    try:
        with open(_QUEUE_PATH, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ANALYTICS] Не удалось записать очередь на диск: {e}")


def track(event_type: str):
    """
    Фиксирует факт использования функции: тип действия + время (UTC).
    Ничего кроме этого не сохраняется (см. заголовок модуля).
    Пишется сразу на диск — переживает падение/принудительное
    завершение процесса (os._exit), которое JARVIS использует при
    выходе через трей.
    """
    if not event_type:
        return
    event = {"type": str(event_type), "ts": _now_iso()}
    with _lock:
        events = _read_queue()
        events.append(event)
        # Если JARVIS месяцами работал без связи с сервером — обрезаем
        # самые старые события, а не блокируем сбор новых.
        if len(events) > _MAX_QUEUE_EVENTS:
            events = events[-_MAX_QUEUE_EVENTS:]
        _write_queue(events)


def _send(events: list, timeout: float) -> bool:
    """Один POST-запрос со всей очередью целиком. True — сервер принял (2xx)."""
    payload = json.dumps({"events": events}).encode("utf-8")
    req = urllib.request.Request(
        ANALYTICS_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= status < 300
    except Exception as e:
        # Любая сетевая ошибка / сервер выключен / таймаут — просто
        # пробуем ещё раз в следующий flush(). Никогда не бросаем исключение
        # наружу, чтобы не мешать нормальной работе/выключению JARVIS.
        print(f"[ANALYTICS] Отправка не удалась, попробуем позже: {e}")
        return False


def flush(timeout: float = _SEND_TIMEOUT_SECONDS) -> bool:
    """
    Пытается одним запросом отправить всю накопленную очередь событий.
      - Очередь пуста              → ничего не делает, возвращает True.
      - Отправка успешна           → очередь на диске очищается, True.
      - Сервер недоступен / ошибка → очередь остаётся на диске нетронутой
        (включая события, которые накопятся дальше — они просто
        подхватятся следующим вызовом flush()), возвращается False.

    Вызывается в двух местах:
      1) при старте JARVIS — досылает всё, что не отправилось в прошлый раз;
      2) перед выключением JARVIS (трей → «Выключить JARVIS») — отправляет
         всё за текущую сессию.
    Если оба раза не получилось — ничего не пропадает: очередь просто
    растёт на диске и повторная попытка происходит на каждом следующем
    старте/выключении, пока сервер снова не станет доступен.
    """
    with _lock:
        events = _read_queue()
        if not events:
            return True
        ok = _send(events, timeout)
        if ok:
            _write_queue([])
        return ok


def flush_async(timeout: float = _SEND_TIMEOUT_SECONDS):
    """
    Неблокирующая версия flush() — используется при СТАРТЕ JARVIS, чтобы
    попытка досылки старой очереди не задерживала появление UI. Для
    выключения используем блокирующий flush() напрямую (у нас есть
    несколько секунд до os._exit, и мы хотим реально попытаться отправить,
    а не просто выстрелить и забыть).
    """
    threading.Thread(target=flush, args=(timeout,), daemon=True,
                      name="AnalyticsFlush").start()