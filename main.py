
import os
import json
import logging
from datetime import date, datetime
from aiohttp import web, ClientSession

TOKEN = os.environ["TELEGRAM_TOKEN"]
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO)

async def telegram(method, data):
    async with ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/{method}", json=data
        ) as response:
            return await response.json()

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
    try:
        update = await request.json()
        message = update.get("message", {})
        text = message.get("text", "")
        
        if text.startswith("/start"):
            await start(update)

        return web.json_response({"ok": True})

    except Exception:
        logging.exception("Ошибка обработки сообщения")
        return web.json_response({"ok": True})

async def health(request):
    return web.json_response({"status": "ok"})

app = web.Application()
app.router.add_post("/webhook", handle)
app.router.add_get("/", health)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)
