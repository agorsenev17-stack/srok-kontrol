
import os
import re
import json
import logging
from pathlib import Path
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
additives = {}


# Эмодзи для разных продуктов
def product_emoji(name):
    name = name.casefold()

    categories = [
        ("🥛", ["молоко", "кефир", "ряженка", "сливки"]),
        ("🥣", ["йогурт", "творожок"]),
        ("🧀", ["сыр", "моцарелла", "пармезан"]),
        ("🥩", ["говядина", "свинина", "мясо", "фарш"]),
        ("🐟", ["рыба", "лосось", "форель", "сельдь"]),
        ("🍗", ["курица", "индейка", "куриное"]),
        ("🥚", ["яйцо", "яйца"]),
        ("🍞", ["хлеб", "батон", "булка"]),
        ("🥦", ["брокколи", "капуста", "овощ"]),
        ("🍎", ["яблок", "груш", "фрукт"]),
        ("🧈", ["сливочное масло"]),
        ("🫒", ["растительное масло", "оливковое масло"]),
        ("🌭", ["колбаса", "сосиски", "сардельки"]),
        ("🍫", ["шоколад", "конфеты"]),
        ("🧃", ["сок", "нектар"]),
        ("🥫", ["консервы", "тушёнка"]),
        ("🍚", ["рис", "гречка", "крупа"]),
        ("🍝", ["макароны", "спагетти"]),
        ("🥔", ["картофель", "картошка"]),
        ("🍊", ["апельсин", "мандарин"]),
        ("🍌", ["банан"]),
        ("🍓", ["клубника", "малина"]),
        ("🍯", ["мёд", "мед"]),
        ("☕", ["кофе", "чай"]),
        ("🍪", ["печенье", "вафли"]),
        ("🍰", ["торт", "пирожное"])
    ]

    for emoji, words in categories:
        if any(word in name for word in words):
            return emoji

    return "🥫"


async def telegram(method, data):
    async with http.post(
        f"{BASE_URL}/{method}",
        json=data
    ) as response:
        return await response.json()


async def send(chat_id, text):
    await telegram("sendMessage", {
        "chat_id": chat_id,
        "text": text
    })


# Загрузка справочника добавок
def load_additives():
    global additives

    file_path = Path(__file__).with_name("additives.json")

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            additives = json.load(file)

        if not isinstance(additives, dict):
            raise ValueError("Неверный формат additives.json")

        logger.info("Загружено добавок: %s", len(additives))

    except Exception:
        additives = {}
        logger.exception("Не удалось загрузить additives.json")


# Поиск добавок по E-номерам и названиям
def find_additives(text):
    found = []

    # Заменяем похожие русские буквы латинскими
    text = text.translate(str.maketrans({
        "Е": "E",
        "е": "e",
        "А": "A",
        "а": "a",
        "В": "B",
        "в": "b",
        "С": "C",
        "с": "c",
        "К": "K",
        "к": "k",
        "М": "M",
        "м": "m",
        "Н": "H",
        "н": "h",
        "О": "O",
        "о": "o",
        "Р": "P",
        "р": "p",
        "Т": "T",
        "т": "t",
        "Х": "X",
        "х": "x"
    }))

    codes = re.findall(
        r"(?i)\bE\s*-?\s*\d{3,4}[a-z]?\b",
        text
    )

    for code in codes:
        normalized = re.sub(r"[\s-]", "", code).upper()

        for key, item in additives.items():
            if key.upper() == normalized:
                if item not in found:
                    found.append(item)

    lower_text = text.casefold()

    for item in additives.values():
        name = item.get("name", "").casefold()

        if len(name) >= 5 and name in lower_text:
            if item not in found:
                found.append(item)

    return found


