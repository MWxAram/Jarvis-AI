"""
jarvis_vip.py — проверка VIP-кода кастомизации через бэкенд JARVIS
(POST /api/vip/verify-code на localhost:8000, тот же эндпоинт, что
использует сайт). Без сторонних зависимостей — только urllib из
стандартной библиотеки.

Логика:
  - Код, который пользователь один раз ввёл в настройках, сохраняется
    в jarvis_config.json (поле "vip_code") — чтобы не вводить его заново
    при каждом перезапуске программы.
  - При каждом запуске программы (и при каждом ручном вводе кода)
    сохранённый код повторно проверяется через сервер — это нужно,
    чтобы вовремя замечать истёкшие или отозванные администратором коды.
  - Если сервера/интернета нет прямо сейчас — не блокируем пользователя
    наглухо: разрешаем доступ по результату ПОСЛЕДНЕЙ успешной проверки
    (офлайн-режим), если она была не слишком давно. Как только связь
    появится, следующая проверка снова обратится к серверу.
"""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

VIP_API_URL = "http://localhost:8000/api/vip/verify-code"
REQUEST_TIMEOUT_SECONDS = 5

# Офлайн-режим: если сервер недоступен, но последняя успешная проверка
# была не более этого времени назад — пускаем по кэшу. Не делаем это
# окно бесконечным, чтобы отозванный/просроченный код не работал вечно
# просто потому, что комьютер был офлайн в момент отзыва.
OFFLINE_GRACE = timedelta(days=3)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value):
    """Парсит ISO-дату из ответа сервера (или None, если ловит ошибку)."""
    if not value:
        return None
    try:
        # Сервер отдаёт naive datetime без 'Z'/offset (UTC) — считаем как UTC
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class VipCheckResult:
    """Результат проверки VIP-кода (онлайн либо из офлайн-кэша)."""

    def __init__(self, valid: bool, message: str, expires_at=None,
                 plan=None, from_cache: bool = False, network_error: bool = False):
        self.valid = valid
        self.message = message
        self.expires_at = expires_at  # datetime или None (None = бессрочно, если valid)
        self.plan = plan
        self.from_cache = from_cache
        self.network_error = network_error


def verify_code_online(code: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> VipCheckResult:
    """
    Делает реальный запрос к серверу. Бросает исключение наружу никогда не
    должна — все сетевые ошибки превращаются в network_error=True, чтобы
    вызывающий код мог принять решение об офлайн-доступе.
    """
    payload = json.dumps({"key": code}).encode("utf-8")
    req = urllib.request.Request(
        VIP_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return VipCheckResult(
                valid=bool(data.get("valid")),
                message=data.get("message", ""),
                expires_at=_parse_iso(data.get("expires_at")),
                plan=data.get("plan"),
            )
    except urllib.error.HTTPError as e:
        # Сервер ответил, но с кодом ошибки (400/422/500) — например,
        # неверный формат ключа. Это НЕ network_error — сервер доступен,
        # просто код некорректен.
        try:
            body = e.read().decode("utf-8")
            data = json.loads(body)
            detail = data.get("detail")
            if isinstance(detail, list) and detail:
                detail = detail[0].get("msg", "Неверный код")
            msg = detail or "Код не найден или некорректен"
        except Exception:
            msg = "Код не найден или некорректен"
        return VipCheckResult(valid=False, message=str(msg))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        # Сервер недоступен (нет интернета, бэкенд выключен и т.д.)
        return VipCheckResult(
            valid=False,
            message="Нет связи с сервером JARVIS",
            network_error=True,
        )
    except Exception as e:
        return VipCheckResult(valid=False, message=f"Ошибка проверки кода: {e}")


def verify_code_with_offline_fallback(code: str, config: dict) -> VipCheckResult:
    """
    Основная точка входа: пробует проверить код онлайн. Если сервер
    недоступен — смотрит, был ли этот же код успешно проверен недавно
    (поля vip_code / vip_last_check_at / vip_last_valid в конфиге),
    и если да — временно пускает по кэшу.
    """
    result = verify_code_online(code)

    if not result.network_error:
        return result

    # ── Сервер недоступен — пробуем офлайн-кэш ───────────────────────────
    cached_code = config.get("vip_code")
    cached_valid = config.get("vip_last_valid")
    cached_check_at = _parse_iso(config.get("vip_last_check_at"))
    cached_expires_at = _parse_iso(config.get("vip_last_expires_at"))

    if cached_code != code or not cached_valid or cached_check_at is None:
        # Этот код раньше не проверялся успешно — нет кэша, на который опереться
        return result

    if _now_utc() - cached_check_at > OFFLINE_GRACE:
        # Кэш слишком старый — не доверяем ему, требуем интернет
        return result

    if cached_expires_at is not None and cached_expires_at <= _now_utc():
        # По кэшированным данным сам код уже истёк
        return VipCheckResult(
            valid=False,
            message="Срок действия кода истёк",
            expires_at=cached_expires_at,
        )

    return VipCheckResult(
        valid=True,
        message="Нет связи с сервером — доступ разрешён по последней успешной проверке",
        expires_at=cached_expires_at,
        plan=config.get("vip_last_plan"),
        from_cache=True,
    )


def build_cache_update(code: str, result: VipCheckResult) -> dict:
    """
    Формирует словарь полей для записи в jarvis_config.json после проверки.
    Кэш обновляем только по результатам РЕАЛЬНОГО онлайн-запроса
    (from_cache=False) — иначе офлайн-показания затирали бы друг друга.
    """
    if result.from_cache:
        return {}

    return {
        "vip_code": code,
        "vip_last_valid": result.valid,
        "vip_last_check_at": _now_utc().isoformat(),
        "vip_last_expires_at": result.expires_at.isoformat() if result.expires_at else None,
        "vip_last_plan": result.plan,
    }
