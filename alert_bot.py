import os
import json
import time
from datetime import timezone, datetime

import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DATA_FILE = "alerts.json"

# ===================== STORAGE =====================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_user(data, chat_id):
    return data.setdefault("users", {}).setdefault(chat_id, {
        "alerts": {},
        "cooldown_min": 360
    })

# ===================== MARKET =====================
async def fetch_price_and_high(ticker, days=60):
    t = yf.Ticker(ticker)
    hist = t.history(period=f"{days}d", interval="1d")
    if hist is None or hist.empty:
        return None, None
    return float(hist["Close"].iloc[-1]), float(hist["Close"].max())

def pct_drop(price, high):
    return (high - price) / high * 100 if high > 0 else 0

# ===================== COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activo\n\n"
        "📉 BUY:\n"
        "/add NVDA 15\n\n"
        "💰 SELL:\n"
        "/entry NVDA 175\n"
        "/setsell NVDA 20 10\n\n"
        "📋 Otros:\n"
        "/list\n/remove NVDA\n/show NVDA"
    )

async def add(update, context):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /add TICKER %")

    ticker = context.args[0].upper()
    pct = float(context.args[1])

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    u["alerts"][ticker] = {
        "pct": pct,
        "last_buy": 0,
        "entry": None,
        "tp": 20,
        "sl": 10,
        "last_sell": 0
    }

    save_data(data)
    await update.message.reply_text(f"✅ BUY creado: {ticker} ≥ {pct}%")

async def entry(update, context):
    ticker = context.args[0].upper()
    price = float(context.args[1])

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    u["alerts"].setdefault(ticker, {})["entry"] = price

    save_data(data)
    await update.message.reply_text(f"📌 Entry guardado {ticker} @ ${price}")

async def setsell(update, context):
    ticker = context.args[0].upper()
    tp = float(context.args[1])
    sl = float(context.args[2])

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    cfg = u["alerts"].setdefault(ticker, {})
    cfg["tp"] = tp
    cfg["sl"] = sl

    save_data(data)
    await update.message.reply_text(f"💰 SELL {ticker} TP {tp}% | SL {sl}%")

async def list_alerts(update, context):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    if not u["alerts"]:
        return await update.message.reply_text("No tienes alertas")

    msg = ["📋 Tus alertas:"]
    for t, c in u["alerts"].items():
        msg.append(f"{t}: BUY {c.get('pct')}% | TP {c.get('tp')} | SL {c.get('sl')}")
    await update.message.reply_text("\n".join(msg))

async def remove(update, context):
    ticker = context.args[0].upper()
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    u["alerts"].pop(ticker, None)
    save_data(data)
    await update.message.reply_text(f"🗑️ Eliminado {ticker}")

async def show(update, context):
    ticker = context.args[0].upper()
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    c = u["alerts"].get(ticker)
    if not c:
        return await update.message.reply_text("No existe")

    await update.message.reply_text(
        f"{ticker}\n"
        f"BUY ≥ {c['pct']}%\n"
        f"Entry: {c['entry']}\n"
        f"TP: {c['tp']}%\n"
        f"SL: {c['sl']}%"
    )

# ===================== CHECKER =====================
async def check_job(context):
    app = context.application
    data = load_data()

    for chat_id, u in data["users"].items():
        for ticker, c in u["alerts"].items():
            price, high = await fetch_price_and_high(ticker)
            if not price:
                continue

            # BUY
            if time.time() - c["last_buy"] > u["cooldown_min"] * 60:
                if pct_drop(price, high) >= c["pct"]:
                    c["last_buy"] = int(time.time())
                    save_data(data)
                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"📉 BUY {ticker}\nPrecio ${price:.2f}\nCaída {pct_drop(price, high):.1f}%"
                    )

            # SELL
            if c["entry"]:
                pnl = (price - c["entry"]) / c["entry"] * 100
                if pnl >= c["tp"] and time.time() - c["last_sell"] > 21600:
                    c["last_sell"] = int(time.time())
                    save_data(data)
                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"💰 TAKE PROFIT {ticker}\n+{pnl:.1f}%"
                    )
                elif pnl <= -c["sl"] and time.time() - c["last_sell"] > 21600:
                    c["last_sell"] = int(time.time())
                    save_data(data)
                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"🛑 STOP LOSS {ticker}\n{pnl:.1f}%"
                    )

# ===================== MAIN =====================
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no definido")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("entry", entry))
    app.add_handler(CommandHandler("setsell", setsell))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("show", show))

    # ⏱️ cada 5 minutos
    app.job_queue.run_repeating(check_job, interval=300, first=10)

    # 🔒 evita conflictos
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