async def check_additives(chat_id, text):
    found = find_additives(text)

    if not found:
        await send(
            chat_id,
            "🔎 В нашем справочнике пока не найдены "
            "известные добавки из этого текста.\n\n"
            "Попробуй отправить E325, E621 "
            "или название добавки.\n\n"
            "Отсутствие результата не означает, "
            "что продукт опасен или запрещён."
        )
        return

    lines = ["🔬 Результаты проверки:\n"]

    for item in found[:10]:
        status = item.get("status", "yellow")

        if status == "green":
            mark = "🟢"
            label = "Обычно не вызывает опасений при разрешённом применении"
        elif status == "red":
            mark = "🔴"
            label = "Требует особого внимания"
        else:
            mark = "🟡"
            label = "Есть особенности или ограничения"

        lines.append(
            f"{mark} {item.get('name', 'Неизвестная добавка')}\n"
            f"Категория: {item.get('category', 'Не указана')}\n"
            f"Оценка: {label}\n"
            f"Назначение: {item.get('purpose', 'Не указано')}\n"
            f"Описание: {item.get('explanation', 'Нет описания')}\n"
            f"Возможные риски: {item.get('risks', 'Нет данных')}\n"
        )

    lines.append(
        "ℹ️ Это справочная информация, а не заключение "
        "о безопасности продукта. Важны количество добавки, "
        "категория продукта и условия её применения."
    )

    await send(chat_id, "\n".join(lines))


# Подключение базы данных и Telegram
async def setup(app):
    global db, http

    load_additives()

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


# Стартовое сообщение
async def start(chat_id):
    await send(
        chat_id,
        "🥫 Добро пожаловать в СрокКонтроль!\n\n"
        "Я помогу следить за сроками годности "
        "и разбираться в составе продуктов.\n\n"
        "Команды:\n"
        "/add — добавить продукт\n"
        "/list — мои продукты\n"
        "/help — помощь\n\n"
        "🔬 Для проверки добавки отправь её номер, "
        "например E325 или E621."
    )


# Добавление продукта
async def add_product(chat_id, user_id):
    user_states[user_id] = {"step": "name"}

    await send(
        chat_id,
        "🥫 Напиши название продукта:"
    )


# Просмотр корзины
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
        emoji = product_emoji(row["name"])

        if remaining < 0:
            status = (
                "⚠️ Срок годности истёк. "
                "Не рекомендуется употреблять."
            )
        elif remaining == 0:
            status = "⚠️ Сегодня последний день по указанной дате."
        else:
            status = f"Осталось дней: {remaining}"

        lines.append(
            f"{emoji} {row['name']}\n"
            f"📅 Дата: {row['expiry_date'].strftime('%d.%m.%Y')}\n"
            f"{status}\n"
        )

    await send(chat_id, "\n".join(lines))


# Обработка добавления продукта
async def handle_text(chat_id, user_id, text):
    state = user_states.get(user_id)

    if not state:
        await check_additives(chat_id, text)
        return

    if state["step"] == "name":
        name = text.strip()

        if not name or len(name) > 100:
            await send(
                chat_id,
                "❌ Название должно содержать от 1 до 100 символов."
            )
            return

        state["name"] = name
        state["step"] = "date"

        emoji = product_emoji(name)

        await send(
            chat_id,
            f"{emoji} Продукт: {name}\n\n"
            "📅 Теперь введи дату окончания срока годности.\n\n"
            "Например: 25.12.2026"
        )
        return

    if state["step"] == "date":
        try:
            expiry = datetime.strptime(
                text.strip(),
                "%d.%m.%Y"
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
        emoji = product_emoji(name)

        if remaining < 0:
            status = (
                "⚠️ Срок годности истёк.\n"
                "Не рекомендуется употреблять продукт. "
                "Не пробуй его, даже если запах и внешний вид нормальные."
            )
        elif remaining == 0:
            status = (
                "⚠️ Сегодня последний день по указанной дате."
            )
        else:
            status = f"По указанной дате осталось {remaining} дн."

        await send(
            chat_id,
            f"✅ Продукт добавлен!\n\n"
            f"{emoji} {name}\n"
            f"📅 Годен до: {expiry.strftime('%d.%m.%Y')}\n\n"
            f"{status}\n\n"
            f"Посмотреть корзину: /list"
        )


# Получение сообщений Telegram
async def handle(request):
    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        return web.Response(status=403)

    try:
        update = await request.json()
        message = update.get("message", {})

        text = message.get("text", "").strip()
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
                "🔬 Отправь E325, E621 или название добавки, "
                "чтобы проверить её."
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
