import os
import json
import time
from datetime import datetime, timezone

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
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_user(data, chat_id: str):
    users = data.setdefault("users", {})
    # cooldown_min = tiempo mínimo entre alertas BUY (para no spamear)
    return users.setdefault(chat_id, {"alerts": {}, "cooldown_min": 360})


# ===================== MARKET HELPERS =====================
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


def pnl_pct_from_entry(price: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    return (price - entry) / entry * 100.0


# ===================== COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot activo.\n\n"
        "📉 BUY por caída desde máximo 60d:\n"
        "  /add NVDA 15\n\n"
        "📌 Guardar tu precio de entrada:\n"
        "  /entry NVDA 175\n\n"
        "💰 Reglas de venta (TP / SL):\n"
        "  /setsell NVDA 20 10\n\n"
        "🧠 DCA inteligente (cuánto comprar según caída):\n"
        "  /dca NVDA 10:10 15:20 20:40\n\n"
        "📋 Ver / administrar:\n"
        "  /list\n"
        "  /show NVDA\n"
        "  /remove NVDA\n\n"
        "⚠️ Nota: este bot solo envía alertas. Tú decides comprar/vender."
    )
    await update.message.reply_text(msg)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /add TICKER %
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
    cfg.setdefault("last_buy_ts", 0)

    # SELL defaults
    cfg.setdefault("entry", None)
    cfg.setdefault("tp", 20.0)
    cfg.setdefault("sl", 10.0)
    cfg.setdefault("last_sell_ts", 0)

    # DCA defaults
    cfg.setdefault("dca_tiers", [])
    cfg.setdefault("dca_last_ts", 0)

    save_data(data)

    await update.message.reply_text(f"✅ BUY creado: {ticker} ≥ {pct:.1f}% (desde máximo 60d)")


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /entry NVDA 175
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /entry TICKER PRECIO (ej: /entry NVDA 175)")

    ticker = context.args[0].upper().strip()
    try:
        entry_price = float(context.args[1])
        if entry_price <= 0:
            raise ValueError()
    except Exception:
        return await update.message.reply_text("Precio inválido. Ej: /entry NVDA 175")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    cfg = u["alerts"].setdefault(ticker, {"pct": 10, "last_buy_ts": 0})
    cfg["entry"] = entry_price

    cfg.setdefault("tp", 20.0)
    cfg.setdefault("sl", 10.0)
    cfg.setdefault("last_sell_ts", 0)

    cfg.setdefault("dca_tiers", [])
    cfg.setdefault("dca_last_ts", 0)

    save_data(data)
    await update.message.reply_text(f"📌 Entry guardado {ticker} @ ${entry_price:.2f}")


async def setsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /setsell NVDA 20 10
    if len(context.args) < 3:
        return await update.message.reply_text("Uso: /setsell TICKER TP SL (ej: /setsell NVDA 20 10)")

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

    cfg = u["alerts"].setdefault(ticker, {"pct": 10, "last_buy_ts": 0})
    cfg["tp"] = tp
    cfg["sl"] = sl
    cfg.setdefault("last_sell_ts", 0)

    cfg.setdefault("entry", None)
    cfg.setdefault("dca_tiers", [])
    cfg.setdefault("dca_last_ts", 0)

    save_data(data)
    await update.message.reply_text(f"✅ SELL config: {ticker} TP={tp:.1f}% | SL={sl:.1f}%")


async def dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /dca NVDA 10:10 15:20 20:40  (drop%:usd)
    if len(context.args) < 2:
        return await update.message.reply_text(
            "Uso: /dca TICKER 10:10 15:20 20:40\n"
            "Formato: caída%:montoUSD (ej: /dca NVDA 10:10 15:20 20:40)"
        )

    ticker = context.args[0].upper().strip()
    tiers = []
    try:
        for part in context.args[1:]:
            drop_str, amt_str = part.split(":")
            drop = float(drop_str)
            amt = float(amt_str)
            if drop <= 0 or drop > 80 or amt <= 0:
                raise ValueError()
            tiers.append({"drop": drop, "amt": amt})
        tiers.sort(key=lambda x: x["drop"])
    except Exception:
        return await update.message.reply_text("Formato inválido. Ej: /dca NVDA 10:10 15:20 20:40")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    cfg = u["alerts"].setdefault(ticker, {"pct": 10, "last_buy_ts": 0})

    cfg["dca_tiers"] = tiers
    cfg.setdefault("dca_last_ts", 0)

    cfg.setdefault("entry", None)
    cfg.setdefault("tp", 20.0)
    cfg.setdefault("sl", 10.0)
    cfg.setdefault("last_sell_ts", 0)

    save_data(data)

    lines = [f"✅ DCA activado para {ticker}:"]
    for t in tiers:
        lines.append(f"• ≥{t['drop']}% → ${t['amt']:.0f}")
    await update.message.reply_text("\n".join(lines))


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    alerts = u.get("alerts", {})
    if not alerts:
        return await update.message.reply_text("No tienes alertas. Usa /add NVDA 15")

    lines = ["📌 Tus alertas:"]
    for tkr, cfg in alerts.items():
        entry_val = cfg.get("entry", None)
        entry_txt = f"{entry_val}" if entry_val is not None else "None"
        tiers = cfg.get("dca_tiers", [])
        dca_txt = "ON" if tiers else "OFF"
        lines.append(
            f"• {tkr}: BUY≥{cfg.get('pct')}% | Entry:{entry_txt} | TP:{cfg.get('tp')}% | SL:{cfg.get('sl')}% | DCA:{dca_txt}"
        )
    await update.message.reply_text("\n".join(lines))


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /show TICKER (ej: /show NVDA)")

    ticker = context.args[0].upper().strip()
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    cfg = u.get("alerts", {}).get(ticker)

    if not cfg:
        return await update.message.reply_text("Ese ticker no está. Usa /add TICKER %")

    entry_val = cfg.get("entry", None)
    entry_txt = f"{float(entry_val):.2f}" if entry_val is not None else "None"

    msg = (
        f"{ticker}\n"
        f"BUY ≥ {cfg.get('pct')}%\n"
        f"Entry: {entry_txt}\n"
        f"TP: {cfg.get('tp')}%\n"
        f"SL: {cfg.get('sl')}%\n"
    )

    tiers = cfg.get("dca_tiers", [])
    if tiers:
        msg += "DCA:\n"
        for t in tiers:
            msg += f"  ≥{t['drop']}% → ${t['amt']:.0f}\n"
    else:
        msg += "DCA: OFF\n"

    await update.message.reply_text(msg)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /remove TICKER (ej: /remove NVDA)")

    ticker = context.args[0].upper().strip()
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))

    if ticker in u["alerts"]:
        del u["alerts"][ticker]
        save_data(data)
        return await update.message.reply_text(f"🗑️ Eliminado: {ticker}")

    await update.message.reply_text("Ese ticker no estaba en tu lista. Usa /list")


