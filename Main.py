import logging
import threading

from app.config import get_settings
from app.logging_setup import setup_logging
from app.services.funpay_listener import FunPayListener
from app.services.telegram_bridge import TelegramBridge


def main() -> None:
    log_file = setup_logging()
    logging.info("Логирование инициализировано: %s", log_file)

    golden_key, telegram_token, telegram_admin_ids = get_settings()

    tg_bridge = TelegramBridge(telegram_token, telegram_admin_ids)
    fp = FunPayListener(golden_key, tg_bridge)
    tg_bridge.set_sender(fp.send_message)

    tg_thread = threading.Thread(target=tg_bridge.run_polling_forever, daemon=True)
    tg_thread.start()
    logging.info("Поток Telegram polling запущен")

    tg_health_thread = threading.Thread(target=tg_bridge.run_health_ping_forever, daemon=True)
    tg_health_thread.start()
    logging.info("Поток health ping запущен")

    logging.info("Мост запущен. Слушаю FunPay и Telegram...")
    fp.run_forever()


if __name__ == "__main__":
    main()

#git push origin :refs/tags/v1.0.0 
#git tag -d v1.0.0
#git tag v1.0.0
#git push origin v1.0.0