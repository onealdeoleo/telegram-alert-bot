import os
import json
import time
from datetime import datetime, timezone

import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DATA_FILE = "alerts.json"

# -------------------- storage helpers --------------------
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
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user(data, chat_id: str):
    users = data.setdefault("users", {})
    return users.setdefault(chat_id, {
        "alerts": {},          # ticker -> cfg
        "cooldown_min": 360    # cooldown BUY alerts
    })

# -------------------- market helpers --------------------
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

# -------------------- commands --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot de alertas activo.\n\n"
        "📌 Comandos:\n"
        "• /add TICKER %      (ej: /add NVDA 15)\n"
        "• /list\n"
        "• /remove TICKER\n\n"
        "💰 Ventas (TP/SL):\n"
        "• /entry TICKER PRECIO   (ej: /entry NVDA 175)\n"
        "• /setsell TICKER TP SL  (ej: /setsell NVDA 20 10)\n"
        "• /show TICKER\n\n"
        "🔔 Te aviso cuando:\n"
        "• BUY: caiga X% desde el máximo reciente (60d)\n"
        "• SELL: llegue a TP o toque SL desde tu entry\n"
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

    cfg = u["alerts"].setdefault(ticker, {})
    cfg["pct"] = pct
    cfg.setdefault("last_sent_ts", 0)

    # defaults para sell (no obligatorios)
    cfg.setdefault("entry", None)
    cfg.setdefault("tp", 20.0)
    cfg.setdefault("sl", 10.0)
    cfg.setdefault("last_sell_ts", 0)

    save_data(data)
    await update.message.reply_text(f"✅ Alerta BUY creada: {ticker} caída ≥ {pct:.1f}% desde el máximo 60d.")

async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    alerts = u.get("alerts", {})
    if not alerts:
        return await update.message.reply_text("No tienes alertas. Usa /add QQQ 10")

    lines = ["📌 Tus alertas:"]
    for tkr, cfg in alerts.items():
        pct = cfg.get("pct", 10)
        entry_price = cfg.get("entry", None)
        tp = cfg.get("tp", None)
        sl = cfg.get("sl", None)

        extra = ""
        if entry_price:
            extra = f" | entry ${float(entry_price):.2f} | TP {float(tp):.0f}% | SL {float(sl):.0f}%"
        lines.append(f"• {tkr}: BUY caída ≥ {pct:.1f}%{extra}")

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
        return await update.message.reply_text(f"🗑️ Eliminado: {ticker}")
    await update.message.reply_text("Ese ticker no estaba en tu lista. Usa /list")

async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /entry TICKER PRECIO  (ej: /entry NVDA 175)")

    ticker = context.args[0].upper().strip()
    try:
        entry_price = float(context.args[1])
        if entry_price <= 0:
            raise ValueError()
    except Exception:
        return await update.message.reply_text("Precio inválido. Ej: /entry NVDA 175")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    cfg = u["alerts"].setdefault(ticker, {"pct": 10, "last_sent_ts": 0})
    cfg["entry"] = entry_price
    cfg.setdefault("tp", 20.0)
    cfg.setdefault("sl", 10.0)
    cfg.setdefault("last_sell_ts", 0)

    save_data(data)
    await update.message.reply_text(f"✅ Entry guardado: {ticker} @ ${entry_price:.2f}")

async def setsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        return await update.message.reply_text("Uso: /setsell TICKER TP SL  (ej: /setsell NVDA 20 10)")

    ticker = context.args[0].upper().strip()
    try:
        tp = float(context.args[1])
        sl = float(context.args[2])
        if tp <= 0 or tp > 300 or sl <= 0 or sl > 80:
            raise ValueError()
    except Exception:
        return await update.message.reply_text("TP/SL inválidos. Ej: /setsell NVDA 20 10")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    cfg = u["alerts"].setdefault(ticker, {"pct": 10, "last_sent_ts": 0})
    cfg["tp"] = tp
    cfg["sl"] = sl
    cfg.setdefault("last_sell_ts", 0)

    save_data(data)
    await update.message.reply_text(f"✅ SELL config: {ticker} TP={tp:.1f}% | SL={sl:.1f}%")

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /show TICKER  (ej: /show NVDA)")
    ticker = context.args[0].upper().strip()

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    cfg = u.get("alerts", {}).get(ticker)
    if not cfg:
        return await update.message.reply_text("Ese ticker no está configurado. Usa /add primero.")

    pct = float(cfg.get("pct", 10))
    entry_price = cfg.get("entry", None)
    tp = cfg.get("tp", None)
    sl = cfg.get("sl", None)

    await update.message.reply_text(
        f"📌 {ticker}\n"
        f"BUY: caída ≥ {pct:.1f}% (max 60d)\n"
        f"ENTRY: {('No definido' if not entry_price else f'${float(entry_price):.2f}')}\n"
        f"TP: {('No definido' if tp is None else f'{float(tp):.1f}%')}\n"
        f"SL: {('No definido' if sl is None else f'{float(sl):.1f}%')}"
    )

