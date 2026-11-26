
import os
import re
import json
import logging
import io
import asyncio
import threading
import pytesseract

from PIL import (
    Image,
    ImageOps,
    ImageEnhance
)
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiohttp import web, ClientSession, ClientTimeout


# ==================================================
# НАСТРОЙКИ
# ==================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

MAX_PHOTO_SIZE = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000

OCR_TIMEOUT = 35
OCR_LOCK = threading.Lock()

TIMEZONE_OPTIONS = {
    "1": ("Калининград", "Europe/Kaliningrad"),
    "2": (
        "Москва, Санкт-Петербург, Воронеж, Краснодар, Ростов-на-Дону",
        "Europe/Moscow"
    ),
    "3": ("Самара, Ижевск, Ульяновск, Саратов", "Europe/Samara"),
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
    "8": ("Иркутск, Улан-Удэ", "Asia/Irkutsk"),
    "9": ("Якутск, Чита", "Asia/Yakutsk"),
    "10": ("Владивосток, Хабаровск, Биробиджан", "Asia/Vladivostok"),
    "11": ("Магадан, Южно-Сахалинск", "Asia/Magadan"),
    "12": (
        "Петропавловск-Камчатский, Анадырь",
        "Asia/Kamchatka"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

db = None
http = None

user_states = {}
additives = {}

background_tasks = set()

ocr_semaphore = asyncio.Semaphore(1)


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
    if http is None:
        raise RuntimeError("HTTP-клиент Telegram ещё не запущен")

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



async def send(chat_id, text, reply_markup=None):
    max_length = 3800

    if len(text) <= max_length:
        payload = {
            "chat_id": chat_id,
            "text": text
        }

        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        await telegram("sendMessage", payload)
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

    for index, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk
        }

        if index == len(chunks) - 1 and reply_markup is not None:
            payload["reply_markup"] = reply_markup

        await telegram("sendMessage", payload)


# ==================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
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

    original_words = set(
        re.findall(r"[a-zа-яё0-9]+", original_text)
    )

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
            normalized_name = normalize_search_text(
                search_name
            )

            if len(normalized_name) < 4:
                continue

            name_words = re.findall(
                r"[a-zа-яё0-9]+",
                normalized_name
            )

            if not name_words:
                continue

            if all(word in original_words for word in name_words):
                if item not in found:
                    found.append(item)

                break

    return found


# ==================================================
# ПОДГОТОВКА ИЗОБРАЖЕНИЯ ДЛЯ OCR
# ==================================================

def prepare_ocr_images(image_data):
    """
    Готовит три варианта фотографии:
    контрастный, чёрно-белый и инвертированный.
    """

    with Image.open(io.BytesIO(image_data)) as original:
        image = ImageOps.exif_transpose(original)

        if image.width * image.height > MAX_IMAGE_PIXELS:
            image.thumbnail(
                (4000, 4000),
                Image.Resampling.LANCZOS
            )

        image = image.convert("RGB")

    image.thumbnail(
        (1800, 1800),
        Image.Resampling.LANCZOS
    )

    new_size = (
        min(image.width * 2, 3000),
        min(image.height * 2, 3000)
    )

    image = image.resize(
        new_size,
        Image.Resampling.LANCZOS
    )

    gray = ImageOps.grayscale(image)

    enhanced = ImageOps.autocontrast(
        gray,
        cutoff=1
    )

    enhanced = ImageEnhance.Contrast(
        enhanced
    ).enhance(1.7)

    enhanced = ImageEnhance.Sharpness(
        enhanced
    ).enhance(1.8)

    binary = enhanced.point(
        lambda pixel: 255 if pixel >= 175 else 0
    )

    inverted = ImageOps.invert(binary)

    return [
        ("контраст", enhanced),
        ("чёрно-белый", binary),
        ("инверсия", inverted)
    ]


# ==================================================
# РАСПОЗНАВАНИЕ ТЕКСТА
# ==================================================

def recognize_label(image_data):
    candidates = []
    errors = []

    with OCR_LOCK:
        images = prepare_ocr_images(image_data)

        modes = [
            ("обычный", "--oem 1 --psm 6"),
            ("блоки текста", "--oem 1 --psm 11")
        ]

        for image_name, image in images:
            for mode_name, config in modes:
                try:
                    result = pytesseract.image_to_string(
                        image,
                        lang="rus+eng",
                        config=config,
                        timeout=OCR_TIMEOUT
                    ).strip()

                    logger.info(
                        "OCR: изображение=%s, режим=%s, символов=%s",
                        image_name,
                        mode_name,
                        len(result)
                    )

                    if result and result not in candidates:
                        candidates.append(result)

                except Exception as error:
                    errors.append(
                        f"{image_name}/{mode_name}: "
                        f"{type(error).__name__}: {error}"
                    )

                    logger.warning(
                        "Ошибка OCR: изображение=%s, режим=%s, ошибка=%s",
                        image_name,
                        mode_name,
                        error
                    )

    if not candidates:
        raise RuntimeError(
            "Все режимы OCR завершились ошибкой: "
            + " | ".join(errors)
        )

    return candidates


def choose_best_ocr(candidates):
    if not candidates:
        return ""

    def score(candidate):
        matches = len(find_additives(candidate))

        letters = sum(
            character.isalpha()
            for character in candidate
        )

        digits = sum(
            character.isdigit()
            for character in candidate
        )

        words = re.findall(
            r"[A-Za-zА-Яа-яЁё]{2,}",
            candidate
        )

        useful_words = sum(
            1 for word in words if len(word) >= 3
        )

        return (
            matches * 100,
            useful_words * 4,
            letters * 2 + digits,
            len(candidate)
        )

    return max(candidates, key=score)


# ==================================================
# ПРОВЕРКА ДОСТУПНОСТИ TESSERACT
# ==================================================

def check_ocr_environment():
    try:
        version = pytesseract.get_tesseract_version()

        languages = pytesseract.get_languages(config="")

        logger.info(
            "Tesseract version: %s",
            version
        )

        logger.info(
            "Доступные языки OCR: %s",
            ", ".join(languages)
        )

        missing = [
            language
            for language in ("rus", "eng")
            if language not in languages
        ]

        if missing:
            logger.error(
                "Отсутствуют языки Tesseract: %s",
                ", ".join(missing)
            )
        else:
            logger.info(
                "Русский и английский OCR доступны"
            )

    except Exception:
        logger.exception(
            "Не удалось проверить окружение Tesseract"
        )


# ==================================================
# АНАЛИЗ ДОБАВОК
# ==================================================

async def check_additives(chat_id, text, photo_text=None):
    found = find_additives(text)

    if not found:
        lines = []

        if photo_text is not None:
            lines.extend([
                "📸 Фотография обработана!",
                "",
                "📝 Распознанный текст:",
                photo_text[:1200] if photo_text else
                "Не удалось уверенно распознать текст.",
                "",
                "🔬 Результат проверки:",
                ""
            ])

        lines.extend([
            "🔎 В нашем справочнике пока не найдены "
            "известные добавки из этого текста.",
            "",
            "Попробуй отправить:",
            "• E325",
            "• E621",
            "• Ванилин",
            "• Ментол",
            "• Лимонен",
            "",
            "Также можно отправить английское название.",
            "",
            "ℹ️ Отсутствие результата не означает, "
            "что продукт опасен или запрещён.",
            "",
            "⚠️ OCR может ошибаться: проверь распознанный "
            "текст по оригинальной упаковке."
        ])

        await send(chat_id, "\n".join(lines))
        return

    lines = []

    if photo_text is not None:
        lines.extend([
            "📸 Фотография обработана!",
            "",
            "📝 Распознанный текст:",
            photo_text[:1200] if photo_text else
            "Не удалось уверенно распознать текст.",
            "",
            "🔬 Результат проверки:",
            ""
        ])

    lines.extend([
        f"🔬 Найдено записей: {len(found)}",
        ""
    ])

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
        "⚠️ Распознавание фотографии может ошибаться. "
        "Сверяй состав с оригинальной упаковкой.",
        "",
        "🛒 Для проверки срока годности используй /add."
    ])

    await send(chat_id, "\n".join(lines))


