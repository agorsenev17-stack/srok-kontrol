
import os
import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import ClientSession


DATABASE_URL = os.environ["DATABASE_URL"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

REMINDER_DAYS = (3, 1, 0)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def send_telegram(session, user_id, message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    async with session.post(
        url,
        json={
            "chat_id": user_id,
            "text": message
        }
    ) as response:
        result = await response.json()

        if response.status != 200 or not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")


def make_message(name, days):
    if days == 3:
        return (
            f"🔔 Напоминание о сроке годности\n\n"
            f"Продукт: {name}\n"
            f"До указанной даты окончания срока годности осталось 3 дня.\n\n"
            f"Проверь условия хранения и состояние упаковки."
        )

    if days == 1:
        return (
            f"⚠️ Скоро заканчивается срок годности!\n\n"
            f"Продукт: {name}\n"
            f"До указанной даты остался 1 день.\n\n"
            f"Проверь маркировку и условия хранения."
        )

    return (
        f"🚨 Сегодня указана дата окончания срока годности!\n\n"
        f"Продукт: {name}\n\n"
        f"Не употребляй продукт после окончания срока годности. "
        f"Учитывай также условия хранения и указания производителя."
    )


async def main():
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_logs (
                product_id BIGINT NOT NULL,
                reminder_days INTEGER NOT NULL,
                expiry_date DATE NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (product_id, reminder_days, expiry_date)
            )
        """)

        products = await conn.fetch("""
            SELECT
                p.id,
                p.user_id,
                p.name,
                p.expiry_date,
                COALESCE(s.timezone, 'Europe/Moscow') AS timezone,
                COALESCE(s.reminder_hour, 9) AS reminder_hour
            FROM products p
            LEFT JOIN user_settings s
                ON s.user_id = p.user_id
        """)

        async with ClientSession() as session:
            for product in products:
                try:
                    timezone = ZoneInfo(product["timezone"])
                    local_now = datetime.now(timezone)

                    if local_now.hour != product["reminder_hour"]:
                        continue

                    today = local_now.date()
                    days_left = (product["expiry_date"] - today).days

                    if days_left not in REMINDER_DAYS:
                        continue

                    message = make_message(
                        product["name"],
                        days_left
                    )

                    # Не отправляем повторное уведомление
                    # для одного продукта и одной даты.
                    already_sent = await conn.fetchval("""
                        SELECT EXISTS (
                            SELECT 1
                            FROM reminder_logs
                            WHERE product_id = $1
                              AND reminder_days = $2
                              AND expiry_date = $3
                        )
                    """,
                        product["id"],
                        days_left,
                        product["expiry_date"]
                    )

                    if already_sent:
                        continue

                    await send_telegram(
                        session,
                        product["user_id"],
                        message
                    )

                    await conn.execute("""
                        INSERT INTO reminder_logs (
                            product_id,
                            reminder_days,
                            expiry_date
                        )
                        VALUES ($1, $2, $3)
                        ON CONFLICT DO NOTHING
                    """,
                        product["id"],
                        days_left,
                        product["expiry_date"]
                    )

                    logger.info(
                        "Reminder sent: product_id=%s, days=%s",
                        product["id"],
                        days_left
                    )

                except Exception:
                    logger.exception(
                        "Failed to process product %s",
                        product["id"]
                    )

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
