import json
import os
import re
from pathlib import Path


SECRETS_FILE = Path("secrets.local.json")


def _parse_admin_ids_from_text(raw: str) -> list[int] | None:
    if not raw:
        return None

    parts = [p.strip() for p in re.split(r"[;,\s]+", raw) if p.strip()]
    if not parts:
        return None

    ids: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        value = int(part)
        if value <= 0:
            return None
        if value not in ids:
            ids.append(value)

    if len(ids) > 3:
        ids = ids[:3]
    if not ids:
        return None
    return ids


def _parse_admin_ids_from_json(raw_value: object) -> list[int] | None:
    if raw_value is None:
        return None

    if isinstance(raw_value, int):
        return [raw_value] if raw_value > 0 else None

    if isinstance(raw_value, str):
        return _parse_admin_ids_from_text(raw_value)

    if isinstance(raw_value, list):
        ids: list[int] = []
        for item in raw_value:
            if isinstance(item, int):
                value = item
            elif isinstance(item, str) and item.strip().isdigit():
                value = int(item.strip())
            else:
                return None

            if value <= 0:
                return None
            if value not in ids:
                ids.append(value)

        if len(ids) > 3:
            ids = ids[:3]
        if not ids:
            return None
        return ids

    return None


def _try_get_from_env() -> tuple[str, str, list[int]] | None:
    golden_key = os.getenv("FUNPAY_GOLDEN_KEY")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_admin_ids_raw = os.getenv("TELEGRAM_ADMIN_IDS") or os.getenv("TELEGRAM_ADMIN_ID")
    admin_ids = _parse_admin_ids_from_text(telegram_admin_ids_raw or "")

    if not (golden_key and telegram_token and admin_ids):
        return None
    return golden_key, telegram_token, admin_ids


def _try_get_from_file() -> tuple[str, str, list[int]] | None:
    if not SECRETS_FILE.exists():
        return None

    raw = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    golden_key = str(raw.get("FUNPAY_GOLDEN_KEY", "")).strip()
    telegram_token = str(raw.get("TELEGRAM_BOT_TOKEN", "")).strip()
    admin_ids = _parse_admin_ids_from_json(raw.get("TELEGRAM_ADMIN_IDS"))
    if not admin_ids:
        admin_ids = _parse_admin_ids_from_json(raw.get("TELEGRAM_ADMIN_ID"))

    if not (golden_key and telegram_token and admin_ids):
        return None
    return golden_key, telegram_token, admin_ids


def _prompt_admin_ids() -> list[int]:
    while True:
        value = input("Введите 1-3 Telegram Admin ID (через запятую): ").strip()
        parsed = _parse_admin_ids_from_text(value)
        if not parsed:
            print("Введите от 1 до 3 корректных числовых ID через запятую.")
            continue
        return parsed


def _run_setup_wizard() -> tuple[str, str, list[int]]:
    print("Первичная настройка")
    print("Введите данные. Они будут сохранены в secrets.local.json")
    print("Подсказка: вставка через Ctrl+V.")

    while True:
        golden_key = input("Введите FunPay Golden Key: ").strip()
        if len(golden_key) < 16:
            print("Golden Key выглядит слишком коротким. Попробуйте снова.")
            continue
        break

    while True:
        telegram_token = input("Введите Telegram Bot Token: ").strip()
        if len(telegram_token) < 20 or ":" not in telegram_token:
            print("Неверный формат токена. Попробуйте снова.")
            continue
        break

    admin_ids = _prompt_admin_ids()

    data = {
        "FUNPAY_GOLDEN_KEY": golden_key,
        "TELEGRAM_BOT_TOKEN": telegram_token,
        "TELEGRAM_ADMIN_IDS": admin_ids,
        "TELEGRAM_ADMIN_ID": admin_ids[0],
    }
    SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    print("Локальные настройки сохранены в secrets.local.json")
    return golden_key, telegram_token, admin_ids


def get_settings() -> tuple[str, str, list[int]]:
    """Get settings in order: env vars -> local secrets file -> interactive wizard."""
    from_env = _try_get_from_env()
    if from_env:
        return from_env

    from_file = _try_get_from_file()
    if from_file:
        return from_file

    return _run_setup_wizard()
