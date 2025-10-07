import json
import time
import logging
import requests
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
from keep_alive import keep_alive

# === ВАЖНО: вставь сюда ТОЛЬКО свой НОВЫЙ токен от @BotFather ===
BOT_TOKEN = "8286820563:AAHupL7wVd1Z8HnzLz2NYXoDI1MSKyIqukI"

# Файл для хранения данных
DATA_FILE = "data.json"

# Порог уведомления (в процентах)
PRICE_CHANGE_THRESHOLD = 1.0
CHECK_INTERVAL_SECONDS = 10

# Словарь user_coins: {chat_id (int): { "BTCUSDT": last_price, ... } }
user_coins = {}

# Словарь запланированных job_id чтобы не дублировать задачи
scheduled_chats = set()

# Логи
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ----------------- Работа с файлом -----------------
def load_data():
    global user_coins
    try:
        with open(DATA_FILE, "r") as f:
            raw = json.load(f)
        # ключи в JSON будут строками — приводим к int
        user_coins = {int(k): v for k, v in raw.items()}
        logger.info(f"Loaded data for {len(user_coins)} chats.")
    except FileNotFoundError:
        user_coins = {}
    except Exception as e:
        logger.exception("Error loading data file, starting with empty data.")
        user_coins = {}


def save_data():
    try:
        # приводим ключи к строкам для JSON
        with open(DATA_FILE, "w") as f:
            json.dump({str(k): v for k, v in user_coins.items()}, f)
        logger.debug("Data saved.")
    except Exception:
        logger.exception("Failed to save data.")


# ----------------- MEXC price -----------------
def get_price_mexc(symbol_raw):
    try:
        s = symbol_raw.upper()
        # если пользователь добавил без USDT — добавим
        if not s.endswith("USDT"):
            s = s + "USDT"
        url = f"https://api.mexc.com/api/v3/ticker/price?symbol={s}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, dict) or "price" not in data:
            logger.warning(f"MEXC returned unexpected for {s}: {data}")
            return None, s  # вернём модифицированный тикер
        price = float(data["price"])
        return price, s
    except Exception as e:
        logger.exception(f"Error fetching price for {symbol_raw}")
        return None, symbol_raw


# ----------------- Планирование задач -----------------
def schedule_check_for_chat(job_queue, chat_id):
    if chat_id in scheduled_chats:
        logger.debug(f"Chat {chat_id} already scheduled, skipping")
        return
    
    # Добавляем уникальное имя для задачи, чтобы избежать дубликатов
    job_name = f"check_prices_{chat_id}"
    
    # Удаляем существующие задачи с таким же именем (если есть)
    current_jobs = job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()
        logger.info(f"Removed duplicate job for chat {chat_id}")
    
    # run_repeating будет передавать context.job.context как chat_id
    job_queue.run_repeating(check_prices,
                            interval=CHECK_INTERVAL_SECONDS,
                            first=10,
                            context=chat_id,
                            name=job_name)
    scheduled_chats.add(chat_id)
    logger.info(f"Scheduled checks for chat {chat_id}")


# ----------------- Проверка цен (выполняется по расписанию) -----------------
def check_prices(context: CallbackContext):
    chat_id = context.job.context
    if not chat_id:
        return
    if chat_id not in user_coins or not user_coins[chat_id]:
        return

    for symbol, old_price in list(user_coins[chat_id].items()):
        price, used_symbol = get_price_mexc(symbol)
        # если API вернул None — пропускаем
        if price is None:
            continue

        # защита от деления на ноль
        if not old_price or old_price == 0:
            user_coins[chat_id][symbol] = price
            save_data()
            continue

        change = (price - old_price) / old_price * 100

        if abs(change) >= PRICE_CHANGE_THRESHOLD:
            direction = "📈 выросла" if change > 0 else "📉 упала"
            text = f"{used_symbol}: {direction} на {change:.2f}%\n💰 Текущая цена: {price}"
            try:
                context.bot.send_message(chat_id=chat_id, text=text)
                # обновляем "last price" только после успешной отправки
                user_coins[chat_id][symbol] = price
                save_data()
                logger.info(
                    f"Sent update to {chat_id} for {symbol}: {change:.2f}%")
            except Exception:
                logger.exception(f"Failed to send message to {chat_id}")


# ----------------- Команды бота -----------------
def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if chat_id not in user_coins:
        user_coins[chat_id] = {}
        save_data()

    update.message.reply_text(
        "👋 Привет! Я Crypto Radar.\n"
        "Добавь монеты через /add SYMBOL (например /add BTC) — я пришлю уведомления при изменениях ≥1%.\n"
        "Команды: /add /remove /list /help")

    # планируем проверки для этого чата
    schedule_check_for_chat(context.job_queue, chat_id)


def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Команды:\n/add SYMBOL — добавить монету\n/remove SYMBOL — удалить\n/list — показать список"
    )


def add_coin(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if chat_id not in user_coins:
        user_coins[chat_id] = {}

    if not context.args:
        update.message.reply_text("Укажи символ, пример: /add BTC")
        return

    symbol = context.args[0].upper()
    price, used_symbol = get_price_mexc(symbol)
    if price is None:
        update.message.reply_text(f"Не найдено на MEXC: {symbol}")
        return

    # Сохраняем ключ как тот, который используем для запросов (used_symbol)
    user_coins[chat_id][used_symbol] = price
    save_data()
    update.message.reply_text(
        f"✅ {used_symbol} добавлен. Текущая цена: {price}")
    # Убедимся, что есть задача по проверке для этого чата
    schedule_check_for_chat(context.job_queue, chat_id)


def remove_coin(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Укажи символ, пример: /remove BTC")
        return
    symbol = context.args[0].upper()
    # клиент мог сохранять тикер как SYMBOLUSDT — попробуем оба варианта
    keys = list(user_coins.get(chat_id, {}).keys())
    found = None
    for k in keys:
        if k.upper().startswith(
                symbol):  # startswith позволяет ловить BTC и BTCUSDT
            found = k
            break
    if found:
        del user_coins[chat_id][found]
        save_data()
        update.message.reply_text(f"🗑 Удалён {found}")
    else:
        update.message.reply_text("Монета не найдена в твоём списке.")


def list_coins_cmd(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    coins = user_coins.get(chat_id, {})
    if not coins:
        update.message.reply_text("У тебя нет отслеживаемых монет.")
        return
    lines = []
    for sym, last in coins.items():
        lines.append(f"{sym}: {last}")
    update.message.reply_text("Твои монеты:\n" + "\n".join(lines))


# ----------------- MAIN -----------------
def main():
    # загружаем сохранённые данные
    load_data()

    # запускаем keep-alive (Flask)
    keep_alive()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("add", add_coin))
    dp.add_handler(CommandHandler("remove", remove_coin))
    dp.add_handler(CommandHandler("list", list_coins_cmd))

    # стартуем polling
    updater.start_polling()
    logger.info("Bot started.")

    # запланируем проверки для всех уже загруженных чатов
    for chat_id in list(user_coins.keys()):
        try:
            schedule_check_for_chat(updater.job_queue, int(chat_id))
        except Exception:
            logger.exception(f"Failed to schedule chat {chat_id}")

    updater.idle()


if __name__ == "__main__":
    main()
