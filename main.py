
import os
import re
import json
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiohttp import web, ClientSession


# ==================================================
# НАСТРОЙКИ
# ==================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "")
TIMEZONE_OPTIONS = {
    "1": ("Калининград", "Europe/Kaliningrad"),

    "2": (
        "Москва, Санкт-Петербург, Воронеж, Краснодар, Ростов-на-Дону",
        "Europe/Moscow"
    ),

    "3": (
        "Самара, Ижевск, Ульяновск, Саратов",
        "Europe/Samara"
    ),

    "4": (
        "Екатеринбург, Уфа, Пермь, Челябинск, Тюмень",
        "Asia/Yekaterinburg"
    ),

    "5": ("Омск", "Asia/Omsk"),

    "6": (
        "Новосибирск, Барнаул, Томск, Кемерово",
        "Asia/Novosibirsk"
    ),

    "7": (
        "Красноярск, Абакан, Норильск, Кызыл",
        "Asia/Krasnoyarsk"
    ),

    "8": (
        "Иркутск, Улан-Удэ",
        "Asia/Irkutsk"
    ),

    "9": (
        "Якутск, Чита",
        "Asia/Yakutsk"
    ),

    "10": (
        "Владивосток, Хабаровск, Биробиджан",
        "Asia/Vladivostok"
    ),

    "11": (
        "Магадан, Южно-Сахалинск",
        "Asia/Magadan"
    ),

    "12": (
        "Петропавловск-Камчатский, Анадырь",
        "Asia/Kamchatka"
    )
}

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db = None
http = None

user_states = {}
additives = {}


# ==================================================
# ЭМОДЗИ ДЛЯ ПРОДУКТОВ
# ==================================================

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


# ==================================================
# TELEGRAM
# ==================================================

async def telegram(method, data):
    async with http.post(
        f"{BASE_URL}/{method}",
        json=data
    ) as response:
        result = await response.json()

        if not result.get("ok"):
            logger.error(
                "Ошибка Telegram API (%s): %s",
                method,
                result
            )

        return result


async def send(chat_id, text):
    max_length = 3800

    if len(text) <= max_length:
        await telegram("sendMessage", {
            "chat_id": chat_id,
            "text": text
        })
        return

    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_length:
            if current:
                chunks.append(current)
                current = ""

        while len(line) > max_length:
            if current:
                chunks.append(current)
                current = ""

            chunks.append(line[:max_length])
            line = line[max_length:]

        if current:
            current += "\n" + line
        else:
            current = line

    if current:
        chunks.append(current)

    for chunk in chunks:
        await telegram("sendMessage", {
            "chat_id": chat_id,
            "text": chunk
        })


# ==================================================
# ЗАГРУЗКА ТРЁХ СПРАВОЧНИКОВ
# ==================================================

def load_additives():
    global additives

    base_path = Path(__file__).parent

    files = [
        "additives.json",
        "additives_extra.json",
        "flavourings.json"
    ]

    combined = {}

    for filename in files:
        file_path = base_path / filename

        try:
            with open(file_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict):
                raise ValueError(
                    f"Неверный формат файла {filename}"
                )

            loaded_count = 0

            for code, information in data.items():

                if not isinstance(information, dict):
                    logger.warning(
                        "Пропущена некорректная запись %s в %s",
                        code,
                        filename
                    )
                    continue

                normalized_code = str(code).strip().upper()

                if normalized_code in combined:
                    logger.warning(
                        "Повторяющийся код %s из файла %s",
                        normalized_code,
                        filename
                    )

                combined[normalized_code] = information
                loaded_count += 1

            logger.info(
                "Загружен файл %s: %s записей",
                filename,
                loaded_count
            )

        except FileNotFoundError:
            logger.warning("Файл не найден: %s", filename)

        except json.JSONDecodeError:
            logger.exception(
                "Ошибка JSON в файле %s",
                filename
            )

        except Exception:
            logger.exception(
                "Ошибка загрузки файла %s",
                filename
            )

    additives = combined

    logger.info(
        "Всего загружено записей справочника: %s",
        len(additives)
    )


# ==================================================
# НОРМАЛИЗАЦИЯ ТЕКСТА
# ==================================================