# -------------------- background job --------------------
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    data = load_data()
    users = data.get("users", {})
    if not users:
        return

    for chat_id, u in users.items():
        alerts = u.get("alerts", {})
        if not alerts:
            continue

        cooldown_sec = int(u.get("cooldown_min", 360)) * 60
        sell_cooldown_sec = 6 * 60 * 60  # 6 horas para no spamear SELL

        for ticker, cfg in list(alerts.items()):
            pct_target = float(cfg.get("pct", 10))
            last_sent = int(cfg.get("last_sent_ts", 0))

            price, recent_high = await fetch_price_and_recent_high(ticker, lookback_days=60)
            if price is None or recent_high is None:
                continue

            # ---------- BUY SIGNAL ----------
            if time.time() - last_sent >= cooldown_sec:
                drop = pct_drop_from_high(price, recent_high)
                if drop >= pct_target:
                    cfg["last_sent_ts"] = int(time.time())
                    data["users"][chat_id]["alerts"][ticker] = cfg
                    save_data(data)

                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            f"📉 ALERTA BUY: {ticker}\n"
                            f"Caída: {drop:.1f}% desde el máximo reciente\n"
                            f"Precio aprox: ${price:.2f}\n"
                            f"Máximo 60d: ${recent_high:.2f}\n\n"
                            f"👉 Si vas a comprar, dime cuánto quieres meter y te digo cómo repartirlo."
                        )
                    )

            # ---------- SELL SIGNAL (TP/SL) ----------
            entry_price = cfg.get("entry", None)
            tp = float(cfg.get("tp", 0) or 0)
            sl = float(cfg.get("sl", 0) or 0)
            last_sell = int(cfg.get("last_sell_ts", 0))

            if entry_price and (time.time() - last_sell >= sell_cooldown_sec):
                entry_price = float(entry_price)
                pnl_pct = (price - entry_price) / entry_price * 100.0

                if tp > 0 and pnl_pct >= tp:
                    cfg["last_sell_ts"] = int(time.time())
                    data["users"][chat_id]["alerts"][ticker] = cfg
                    save_data(data)

                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            f"💰 ALERTA SELL (TAKE PROFIT): {ticker}\n"
                            f"Ganancia: +{pnl_pct:.1f}%\n"
                            f"Precio aprox: ${price:.2f}\n"
                            f"Entry: ${entry_price:.2f}\n"
                            f"TP objetivo: {tp:.1f}%\n\n"
                            f"👉 ¿Vendes todo o por partes? Te recomiendo la mejor opción."
                        )
                    )

                elif sl > 0 and pnl_pct <= -sl:
                    cfg["last_sell_ts"] = int(time.time())
                    data["users"][chat_id]["alerts"][ticker] = cfg
                    save_data(data)

                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            f"🛑 ALERTA SELL (STOP LOSS): {ticker}\n"
                            f"Pérdida: {pnl_pct:.1f}%\n"
                            f"Precio aprox: ${price:.2f}\n"
                            f"Entry: ${entry_price:.2f}\n"
                            f"SL límite: {sl:.1f}%\n\n"
                            f"👉 ¿Sales todo o reduces? Te digo la salida más limpia."
                        )
                    )

# -------------------- main --------------------
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno (Render).")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("entry", entry))
    app.add_handler(CommandHandler("setsell", setsell))
    app.add_handler(CommandHandler("show", show))

    # Background checks:
    # Cada 5 minutos (cámbialo a 15*60 si quieres 15 min)
    app.job_queue.run_repeating(check_job, interval=5 * 60, first=10)

    # Polling (limpia updates viejos al arrancar)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