# ==================================================
# ОБРАБОТКА ФОТОГРАФИИ
# ==================================================

async def handle_photo(chat_id, user_id, message):
    message_id = message.get("message_id")
    photos = message.get("photo", [])

    file_unique_id = (
        photos[-1].get("file_unique_id")
        if photos else None
    )

    logger.info(
        "Начата обработка фото: chat_id=%s message_id=%s file_unique_id=%s",
        chat_id,
        message_id,
        file_unique_id
    )

    try:
        await send(
            chat_id,
            "⏳ Пожалуйста, подождите, ваше фото обрабатывается.\n\n"
            "Я загружаю изображение и распознаю состав. "
            "Это может занять некоторое время."
        )

        if not photos:
            await send(
                chat_id,
                "❌ Не удалось получить фотографию. "
                "Попробуй отправить её ещё раз."
            )
            return

        photo = photos[-1]
        file_id = photo.get("file_id")

        if not file_id:
            await send(
                chat_id,
                "❌ Не удалось определить файл фотографии."
            )
            return

        file_info = await telegram(
            "getFile",
            {"file_id": file_id}
        )

        if not file_info.get("ok"):
            await send(
                chat_id,
                "❌ Telegram не смог предоставить фотографию."
            )
            return

        file_data = file_info.get("result", {})
        file_path = file_data.get("file_path")

        if not file_path:
            await send(
                chat_id,
                "❌ Telegram не предоставил путь к фотографии."
            )
            return

        file_size = file_data.get("file_size", 0)

        if file_size and file_size > MAX_PHOTO_SIZE:
            await send(
                chat_id,
                "⚠️ Фотография слишком большая. "
                "Отправь изображение меньшего размера."
            )
            return

        file_url = (
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        )

        async with http.get(file_url) as response:
            if response.status != 200:
                logger.error(
                    "Не удалось скачать фото: HTTP %s",
                    response.status
                )

                await send(
                    chat_id,
                    "❌ Не удалось скачать фотографию из Telegram."
                )
                return

            image_data = await response.read()

        if len(image_data) > MAX_PHOTO_SIZE:
            await send(
                chat_id,
                "⚠️ Размер фотографии превышает допустимый."
            )
            return

        if not image_data:
            await send(
                chat_id,
                "❌ Telegram вернул пустой файл фотографии."
            )
            return

        logger.info(
            "Фото скачано: %s байт",
            len(image_data)
        )

        try:
            async with ocr_semaphore:
                candidates = await asyncio.to_thread(
                    recognize_label,
                    image_data
                )

        except Exception:
            logger.exception(
                "Техническая ошибка OCR: chat_id=%s message_id=%s",
                chat_id,
                message_id
            )

            await send(
                chat_id,
                "⚠️ Фотография получена, но произошла "
                "техническая ошибка распознавания.\n\n"
                "Это не означает, что снимок плохой. "
                "Попробуй повторить проверку немного позже."
            )
            return

        recognized_text = choose_best_ocr(candidates)

        caption = (
            message.get("caption") or ""
        ).strip()

        combined_text = "\n".join(
            part
            for part in (caption, recognized_text)
            if part
        ).strip()

        logger.info(
            "OCR завершён: вариантов=%s лучший_текст=%s символов",
            len(candidates),
            len(recognized_text)
        )

        if not combined_text:
            await send(
                chat_id,
                "🔎 Фотография получена, но текст "
                "не удалось распознать.\n\n"
                "Попробуй отправить фотографию обратной стороны "
                "упаковки крупнее или отдельным снимком."
            )
            return

        await check_additives(
            chat_id,
            combined_text,
            photo_text=recognized_text
        )

        logger.info(
            "Обработка фото завершена: chat_id=%s message_id=%s",
            chat_id,
            message_id
        )

    except Exception:
        logger.exception(
            "Необработанная ошибка фотографии: "
            "chat_id=%s message_id=%s",
            chat_id,
            message_id
        )

        try:
            await send(
                chat_id,
                "❌ При обработке фотографии произошла "
                "непредвиденная ошибка.\n\n"
                "Попробуй отправить её ещё раз."
            )
        except Exception:
            logger.exception(
                "Не удалось отправить сообщение об ошибке OCR"
            )


