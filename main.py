
import os
import logging
from datetime import date, datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiohttp import web, ClientSession

TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db = None
http = None
user_states = {}


async def telegram(method, data):
    async with http.post(
        f"{BASE_URL}/{method}", json=data
    ) as response:
        return await response.json()


async def send(chat_id, text):
    await telegram("sendMessage", {
        "chat_id": chat_id,
        "text": text
    })


async def setup(app):
    global db, http

    http = ClientSession()

    parts = urlsplit(DATABASE_URL)
    params = dict(parse_qsl(parts.query))
    params.pop("sslmode", None)
    params.pop("channel_binding", None)

    dsn = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(params),
        parts.fragment
    ))

    db = await asyncpg.create_pool(
        dsn=dsn,
        ssl="require"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            expiry_date DATE NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if render_url:
        result = await telegram("setWebhook", {
            "url": f"{render_url}/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"]
        })

        if result.get("ok"):
            logger.info("Webhook подключён")
        else:
            logger.error("Ошибка подключения Webhook: %s", result)


async def cleanup(app):
    if db:
        await db.close()
    if http:
        await http.close()


async def start(chat_id):
    await send(
        chat_id,
        "🥫 Добро пожаловать в СрокКонтроль!\n\n"
        "Я помогу следить за сроками годности продуктов.\n\n"
        "Команды:\n"
        "/add — добавить продукт\n"
        "/list — мои продукты\n"
        "/help — помощь"
    )


async def add_product(chat_id, user_id):
    user_states[user_id] = {"step": "name"}
    await send(
        chat_id,
        "🥫 Напиши название продукта:"
    )


async def show_products(chat_id, user_id):
    rows = await db.fetch("""
        SELECT name, expiry_date
        FROM products
        WHERE user_id = $1
        ORDER BY expiry_date
    """, user_id)

    if not rows:
        await send(chat_id, "Твоя корзина пока пуста.")
        return

    today = date.today()
    lines = ["📦 Твои продукты:\n"]

    for row in rows:
        remaining = (row["expiry_date"] - today).days

        if remaining < 0:
            status = f"⚠️ Срок истёк {abs(remaining)} дн. назад"
        elif remaining == 0:
            status = "⚠️ Последний день по указанной дате"
        else:
            status = f"Осталось дней: {remaining}"

        lines.append(
            f"🥫 {row['name']}\n"
            f"Дата: {row['expiry_date'].strftime('%d.%m.%Y')}\n"
            f"{status}\n"
        )

    await send(chat_id, "\n".join(lines))


async def handle_text(chat_id, user_id, text):
    state = user_states.get(user_id)

    if not state:
        await send(
            chat_id,
            "Не понял команду. Используй /add или /list."
        )
        return

    if state["step"] == "name":
        state["name"] = text.strip()
        state["step"] = "date"

        await send(
            chat_id,
            "📅 Теперь введи дату окончания срока годности.\n\n"
            "Например: 25.12.2026"
        )
        return

    if state["step"] == "date":
        try:
            expiry = datetime.strptime(
                text.strip(), "%d.%m.%Y"
            ).date()
        except ValueError:
            await send(
                chat_id,
                "❌ Не удалось распознать дату.\n"
                "Введи её в формате ДД.ММ.ГГГГ."
            )
            return

        name = state["name"]

        await db.execute("""
            INSERT INTO products (user_id, name, expiry_date)
            VALUES ($1, $2, $3)
        """, user_id, name, expiry)

        user_states.pop(user_id, None)

        remaining = (expiry - date.today()).days

        if remaining < 0:
            status = "Указанная дата уже прошла."
        elif remaining == 0:
            status = "Сегодня последний день по указанной дате."
        else:
            status = f"По указанной дате осталось {remaining} дн."

        await send(
            chat_id,
            f"✅ Продукт добавлен!\n\n"
            f"🥫 {name}\n"
            f"📅 Годен до: {expiry.strftime('%d.%m.%Y')}\n\n"
            f"{status}\n\n"
            f"Посмотреть корзину: /list"
        )


async def handle(request):
    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        return web.Response(status=403)

    try:
        update = await request.json()
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")

        if not chat_id or not user_id or not text:
            return web.json_response({"ok": True})

        if text.startswith("/start"):
            user_states.pop(user_id, None)
            await start(chat_id)

        elif text.startswith("/add"):
            await add_product(chat_id, user_id)

        elif text.startswith("/list"):
            await show_products(chat_id, user_id)

        elif text.startswith("/cancel"):
            user_states.pop(user_id, None)
            await send(chat_id, "Добавление отменено.")

        elif text.startswith("/help"):
            await send(
                chat_id,
                "📖 Помощь\n\n"
                "/add — добавить продукт\n"
                "/list — посмотреть продукты\n"
                "/cancel — отменить добавление\n\n"
                "Вводи дату в формате ДД.ММ.ГГГГ."
            )

        else:
            await handle_text(chat_id, user_id, text)

        return web.json_response({"ok": True})

    except Exception:
        logger.exception("Ошибка обработки сообщения")
        return web.json_response({"ok": True})


async def health(request):
    return web.json_response({"status": "ok"})


app = web.Application()
app.router.add_post("/webhook", handle)
app.router.add_get("/", health)
app.on_startup.append(setup)
app.on_cleanup.append(cleanup)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)
