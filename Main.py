import logging
import threading

from app.config import get_settings
from app.services.funpay_listener import FunPayListener
from app.services.telegram_bridge import TelegramBridge


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    golden_key, telegram_token, telegram_admin_id = get_settings()

    tg_bridge = TelegramBridge(telegram_token, telegram_admin_id)
    fp = FunPayListener(golden_key, tg_bridge)
    tg_bridge.set_sender(fp.send_message)

    tg_thread = threading.Thread(target=tg_bridge.run_polling_forever, daemon=True)
    tg_thread.start()

    tg_health_thread = threading.Thread(target=tg_bridge.run_health_ping_forever, daemon=True)
    tg_health_thread.start()

    logging.info("Мост запущен. Слушаю FunPay и Telegram...")
    fp.run_forever()


if __name__ == "__main__":
    main()
