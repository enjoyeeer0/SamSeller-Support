import getpass
import json
import os
from pathlib import Path


SECRETS_FILE = Path("secrets.local.json")


def _try_get_from_env() -> tuple[str, str, int] | None:
    golden_key = os.getenv("FUNPAY_GOLDEN_KEY")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_admin_id = os.getenv("TELEGRAM_ADMIN_ID")

    if not (golden_key and telegram_token and telegram_admin_id):
        return None
    return golden_key, telegram_token, int(telegram_admin_id)


def _try_get_from_file() -> tuple[str, str, int] | None:
    if not SECRETS_FILE.exists():
        return None

    raw = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    golden_key = str(raw.get("FUNPAY_GOLDEN_KEY", "")).strip()
    telegram_token = str(raw.get("TELEGRAM_BOT_TOKEN", "")).strip()
    admin_raw = str(raw.get("TELEGRAM_ADMIN_ID", "")).strip()
    if not (golden_key and telegram_token and admin_raw):
        return None
    return golden_key, telegram_token, int(admin_raw)


def _prompt_admin_id() -> int:
    while True:
        value = input("Enter Telegram Admin ID: ").strip()
        if not value.isdigit():
            print("Admin ID must be a number.")
            continue
        admin_id = int(value)
        if admin_id <= 0:
            print("Admin ID must be > 0.")
            continue
        return admin_id


def _run_setup_wizard() -> tuple[str, str, int]:
    print("First run setup")
    print("Enter your credentials. They will be saved to secrets.local.json")

    while True:
        golden_key = getpass.getpass("Enter FunPay Golden Key: ").strip()
        if len(golden_key) < 16:
            print("Golden Key looks too short. Try again.")
            continue
        break

    while True:
        telegram_token = getpass.getpass("Enter Telegram Bot Token: ").strip()
        if len(telegram_token) < 20 or ":" not in telegram_token:
            print("Token format looks invalid. Try again.")
            continue
        break

    admin_id = _prompt_admin_id()

    data = {
        "FUNPAY_GOLDEN_KEY": golden_key,
        "TELEGRAM_BOT_TOKEN": telegram_token,
        "TELEGRAM_ADMIN_ID": admin_id,
    }
    SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    print("Saved local settings to secrets.local.json")
    return golden_key, telegram_token, admin_id


def get_settings() -> tuple[str, str, int]:
    """Get settings in order: env vars -> local secrets file -> interactive wizard."""
    from_env = _try_get_from_env()
    if from_env:
        return from_env

    from_file = _try_get_from_file()
    if from_file:
        return from_file

    return _run_setup_wizard()