def background_task_done(task):
    background_tasks.discard(task)

    if task.cancelled():
        return

    error = task.exception()

    if error:
        logger.error(
            "Ошибка фоновой задачи",
            exc_info=(
                type(error),
                error,
                error.__traceback__
            )
        )


# ==================================================
# ПОДКЛЮЧЕНИЕ NEON И TELEGRAM WEBHOOK
# ==================================================

async def setup(app):
    global db, http

    load_additives()
    check_ocr_environment()

    http = ClientSession(
        timeout=ClientTimeout(total=40)
    )

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

    logger.info("Подключение к Neon...")

    try:
        db = await asyncpg.create_pool(
            dsn=dsn,
            ssl="require",
            min_size=1,
            max_size=5,
            timeout=30,
            command_timeout=30
        )

        logger.info("Пул соединений Neon создан")

    except Exception:
        logger.exception("Не удалось подключиться к Neon")
        raise

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
        CREATE TABLE IF NOT EXISTS processed_updates (
            update_id BIGINT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

    if background_tasks:
        tasks = list(background_tasks)

        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=30
            )

        except asyncio.TimeoutError:
            logger.warning(
                "Не все фоновые задачи завершились при остановке"
            )

    if db:
        await db.close()

    if http:
        await http.close()


# ==================================================
# СТАРТ
# ==================================================


