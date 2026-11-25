
import os
import logging
from aiohttp import web, ClientSession

TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def telegram(method, data):
    async with ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/{method}", json=data
        ) as response:
            return await response.json()


async def setup_webhook(app):
    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if not render_url:
        logger.error("RENDER_EXTERNAL_URL не найден")
        return

    result = await telegram("setWebhook", {
        "url": f"{render_url}/webhook",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message"]
    })

    if result.get("ok"):
        logger.info("Webhook успешно подключён")
    else:
        logger.error("Не удалось подключить Webhook")


async def start(update):
    chat_id = update["message"]["chat"]["id"]

    await telegram("sendMessage", {
        "chat_id": chat_id,
        "text": (
            "🥫 Добро пожаловать в СрокКонтроль!\n\n"
            "Я помогу следить за сроками годности продуктов.\n\n"
            "Нажми /add, чтобы добавить первый продукт."
        )
    })


async def handle(request):
    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        return web.Response(status=403)

    try:
        update = await request.json()
        message = update.get("message", {})
        text = message.get("text", "")

        if text.startswith("/start"):
            await start(update)

        return web.json_response({"ok": True})

    except Exception:
        logger.exception("Ошибка обработки сообщения")
        return web.json_response({"ok": True})


async def health(request):
    return web.json_response({"status": "ok"})


app = web.Application()
app.router.add_post("/webhook", handle)
app.router.add_get("/", health)
app.on_startup.append(setup_webhook)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)
