import os, json, time

import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DATA_FILE = "alerts.json"


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(data, chat_id: str):
    users = data.setdefault("users", {})
    return users.setdefault(chat_id, {"alerts": {}, "cooldown_min": 360})


async def fetch_price_and_recent_high(ticker: str, lookback_days: int = 60):
    t = yf.Ticker(ticker)
    hist = t.history(period=f"{lookback_days}d", interval="1d")
    if hist is None or hist.empty:
        return None, None
    close = float(hist["Close"].iloc[-1])
    recent_high = float(hist["Close"].max())
    return close, recent_high


def pct_drop_from_high(price: float, recent_high: float) -> float:
    if recent_high <= 0:
        return 0.0
    return (recent_high - price) / recent_high * 100.0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot de alertas activo.\n\n"
        "Comandos:\n"
        "• /add TICKER %  (ej: /add QQQ 10)\n"
        "• /list\n"
        "• /remove TICKER\n\n"
        "Te avisaré cuando el precio caiga X% desde el máximo reciente (últimos 60 días)."
    )
    await update.message.reply_text(msg)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /add TICKER %  (ej: /add NVDA 15)")

    ticker = context.args[0].upper().strip()
    try:
        pct = float(context.args[1])
        if pct <= 0 or pct > 80:
            raise ValueError()
    except Exception:
        return await update.message.reply_text("El % debe ser un número válido (ej: 10, 15).")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    u["alerts"][ticker] = {"pct": pct, "last_sent_ts": 0}
    save_data(data)

    await update.message.reply_text(f"✅ Alerta creada: {ticker} caída ≥ {pct:.1f}% desde el máximo reciente.")


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    alerts = u.get("alerts", {})
    if not alerts:
        return await update.message.reply_text("No tienes alertas. Usa /add QQQ 10")

    lines = ["📌 Tus alertas:"]
    for tkr, cfg in alerts.items():
        lines.append(f"• {tkr}: caída ≥ {cfg['pct']}%")
    await update.message.reply_text("\n".join(lines))


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /remove TICKER  (ej: /remove QQQ)")

    ticker = context.args[0].upper().strip()
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    if ticker in u["alerts"]:
        del u["alerts"][ticker]
        save_data(data)
        return await update.message.reply_text(f"🗑️ Alerta eliminada: {ticker}")

    await update.message.reply_text("Ese ticker no estaba en tu lista. Usa /list")


async def check_alerts(app: Application):
    data = load_data()
    users = data.get("users", {})
    if not users:
        return

    for chat_id, u in users.items():
        alerts = u.get("alerts", {})
        if not alerts:
            continue

        cooldown_sec = int(u.get("cooldown_min", 360)) * 60
        for ticker, cfg in list(alerts.items()):
            pct_target = float(cfg.get("pct", 10))
            last_sent = int(cfg.get("last_sent_ts", 0))

            if time.time() - last_sent < cooldown_sec:
                continue

            price, recent_high = await fetch_price_and_recent_high(ticker, lookback_days=60)
            if price is None or recent_high is None:
                continue

            drop = pct_drop_from_high(price, recent_high)
            if drop >= pct_target:
                cfg["last_sent_ts"] = int(time.time())
                data["users"][chat_id]["alerts"][ticker] = cfg
                save_data(data)

                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"📉 ALERTA: {ticker}\n"
                        f"Caída: {drop:.1f}% desde el máximo reciente\n"
                        f"Precio aprox: ${price:.2f}\n"
                        f"Máximo 60d: ${recent_high:.2f}\n\n"
                        f"👉 Si vas a comprar, dime cuánto quieres meter y te digo cómo repartirlo."
                    )
                )


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    await check_alerts(context.application)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("remove", remove))

    app.job_queue.run_repeating(check_job, interval=15 * 60, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