# ===================== BACKGROUND CHECKER =====================
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

        buy_cooldown_sec = int(u.get("cooldown_min", 360)) * 60
        sell_cooldown_sec = 6 * 60 * 60   # 6 horas para no repetir alertas SELL
        dca_cooldown_sec = 6 * 60 * 60    # 6 horas para no repetir DCA

        for ticker, cfg in list(alerts.items()):
            price, recent_high = await fetch_price_and_recent_high(ticker, lookback_days=60)
            if price is None or recent_high is None:
                continue

            drop = pct_drop_from_high(price, recent_high)

            # -------- BUY (caída desde máximo 60d) --------
            pct_target = float(cfg.get("pct", 10))
            last_buy = int(cfg.get("last_buy_ts", 0))
            if drop >= pct_target and (time.time() - last_buy >= buy_cooldown_sec):
                cfg["last_buy_ts"] = int(time.time())
                data["users"][chat_id]["alerts"][ticker] = cfg
                save_data(data)

                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"📉 ALERTA BUY: {ticker}\n"
                        f"Caída: {drop:.1f}% desde máximo 60d\n"
                        f"Precio aprox: ${price:.2f}\n"
                        f"Máximo 60d: ${recent_high:.2f}"
                    )
                )

            # -------- DCA INTELIGENTE --------
            tiers = cfg.get("dca_tiers", [])
            if tiers:
                last_dca = int(cfg.get("dca_last_ts", 0))
                if time.time() - last_dca >= dca_cooldown_sec:
                    suggested = 0.0
                    for t in tiers:
                        if drop >= float(t["drop"]):
                            suggested = float(t["amt"])
                    if suggested > 0:
                        cfg["dca_last_ts"] = int(time.time())
                        data["users"][chat_id]["alerts"][ticker] = cfg
                        save_data(data)

                        await app.bot.send_message(
                            chat_id=int(chat_id),
                            text=(
                                f"🧠 DCA ALERTA: {ticker}\n"
                                f"Caída: {drop:.1f}% (máximo 60d)\n"
                                f"Precio aprox: ${price:.2f}\n\n"
                                f"👉 Recomendación DCA: comprar ${suggested:.0f}"
                            )
                        )

            # -------- SELL (TP/SL desde entry) --------
            entry_price = cfg.get("entry", None)
            if entry_price is None:
                continue

            try:
                entry_price = float(entry_price)
            except Exception:
                continue

            tp = float(cfg.get("tp", 0) or 0)
            sl = float(cfg.get("sl", 0) or 0)
            last_sell = int(cfg.get("last_sell_ts", 0))

            if time.time() - last_sell < sell_cooldown_sec:
                continue

            pnl = pnl_pct_from_entry(price, entry_price)

            if tp > 0 and pnl >= tp:
                cfg["last_sell_ts"] = int(time.time())
                data["users"][chat_id]["alerts"][ticker] = cfg
                save_data(data)

                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"💰 ALERTA SELL (TAKE PROFIT): {ticker}\n"
                        f"Ganancia: +{pnl:.1f}%\n"
                        f"Precio aprox: ${price:.2f}\n"
                        f"Entry: ${entry_price:.2f} | TP: {tp:.1f}%"
                    )
                )

            elif sl > 0 and pnl <= -sl:
                cfg["last_sell_ts"] = int(time.time())
                data["users"][chat_id]["alerts"][ticker] = cfg
                save_data(data)

                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"🛑 ALERTA SELL (STOP LOSS): {ticker}\n"
                        f"Pérdida: {pnl:.1f}%\n"
                        f"Precio aprox: ${price:.2f}\n"
                        f"Entry: ${entry_price:.2f} | SL: {sl:.1f}%"
                    )
                )


# ===================== MAIN =====================
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno (Render).")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("entry", entry))
    app.add_handler(CommandHandler("setsell", setsell))
    app.add_handler(CommandHandler("dca", dca))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("show", show))
    app.add_handler(CommandHandler("remove", remove))

    # ✅ Revisión automática cada 5 minutos (primera revisión a los 10 segundos)
    app.job_queue.run_repeating(check_job, interval=300, first=10)

    # ✅ Importante para evitar conflictos en Render
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
