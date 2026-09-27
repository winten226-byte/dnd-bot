import json
import logging
import sqlite3
from datetime import datetime, time, timezone

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ==================== НАСТРОЙКИ ====================
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
DB_PATH = "dnd_bot.db"

SLOTS = {
    "fri_sun": "Пт–Вс",
    "mon_wed": "Пн–Ср",
    "tue_thu": "Вт–Чт",
    "pass": "Пасс",
}

POPULAR_THRESHOLD = 6

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_status (
            user_id INTEGER, chat_id INTEGER, username TEXT,
            slot TEXT, updated_at TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    conn.commit()
    conn.close()


def save_status(user_id, chat_id, username, slot):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO user_status VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET
            username=excluded.username, slot=excluded.slot,
            updated_at=excluded.updated_at
    """, (user_id, chat_id, username, slot, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_status(user_id, chat_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT slot, updated_at FROM user_status WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    ).fetchone()
    conn.close()
    return row


def get_all_statuses(chat_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, slot FROM user_status WHERE chat_id=? ORDER BY slot, username",
        (chat_id,)
    ).fetchall()
    conn.close()
    return rows


def get_all_chat_ids():
    """Все chat_id, где есть записи — для уведомлений."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT chat_id FROM user_status").fetchall()
    conn.close()
    return [r[0] for r in rows]