def normalize_search_text(text):
    text = str(text).casefold()
    text = text.replace("ё", "е")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_e_codes(text):
    return text.translate(str.maketrans({
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


# ==================================================
# ПОИСК ДОБАВОК И АРОМАТИЗАТОРОВ
# ==================================================

def find_additives(text):
    found = []

    original_text = normalize_search_text(text)
    code_text = normalize_e_codes(text)

    codes = re.findall(
        r"(?i)\bE\s*-?\s*\d{3,4}[a-z]?\b",
        code_text
    )

    for code in codes:
        normalized = re.sub(
            r"[\s-]",
            "",
            code
        ).upper()

        item = additives.get(normalized)

        if item and item not in found:
            found.append(item)

    for item in additives.values():

        if not isinstance(item, dict):
            continue

        search_names = []

        name = item.get("name", "")

        if isinstance(name, str) and name:
            search_names.append(name)

        aliases = item.get("aliases", [])

        if isinstance(aliases, list):
            search_names.extend(
                alias
                for alias in aliases
                if isinstance(alias, str)
            )

        elif isinstance(aliases, str):
            search_names.append(aliases)

        for search_name in search_names:

            search_name = normalize_search_text(search_name)

            if len(search_name) < 4:
                continue

            if search_name in original_text:

                if item not in found:
                    found.append(item)

                break

    return found


# ==================================================
# КАРТОЧКИ ДОБАВОК И АРОМАТИЗАТОРОВ
# ==================================================

async def check_additives(chat_id, text):
    found = find_additives(text)

    if not found:
        await send(
            chat_id,
            "🔎 В нашем справочнике пока не найдены "
            "известные добавки из этого текста.\n\n"
            "Попробуй отправить:\n"
            "• E325\n"
            "• E621\n"
            "• Ванилин\n"
            "• Ментол\n"
            "• Лимонен\n\n"
            "Также можно отправить английское название.\n\n"
            "ℹ️ Отсутствие результата не означает, "
            "что продукт опасен или запрещён."
        )
        return

    lines = [
        f"🔬 Найдено записей: {len(found)}",
        ""
    ]

    for item in found[:10]:

        status = str(
            item.get("status", "yellow")
        ).lower()

        if status == "green":
            mark = "🟢"
            label = (
                "Нет оснований для опасений "
                "при оценённых условиях применения"
            )

        elif status == "red":
            mark = "🔴"
            label = "Требует особого внимания"

        else:
            mark = "🟡"
            label = "Есть особенности или ограничения"

        name = item.get("name", "Неизвестное вещество")
        category = item.get("category", "Не указана")
        purpose = item.get("purpose", "Информация отсутствует")
        explanation = item.get("explanation", "Информация отсутствует")
        risks = item.get("risks", "Достоверные сведения не указаны")
        effects = item.get("effects", "Не указаны")
        who = item.get(
            "who_should_be_careful",
            "Специальные рекомендации не указаны"
        )

        assessment = item.get("assessment", "")
        regulatory = item.get("regulatory_note", "")
        source = item.get("source", "Источник не указан")

        lines.extend([
            f"{mark} {name}",
            f"Категория: {category}",
            f"Оценка: {label}",
            "",
            f"🎯 Назначение:\n{purpose}",
            "",
            f"📖 Что это такое:\n{explanation}",
            "",
            f"⚠️ Возможные риски:\n{risks}",
            "",
            f"🧬 Воздействие:\n{effects}",
            "",
            f"👤 Кому стоит обратить внимание:\n{who}",
        ])

        if assessment:
            lines.extend([
                "",
                f"📋 Научная оценка:\n{assessment}"
            ])

        if regulatory:
            lines.extend([
                "",
                f"⚖️ Регуляторная информация:\n{regulatory}"
            ])

        lines.extend([
            "",
            f"📚 Источник: {source}",
            "",
            "━━━━━━━━━━━━━━",
            ""
        ])

    if len(found) > 10:
        lines.append(
            f"ℹ️ Показаны первые 10 записей из {len(found)}."
        )

    lines.extend([
        "",
        "❗ ВАЖНО:",
        "Эта проверка носит справочный характер.",
        "Наличие вещества само по себе не доказывает, "
        "что продукт опасен.",
        "Важны количество вещества, категория продукта, "
        "условия применения и индивидуальная чувствительность.",
        "",
        "🛒 Для проверки срока годности используй /add."
    ])

    await send(chat_id, "\n".join(lines))


# ==================================================
# ПОДКЛЮЧЕНИЕ NEON И TELEGRAM WEBHOOK
# ==================================================

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

    await db.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id BIGINT PRIMARY KEY,
        timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
        reminder_hour INTEGER NOT NULL DEFAULT 9
    )
""")
    
    await db.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id BIGINT PRIMARY KEY,
        timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
        reminder_hour INTEGER NOT NULL DEFAULT 9
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
            logger.error(
                "Ошибка подключения Webhook: %s",
                result
            )

    logger.info("СрокКонтроль успешно запущен")


async def cleanup(app):
    global db, http

    if db:
        await db.close()

    if http:
        await http.close()


# ==================================================
# СТАРТ
# ==================================================

async def start(chat_id):
    await send(
        chat_id,
        "🥫 Добро пожаловать в СрокКонтроль!\n\n"
        "Я помогу следить за сроками годности "
        "и разбираться в составе продуктов.\n\n"
        "📦 Команды:\n"
        "/add — добавить продукт\n"
        "/list — мои продукты\n"
        "/edit — изменить продукт\n"
        "/delete — удалить продукт\n"
        "/timezone — выбрать свой город и время напоминаний\n"
        "/cancel — отменить действие\n"
        "/help — помощь\n\n"
        "🔬 Проверка состава:\n"
        "Отправь E325, E621, ванилин, ментол "
        "или название другого вещества.\n\n"
        "🧪 Также можно искать ароматизаторы "
        "по русским и английским названиям."
    )


# ==================================================
# ОБЩЕЕ ПОЛУЧЕНИЕ ПРОДУКТОВ ПОЛЬЗОВАТЕЛЯ
# ==================================================

async def get_user_products(user_id):
    return await db.fetch("""
        SELECT id, name, expiry_date
        FROM products
        WHERE user_id = $1
        ORDER BY expiry_date, id
    """, user_id)


def parse_product_number(text, total):
    try:
        number = int(text.strip())
    except ValueError:
        return None

    if number < 1 or number > total:
        return None

    return number - 1
    async def start_timezone(chat_id, user_id):

    user_states[user_id] = {
        "step": "timezone_choice"
    }

    lines = [
        "🌍 Выбери свой город или часовой пояс:",
        "",
    ]

    for number, (cities, timezone) in TIMEZONE_OPTIONS.items():
        lines.append(f"{number} — {cities}")

    lines.extend([
        "",
        "Отправь номер своего варианта.",
        "",
        "⏰ Напоминания будут приходить в 09:00 по выбранному времени.",
        "Для отмены: /cancel"
    ])

    await send(chat_id, "\n".join(lines))


# ==================================================
# ДОБАВЛЕНИЕ ПРОДУКТА
# ==================================================

async def add_product(chat_id, user_id):
    user_states[user_id] = {
        "step": "name"
    }

    await send(
        chat_id,
        "🥫 Напиши название продукта:"
    )


# ==================================================
# ПРОСМОТР КОРЗИНЫ
# ==================================================

async def show_products(chat_id, user_id):

    rows = await get_user_products(user_id)

    if not rows:
        await send(
            chat_id,
            "📦 Твоя корзина пока пуста.\n\n"
            "Добавить продукт: /add"
        )
        return

    today = date.today()

    lines = [
        "📦 Твои продукты:",
        ""
    ]

    for index, row in enumerate(rows, 1):

        remaining = (
            row["expiry_date"] - today
        ).days

        emoji = product_emoji(row["name"])

        if remaining < 0:
            status = (
                "⚠️ Срок годности истёк. "
                "Не рекомендуется употреблять."
            )

        elif remaining == 0:
            status = (
                "⚠️ Сегодня последний день "
                "по указанной дате."
            )

        else:
            status = (
                f"⏳ Осталось дней: {remaining}"
            )

        lines.append(
            f"**{index}.** {emoji} {row['name']}\n"
            f"📅 Дата: "
            f"{row['expiry_date'].strftime('%d.%m.%Y')}\n"
            f"{status}\n"
        )

    lines.extend([
        "ℹ️ Расчёт основан на введённой дате. "
        "Учитывай условия хранения и маркировку.",
        "",
        "✏️ Изменить продукт: /edit",
        "🗑 Удалить продукт: /delete"
    ])

    await send(
        chat_id,
        "\n".join(lines)
    )


# ==================================================
# УДАЛЕНИЕ ПРОДУКТА
# ==================================================

async def start_delete(chat_id, user_id):

    rows = await get_user_products(user_id)

    if not rows:
        await send(
            chat_id,
            "📦 Корзина пуста. Удалять пока нечего."
        )
        return

    user_states[user_id] = {
        "step": "delete_number"
    }

    lines = [
        "🗑 **Удаление продукта**",
        "",
        "Выбери номер продукта:",
        ""
    ]

    for index, row in enumerate(rows, 1):
        emoji = product_emoji(row["name"])

        lines.append(
            f"{index}. {emoji} {row['name']} — "
            f"{row['expiry_date'].strftime('%d.%m.%Y')}"
        )

    lines.extend([
        "",
        "Отправь номер продукта.",
        "Для отмены: /cancel"
    ])

    await send(chat_id, "\n".join(lines))


# ==================================================
# РЕДАКТИРОВАНИЕ ПРОДУКТА
# ==================================================

async def start_edit(chat_id, user_id):

    rows = await get_user_products(user_id)

    if not rows:
        await send(
            chat_id,
            "📦 Корзина пуста. Изменять пока нечего."
        )
        return

    user_states[user_id] = {
        "step": "edit_number"
    }

    lines = [
        "✏️ **Редактирование продукта**",
        "",
        "Выбери номер продукта:",
        ""
    ]

    for index, row in enumerate(rows, 1):
        emoji = product_emoji(row["name"])

        lines.append(
            f"{index}. {emoji} {row['name']} — "
            f"{row['expiry_date'].strftime('%d.%m.%Y')}"
        )

    lines.extend([
        "",
        "Отправь номер продукта.",
        "Для отмены: /cancel"
    ])

    await send(chat_id, "\n".join(lines))


# ==================================================
# ОБРАБОТКА ТЕКСТОВЫХ ДЕЙСТВИЙ
# ==================================================

async def handle_text(chat_id, user_id, text):

    state = user_states.get(user_id)

    if not state:
        await check_additives(chat_id, text)
        return

    step = state.get("step")
        # ----------------------------------------------
    # ВЫБОР ЧАСОВОГО ПОЯСА
    # ----------------------------------------------

    if step == "timezone_choice":

        choice = text.strip()

        if choice not in TIMEZONE_OPTIONS:
            await send(
                chat_id,
                "❌ Такого номера нет. "
                "Выбери номер из списка или отправь /cancel."
            )
            return

        cities, timezone = TIMEZONE_OPTIONS[choice]

        try:
            ZoneInfo(timezone)
        except Exception:
            logger.exception("Некорректный часовой пояс: %s", timezone)
            await send(chat_id, "⚠️ Не удалось установить часовой пояс. Попробуй позже.")
            return

        await db.execute("""
            INSERT INTO user_settings (
                user_id,
                timezone,
                reminder_hour
            )
            VALUES ($1, $2, 9)
            ON CONFLICT (user_id)
            DO UPDATE SET timezone = EXCLUDED.timezone
        """, user_id, timezone)

        user_states.pop(user_id, None)

        await send(
            chat_id,
            f"✅ Часовой пояс сохранён!\n\n"
            f"📍 Регион: {cities}\n"
            f"🌍 Часовой пояс: {timezone}\n"
            f"⏰ Время напоминаний: 09:00\n\n"
            "Теперь можно пользоваться ботом."
        )
        return

    # ----------------------------------------------
    # ДОБАВЛЕНИЕ: НАЗВАНИЕ
    # ----------------------------------------------

    if step == "name":

        name = text.strip()

        if not name or len(name) > 100:
            await send(
                chat_id,
                "❌ Название должно содержать "
                "от 1 до 100 символов."
            )
            return

        state["name"] = name
        state["step"] = "date"

        emoji = product_emoji(name)

        await send(
            chat_id,
            f"{emoji} Продукт: {name}\n\n"
            "📅 Теперь введи дату окончания "
            "срока годности.\n\n"
            "Например: 25.12.2026"
        )

        return

    # ----------------------------------------------
    # ДОБАВЛЕНИЕ: ДАТА
    # ----------------------------------------------

    if step == "date":

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
            INSERT INTO products (
                user_id,
                name,
                expiry_date
            )
            VALUES ($1, $2, $3)
        """, user_id, name, expiry)

        user_states.pop(user_id, None)

        remaining = (
            expiry - date.today()
        ).days

        emoji = product_emoji(name)

        if remaining < 0:
            status = (
                "⚠️ Указанная дата уже прошла.\n"
                "Не рекомендуется употреблять продукт."
            )

        elif remaining == 0:
            status = (
                "⚠️ Сегодня последний день "
                "по указанной дате."
            )

        else:
            status = (
                f"⏳ По указанной дате осталось "
                f"{remaining} дн."
            )

        await send(
            chat_id,
            f"✅ Продукт добавлен!\n\n"
            f"{emoji} {name}\n"
            f"📅 Годен до: "
            f"{expiry.strftime('%d.%m.%Y')}\n\n"
            f"{status}\n\n"
            "Посмотреть корзину: /list"
        )

        return

    # ----------------------------------------------
    # УДАЛЕНИЕ: ВЫБОР ПРОДУКТА
    # ----------------------------------------------

    if step == "delete_number":

        rows = await get_user_products(user_id)

        if not rows:
            user_states.pop(user_id, None)
            await send(chat_id, "📦 Корзина уже пуста.")
            return

        index = parse_product_number(text, len(rows))

        if index is None:
            await send(
                chat_id,
                f"❌ Введи номер от 1 до {len(rows)}."
            )
            return

        selected = rows[index]

        state["product_id"] = selected["id"]
        state["product_name"] = selected["name"]
        state["step"] = "delete_confirm"

        await send(
            chat_id,
            f"🗑 Ты выбрал:\n\n"
            f"{product_emoji(selected['name'])} "
            f"{selected['name']}\n"
            f"📅 Дата: "
            f"{selected['expiry_date'].strftime('%d.%m.%Y')}\n\n"
            "Точно удалить этот продукт?\n\n"
            "1 — Да, удалить\n"
            "2 — Нет, отменить"
        )

        return

    # ----------------------------------------------
    # УДАЛЕНИЕ: ПОДТВЕРЖДЕНИЕ
    # ----------------------------------------------

    if step == "delete_confirm":

        answer = text.strip().casefold()

        if answer in ("1", "да", "удалить"):

            result = await db.execute("""
                DELETE FROM products
                WHERE id = $1 AND user_id = $2
            """, state["product_id"], user_id)

            user_states.pop(user_id, None)

            if result == "DELETE 1":
                await send(
                    chat_id,
                    f"✅ Продукт «{state['product_name']}» "
                    "успешно удалён.\n\n"
                    "Посмотреть корзину: /list"
                )
            else:
                await send(
                    chat_id,
                    "⚠️ Не удалось найти этот продукт. "
                    "Обнови список командой /list."
                )

        elif answer in ("2", "нет", "отмена"):
            user_states.pop(user_id, None)

            await send(
                chat_id,
                "↩️ Удаление отменено. "
                "Продукт остался в корзине."
            )

        else:
            await send(
                chat_id,
                "Выбери один вариант:\n"
                "1 — Да, удалить\n"
                "2 — Нет, отменить"
            )

        return

    # ----------------------------------------------
    # РЕДАКТИРОВАНИЕ: ВЫБОР ПРОДУКТА
    # ----------------------------------------------

    if step == "edit_number":

        rows = await get_user_products(user_id)

        if not rows:
            user_states.pop(user_id, None)
            await send(chat_id, "📦 Корзина уже пуста.")
            return

        index = parse_product_number(text, len(rows))

        if index is None:
            await send(
                chat_id,
                f"❌ Введи номер от 1 до {len(rows)}."
            )
            return

        selected = rows[index]

        state["product_id"] = selected["id"]
        state["product_name"] = selected["name"]
        state["step"] = "edit_choice"

        await send(
            chat_id,
            f"✏️ Выбран продукт:\n\n"
            f"{product_emoji(selected['name'])} "
            f"{selected['name']}\n"
            f"📅 Дата: "
            f"{selected['expiry_date'].strftime('%d.%m.%Y')}\n\n"
            "Что хочешь изменить?\n\n"
            "1 — Название продукта\n"
            "2 — Срок годности"
        )

        return

    # ----------------------------------------------
    # РЕДАКТИРОВАНИЕ: ВЫБОР ПОЛЯ
    # ----------------------------------------------

    if step == "edit_choice":

        answer = text.strip()

        if answer == "1":
            state["step"] = "edit_name"

            await send(
                chat_id,
                "✏️ Напиши новое название продукта:"
            )

        elif answer == "2":
            state["step"] = "edit_date"

            await send(
                chat_id,
                "📅 Напиши новую дату окончания "
                "срока годности.\n\n"
                "Формат: ДД.ММ.ГГГГ\n"
                "Например: 25.12.2026"
            )

        else:
            await send(
                chat_id,
                "❌ Выбери:\n"
                "1 — Название\n"
                "2 — Срок годности"
            )

        return

    # ----------------------------------------------
    # РЕДАКТИРОВАНИЕ: НОВОЕ НАЗВАНИЕ
    # ----------------------------------------------

    if step == "edit_name":

        new_name = text.strip()

        if not new_name or len(new_name) > 100:
            await send(
                chat_id,
                "❌ Название должно содержать "
                "от 1 до 100 символов."
            )
            return

        result = await db.execute("""
            UPDATE products
            SET name = $1
            WHERE id = $2 AND user_id = $3
        """, new_name, state["product_id"], user_id)

        user_states.pop(user_id, None)

        if result == "UPDATE 1":
            await send(
                chat_id,
                f"✅ Название изменено!\n\n"
                f"{product_emoji(new_name)} {new_name}\n\n"
                "Посмотреть корзину: /list"
            )
        else:
            await send(
                chat_id,
                "⚠️ Не удалось изменить продукт. "
                "Попробуй снова."
            )

        return

    # ----------------------------------------------
    # РЕДАКТИРОВАНИЕ: НОВАЯ ДАТА
    # ----------------------------------------------

    if step == "edit_date":

        try:
            new_date = datetime.strptime(
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

        result = await db.execute("""
            UPDATE products
            SET expiry_date = $1
            WHERE id = $2 AND user_id = $3
        """, new_date, state["product_id"], user_id)

        user_states.pop(user_id, None)

        if result == "UPDATE 1":
            await send(
                chat_id,
                "✅ Дата срока годности изменена!\n\n"
                f"📅 Новая дата: "
                f"{new_date.strftime('%d.%m.%Y')}\n\n"
                "Посмотреть корзину: /list"
            )
        else:
            await send(
                chat_id,
                "⚠️ Не удалось изменить продукт. "
                "Попробуй снова."
            )

        return

    user_states.pop(user_id, None)

    await send(
        chat_id,
        "⚠️ Действие сброшено. "
        "Попробуй начать заново."
    )


# ==================================================
# ПОЛУЧЕНИЕ СООБЩЕНИЙ TELEGRAM
# ==================================================

async def handle(request):

    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:

        return web.Response(status=403)

    try:
        update = await request.json()

        message = update.get("message", {})

        text = message.get("text", "").strip()

        chat_id = message.get(
            "chat",
            {}
        ).get("id")

        user_id = message.get(
            "from",
            {}
        ).get("id")

        if not chat_id or not user_id or not text:
            return web.json_response({"ok": True})

        command = text.split()[0].split("@")[0]

        if command == "/start":

            user_states.pop(user_id, None)
            await start(chat_id)

        elif command == "/add":

            await add_product(chat_id, user_id)

        elif command == "/list":

            await show_products(chat_id, user_id)

        elif command == "/delete":

            await start_delete(chat_id, user_id)

        elif command == "/edit":

    await start_edit(chat_id, user_id)

        elif command == "/delete":

            await start_delete(chat_id, user_id)

        elif command == "/edit":

            await start_edit(chat_id, user_id)

        elif command == "/timezone":

            await start_timezone(chat_id, user_id)

        elif command == "/cancel":

            user_states.pop(user_id, None)

            await send(
                chat_id,
                "❌ Текущее действие отменено."
            )

        elif command == "/help":

            await send(
        elif command == "/help":

            await send(
                chat_id,
                "📖 Помощь\n\n"
                "/start — запуск бота\n"
                "/add — добавить продукт\n"
                "/list — посмотреть продукты\n"
                "/edit — изменить продукт\n"
                "/delete — удалить продукт\n"
                "/timezone — выбрать город и часовой пояс\n"
                "/cancel — отменить действие\n"
                "/help — помощь\n\n"
                "🔬 Для проверки состава отправь "
                "E325, E621, ванилин, ментол "
                "или английское название вещества."
            )

        else:

            await handle_text(
                chat_id,
                user_id,
                text
            )

        return web.json_response({"ok": True})

    except Exception:

        logger.exception("Ошибка обработки сообщения")

        return web.json_response({"ok": True})


# ==================================================
# ПРОВЕРКА РАБОТОСПОСОБНОСТИ RENDER
# ==================================================

async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "СрокКонтроль",
        "additives_loaded": len(additives)
    })


# ==================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ==================================================

app = web.Application()

app.router.add_post("/webhook", handle)
app.router.add_get("/", health)

app.on_startup.append(setup)
app.on_cleanup.append(cleanup)


if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 10000)
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=port
    )