async def start(chat_id):
    keyboard = {
        "keyboard": [
            [
                {"text": "📸 Проверить состав"},
                {"text": "🔬 Справочник"}
            ],
            [
                {"text": "➕ Добавить продукт"},
                {"text": "📦 Мои продукты"}
            ],
            [
                {"text": "✏️ Изменить продукт"},
                {"text": "🗑 Удалить продукт"}
            ],
            [
                {"text": "⏰ Напоминания"},
                {"text": "⚙️ Настройки"}
            ]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

    await send(
        chat_id,
        "🥫 Добро пожаловать в СрокКонтроль!\n\n"
        "Твой помощник по срокам годности "
        "и составу продуктов.\n\n"
        "Выбери нужный раздел в меню ниже 👇",
        reply_markup=keyboard
    )



# ==================================================
# ЧАСОВОЙ ПОЯС
# ==================================================

async def start_timezone(chat_id, user_id):
    user_states[user_id] = {
        "step": "timezone_choice"
    }

    lines = [
        "🌍 Выбери свой город или часовой пояс:",
        ""
    ]

    for number, (cities, timezone) in TIMEZONE_OPTIONS.items():
        lines.append(f"{number} — {cities}")

    lines.extend([
        "",
        "Отправь номер своего варианта.",
        "",
        "⏰ В дальнейшем напоминания будут приходить "
        "в 09:00 по выбранному времени.",
        "Для отмены: /cancel"
    ])

    await send(chat_id, "\n".join(lines))


# ==================================================
# ПОЛУЧЕНИЕ ПРОДУКТОВ ПОЛЬЗОВАТЕЛЯ
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
            status = f"⏳ Осталось дней: {remaining}"

        lines.append(
            f"{index}. {emoji} {row['name']}\n"
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

    await send(chat_id, "\n".join(lines))


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
        "step": "delete_mode"
    }

    lines = [
        "🗑 Удаление продуктов",
        "",
        "Твои продукты:",
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
        "Как будем удалять?",
        "",
        "1 — Один продукт",
        "2 — Несколько продуктов",
        "",
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
        "✏️ Редактирование продукта",
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

    # ВЫБОР ЧАСОВОГО ПОЯСА

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
            logger.exception(
                "Некорректный часовой пояс: %s",
                timezone
            )
            await send(
                chat_id,
                "⚠️ Не удалось установить часовой пояс. "
                "Попробуй позже."
            )
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

    # ДОБАВЛЕНИЕ: НАЗВАНИЕ

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

    # ДОБАВЛЕНИЕ: ДАТА

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

    # УДАЛЕНИЕ: ВЫБОР РЕЖИМА

    if step == "delete_mode":
        answer = text.strip()

        if answer not in ("1", "2"):
            await send(
                chat_id,
                "❌ Выбери вариант:\n"
                "1 — Один продукт\n"
                "2 — Несколько продуктов"
            )
            return

        rows = await get_user_products(user_id)

        if not rows:
            user_states.pop(user_id, None)
            await send(chat_id, "📦 Корзина уже пуста.")
            return

        if answer == "1":
            state["step"] = "delete_number"

            await send(
                chat_id,
                "🗑 Отправь номер одного продукта, "
                "который нужно удалить."
            )
        else:
            state["step"] = "delete_multiple_numbers"

            await send(
                chat_id,
                "🗑 Введи номера продуктов через запятую.\n\n"
                "Например: 1, 3, 5\n\n"
                f"Сейчас в корзине продуктов: {len(rows)}\n\n"
                "Для отмены: /cancel"
            )

        return

    # УДАЛЕНИЕ: ВЫБОР ОДНОГО ПРОДУКТА

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

    # УДАЛЕНИЕ: ВЫБОР НЕСКОЛЬКИХ ПРОДУКТОВ

    if step == "delete_multiple_numbers":
        rows = await get_user_products(user_id)

        if not rows:
            user_states.pop(user_id, None)
            await send(chat_id, "📦 Корзина уже пуста.")
            return

        parts = re.split(r"[,;\s]+", text.strip())

        if not parts or any(not part.isdigit() for part in parts):
            await send(
                chat_id,
                "❌ Введи только номера через запятую.\n\n"
                "Например: 1, 3, 5"
            )
            return

        numbers = sorted(set(int(part) for part in parts))

        if any(number < 1 or number > len(rows) for number in numbers):
            await send(
                chat_id,
                f"❌ Номера должны быть от 1 до {len(rows)}."
            )
            return

        selected_rows = [
            rows[number - 1]
            for number in numbers
        ]

        state["product_ids"] = [
            row["id"]
            for row in selected_rows
        ]

        state["product_names"] = [
            row["name"]
            for row in selected_rows
        ]

        state["step"] = "delete_multiple_confirm"

        lines = [
            "🗑 Будут удалены следующие продукты:",
            ""
        ]

        for number, row in zip(numbers, selected_rows):
            lines.append(
                f"{number}. {product_emoji(row['name'])} "
                f"{row['name']} — "
                f"{row['expiry_date'].strftime('%d.%m.%Y')}"
            )

        lines.extend([
            "",
            f"Всего выбрано: {len(selected_rows)}",
            "",
            "Точно удалить выбранные продукты?",
            "",
            "1 — Да, удалить",
            "2 — Нет, отменить"
        ])

        await send(chat_id, "\n".join(lines))
        return

    # УДАЛЕНИЕ: ПОДТВЕРЖДЕНИЕ НЕСКОЛЬКИХ ПРОДУКТОВ

    if step == "delete_multiple_confirm":
        answer = text.strip().casefold()

        if answer in ("1", "да", "удалить"):
            result = await db.execute("""
                DELETE FROM products
                WHERE id = ANY($1::BIGINT[])
                AND user_id = $2
            """, state["product_ids"], user_id)

            deleted_count = int(
                result.split()[-1]
            )

            user_states.pop(user_id, None)

            await send(
                chat_id,
                f"✅ Удаление завершено!\n\n"
                f"🗑 Удалено продуктов: {deleted_count}\n\n"
                "Посмотреть оставшиеся продукты: /list"
            )

        elif answer in ("2", "нет", "отмена"):
            user_states.pop(user_id, None)

            await send(
                chat_id,
                "↩️ Массовое удаление отменено.\n\n"
                "Все продукты остались в корзине."
            )

        else:
            await send(
                chat_id,
                "Выбери один вариант:\n"
                "1 — Да, удалить\n"
                "2 — Нет, отменить"
            )

        return

    # УДАЛЕНИЕ: ПОДТВЕРЖДЕНИЕ ОДНОГО ПРОДУКТА

    if step == "delete_confirm":
        answer = text.strip().casefold()

        if answer in ("1", "да", "удалить"):
            result = await db.execute("""
                DELETE FROM products
                WHERE id = $1 AND user_id = $2
            """, state["product_id"], user_id)

            product_name = state["product_name"]
            user_states.pop(user_id, None)

            if result == "DELETE 1":
                await send(
                    chat_id,
                    f"✅ Продукт «{product_name}» "
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

    # РЕДАКТИРОВАНИЕ: ВЫБОР ПРОДУКТА

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

    # РЕДАКТИРОВАНИЕ: ВЫБОР ПОЛЯ

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

    # РЕДАКТИРОВАНИЕ: НОВОЕ НАЗВАНИЕ

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

    # РЕДАКТИРОВАНИЕ: НОВАЯ ДАТА

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

        update_id = update.get("update_id")

        if update_id is not None:
            try:
                inserted = await db.fetchval("""
                    INSERT INTO processed_updates (update_id)
                    VALUES ($1)
                    ON CONFLICT (update_id) DO NOTHING
                    RETURNING update_id
                """, update_id)

                if inserted is None:
                    logger.warning(
                        "Повторное обновление пропущено: update_id=%s",
                        update_id
                    )
                    return web.json_response({"ok": True})

            except Exception:
                logger.exception(
                    "Ошибка проверки повторного обновления"
                )
                return web.Response(status=500)

        message = update.get("message", {})

        text = (message.get("text") or "").strip()

        chat_id = message.get(
            "chat",
            {}
        ).get("id")

        user_id = message.get(
            "from",
            {}
        ).get("id")

        message_id = message.get("message_id")

        if not chat_id or not user_id:
            return web.json_response({"ok": True})

        if not text and not message.get("photo"):
            return web.json_response({"ok": True})

        if message.get("photo"):
            logger.info(
                "Получено фото: update_id=%s message_id=%s",
                update_id,
                message_id
            )

            task = asyncio.create_task(
                handle_photo(
                    chat_id,
                    user_id,
                    message
                )
            )

            background_tasks.add(task)
            task.add_done_callback(background_task_done)

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
                "или английское название вещества.\n\n"
                "📸 Также можешь отправить фотографию "
                "состава продукта для проверки."
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