def clear_all_statuses():
    """Удалить все записи из user_status."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM user_status")
    conn.commit()
    conn.close()


# ==================== КЛАВИАТУРЫ ====================
def main_reply_kb():
    return ReplyKeyboardMarkup(
        [["🎲 D&D Запись"]],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_inline_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Отметить дни", callback_data="menu_days")],
        [InlineKeyboardButton("👥 Все записи", callback_data="menu_all")],
    ])


def slots_inline_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Пт–Вс", callback_data="set_fri_sun")],
        [InlineKeyboardButton("📅 Пн–Ср", callback_data="set_mon_wed")],
        [InlineKeyboardButton("📅 Вт–Чт", callback_data="set_tue_thu")],
        [InlineKeyboardButton("🚫 Пасс", callback_data="set_pass")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
    ])


# ==================== RAW API ====================
async def _raw(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, timeout=15)
            data = r.json()
    except Exception as e:
        logger.error(f"RAW ✗ {method}: {e}")
        return {"ok": False, "error": str(e)}

    if not data.get("ok"):
        logger.error(f"RAW ✗ {method}: {data.get('description')}")
    return data


async def send_ephemeral(chat_id, user_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "ephemeral_message_parameters": json.dumps({
            "receiver_user_id": user_id,
        }),
    }
    if reply_markup is not None and hasattr(reply_markup, "to_dict"):
        payload["reply_markup"] = json.dumps(reply_markup.to_dict())

    data = await _raw("sendMessage", payload)
    if data.get("ok"):
        return data["result"]
    return None


async def edit_ephemeral(chat_id, user_id, ephemeral_message_id, text,
                         reply_markup=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "receiver_user_id": user_id,
        "ephemeral_message_id": ephemeral_message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None and hasattr(reply_markup, "to_dict"):
        payload["reply_markup"] = json.dumps(reply_markup.to_dict())

    return await _raw("editEphemeralMessageText", payload)


async def safe_answer(query, text: str = None, show_alert: bool = False):
    try:
        if text is not None:
            await query.answer(text=text, show_alert=show_alert)
        else:
            await query.answer()
    except Exception as e:
        logger.warning(f"answer callback failed: {e}")


async def refresh_ephemeral(context, chat_id, user_id, text,
                            reply_markup=None, parse_mode="HTML"):
    old_id = context.user_data.get("ephemeral_menu_id")

    if old_id:
        data = await edit_ephemeral(
            chat_id, user_id, old_id, text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )
        if data.get("ok"):
            return
        logger.warning(f"edit не удался, отправляем новое: {data.get('description')}")
        context.user_data.pop("ephemeral_menu_id", None)

    result = await send_ephemeral(
        chat_id, user_id, text,
        reply_markup=reply_markup, parse_mode=parse_mode,
    )
    if result:
        new_id = result.get("ephemeral_message_id") or result.get("message_id")
        context.user_data["ephemeral_menu_id"] = new_id


# ==================== ХЭНДЛЕРЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для организации D&D игр.\nНажми кнопку внизу ⬇️",
        reply_markup=main_reply_kb(),
    )


async def group_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    await refresh_ephemeral(
        context, chat.id, user.id,
        "🎲 <b>Меню записи</b>\n\nВыбери действие:",
        reply_markup=main_inline_kb(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    chat_id = query.message.chat_id

    # === Меню выбора дней ===
    if data == "menu_days":
        await safe_answer(query)
        await refresh_ephemeral(
            context, chat_id, user.id,
            "📅 <b>Выбери удобный слот:</b>\n\n"
            "• Пт–Вс\n• Пн–Ср\n• Вт–Чт\n• Пасс (не участвую)",
            reply_markup=slots_inline_kb(),
        )
        return

    # === Назад ===
    if data == "menu_back":
        await safe_answer(query)
        await refresh_ephemeral(
            context, chat_id, user.id,
            "🎲 <b>Меню записи</b>\n\nВыбери действие:",
            reply_markup=main_inline_kb(),
        )
        return

    # === Публичный просмотр всех записей ===
    if data == "menu_all":
        await safe_answer(query)

        statuses = get_all_statuses(chat_id)

        if not statuses:
            text = "📊 <b>Записи на игру</b>\n\nПока никто не отметился."
        else:
            by_slot = {}
            for uid, username, slot in statuses:
                by_slot.setdefault(slot, []).append(username)

            lines = ["📊 <b>Записи на игру</b>\n"]
            for slot_key in ["fri_sun", "mon_wed", "tue_thu"]:
                names = by_slot.get(slot_key, [])
                slot_name = SLOTS.get(slot_key, slot_key)
                if len(names) >= POPULAR_THRESHOLD:
                    lines.append(f"🔥 <b>{slot_name}</b> — <b>{len(names)} чел.</b>")
                    lines.append(f"   {', '.join(names)}")
                else:
                    lines.append(f"• <b>{slot_name}</b> — {len(names)} чел.")
                    if names:
                        lines.append(f"   {', '.join(names)}")

            pass_names = by_slot.get("pass", [])
            if pass_names:
                lines.append(f"\n🚫 <b>Пасс</b> — {len(pass_names)} чел.")
                lines.append(f"   {', '.join(pass_names)}")

            text = "\n".join(lines)

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить публичный список: {e}")
        return

    # === Установка статуса ===
    if data.startswith("set_"):
        slot_key = data[4:]
        slot_name = SLOTS.get(slot_key, slot_key)
        username = user.username or user.full_name
        save_status(user.id, chat_id, username, slot_key)

        await safe_answer(query, text=f"✅ Сохранено: {slot_name}")

        await refresh_ephemeral(
            context, chat_id, user.id,
            f"✅ <b>Статус обновлён!</b>\n\nТы выбрал: <b>{slot_name}</b>",
            reply_markup=main_inline_kb(),
        )
        return

    await safe_answer(query, text="Неизвестное действие")


# ==================== ЕЖЕНЕДЕЛЬНЫЙ СБРОС ====================
async def weekly_reset(context: ContextTypes.DEFAULT_TYPE):
    """
    Задача: очистка всех записей раз в неделю.
    Опционально уведомляет все группы, где были записи.
    """
    chat_ids = get_all_chat_ids()
    clear_all_statuses()
    logger.info(f"🔄 Еженедельный сброс записей. Уведомляю {len(chat_ids)} чат(ов).")

    for cid in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=cid,
                text=(
                    "🔄 <b>Новая неделя!</b>\n\n"
                    "Записи на игру сброшены. "
                    "Нажми «🎲 D&D Запись», чтобы отметить дни."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить {cid}: {e}")


# ==================== ОБРАБОТКА ОШИБОК ====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Исключение:", exc_info=context.error)


# ==================== ЗАПУСК ====================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🎲 D&D Запись$"), group_menu))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    # Планируем еженедельный сброс — каждый понедельник в 00:00 UTC
    if app.job_queue:
        app.job_queue.run_daily(
            weekly_reset,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            days=(0,),  # 0 = понедельник
            name="weekly_reset",
        )
        logger.info("⏰ Запланирован еженедельный сброс (Пн 00:00 UTC)")
    else:
        logger.warning("⚠️ JobQueue недоступен. Установи APScheduler: "
                       "pip install \"python-telegram-bot[job-queue]\"")

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
