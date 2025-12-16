import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

import yfinance as yf

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# ENV VARS (Render)
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# =========================
# DB HELPERS
# =========================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("Falta DATABASE_URL en Render (Environment Variables).")
    # sslmode=require funciona bien en Render Postgres
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, sslmode="require")


def db_init():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
                drop_pct NUMERIC,          -- /add TICKER 10
                entry_price NUMERIC,       -- /entry TICKER 175
                tp_pct NUMERIC,            -- /setsell TICKER 20 10
                sl_pct NUMERIC,
                dca_rules JSONB,           -- /dca TICKER 10:15 15:25 ...
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(telegram_id, ticker)
            );
            """)

            # anti-spam columns
            cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_buy_alert_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_tp_alert_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_sl_alert_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_buy_drop_sent NUMERIC;")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                weekly_budget NUMERIC DEFAULT 0,
                dips_budget NUMERIC DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
                amount NUMERIC NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            conn.commit()


def upsert_user(update: Update):
    tg_id = update.effective_user.id
    username = update.effective_user.username
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO users (telegram_id, username)
            VALUES (%s, %s)
            ON CONFLICT (telegram_id)
            DO UPDATE SET username = EXCLUDED.username;
            """, (tg_id, username))
            conn.commit()


def normalize_ticker(t: str) -> str:
    return (t or "").strip().upper()


def now_utc():
    return datetime.now(timezone.utc)


# =========================
# PRICE HELPERS (yfinance)
# =========================
def fetch_price_and_60d_high(ticker: str):
    """
    Returns: (current_price, high_60d)
    Uses yfinance history. (Simple + reliable)
    """
    tk = yf.Ticker(ticker)

    # last ~3 months is enough to compute 60 trading days
    hist = tk.history(period="3mo", interval="1d")
    if hist is None or hist.empty:
        return None, None

    # current price: last Close
    current = float(hist["Close"].iloc[-1])

    # 60-day high: max of last ~60 rows (if fewer, use all)
    tail = hist.tail(60)
    high_60d = float(tail["High"].max())

    return current, high_60d


# =========================
# FORMATTERS
# =========================
def fmt_money(x):
    if x is None:
        return "None"
    try:
        return f"{float(x):,.2f}"
    except:
        return str(x)


def fmt_pct(x):
    if x is None:
        return "None"
    try:
        return f"{float(x):.1f}%"
    except:
        return str(x)


def parse_dca_rules(parts):
    """
    Input: ["10:15","15:25","20:40"] -> list of dicts sorted by drop asc
    Each is {"drop":10.0, "amount":15.0}
    """
    rules = []
    for p in parts:
        p = p.strip()
        if not p or ":" not in p:
            continue
        a, b = p.split(":", 1)
        drop = float(a)
        amt = float(b)
        rules.append({"drop": drop, "amount": amt})
    rules.sort(key=lambda r: r["drop"])
    return rules


def dca_suggest_amount(dca_rules, drop_pct, dips_budget):
    """
    If drop >= rule.drop => candidate amount
    Return the highest matched rule amount, capped by dips_budget (if dips_budget>0)
    """
    if not dca_rules or drop_pct is None:
        return None
    best = None
    for r in dca_rules:
        if drop_pct >= float(r["drop"]):
            best = float(r["amount"])
    if best is None:
        return None
    if dips_budget is not None and float(dips_budget) > 0:
        return float(min(best, float(dips_budget)))
    return best


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM alerts WHERE telegram_id=%s ORDER BY ticker;", (tg_id,))
            tickers = [r["ticker"] for r in cur.fetchall()]

    if tickers:
        tracked = ", ".join(tickers)
    else:
        tracked = "(ninguno todavía)"

    msg = (
        "✅ Bot activo.\n\n"
        f"📌 Tickers en seguimiento: {tracked}\n\n"
        "📌 Ver todo: /list\n"
        "📌 Ver detalle: /show TICKER\n\n"
        "🟦 BUY por caída desde máximo 60d:\n"
        "  /add QQQ 10\n\n"
        "📍 Guardar precio de entrada:\n"
        "  /entry QQQ 450\n\n"
        "🧾 Reglas TP/SL (solo alerta, NO opera):\n"
        "  /setsell QQQ 10 7\n\n"
        "🧠 DCA inteligente (cuánto comprar según caída):\n"
        "  /dca QQQ 10:15 15:25 20:40\n\n"
        "💰 Presupuesto:\n"
        "  /setbudget 70 40   (semanal / dips)\n"
        "  /plan QQQ 30 SCHD 20 JEPQ 20  (plan fijo lunes)\n"
        "  /monday  (ver plan)\n\n"
        "⚠️ Nota: este bot solo envía alertas/sugerencias. Tú decides comprar/vender."
    )
    await update.message.reply_text(msg)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /add TICKER DROP%\nEj: /add QQQ 10")

    ticker = normalize_ticker(context.args[0])
    drop = float(context.args[1])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO alerts (telegram_id, ticker, drop_pct)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id, ticker)
            DO UPDATE SET drop_pct = EXCLUDED.drop_pct;
            """, (tg_id, ticker, drop))
            conn.commit()

    await update.message.reply_text(f"✅ BUY creado: {ticker} ≥ {drop:.1f}% (caída desde máximo 60d)")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /remove TICKER\nEj: /remove QQQ")

    ticker = normalize_ticker(context.args[0])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alerts WHERE telegram_id=%s AND ticker=%s;", (tg_id, ticker))
            conn.commit()

    await update.message.reply_text(f"🗑️ Eliminado: {ticker}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT ticker, drop_pct
            FROM alerts
            WHERE telegram_id=%s
            ORDER BY ticker;
            """, (tg_id,))
            rows = cur.fetchall()

    if not rows:
        return await update.message.reply_text("No tienes alertas. Usa /add TICKER %  (ej: /add QQQ 10)")

    lines = ["📌 Tus alertas:"]
    for r in rows:
        lines.append(f"• {r['ticker']}: caída ≥ {fmt_pct(r['drop_pct'])}")
    await update.message.reply_text("\n".join(lines))


async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /show TICKER\nEj: /show QQQ")

    ticker = normalize_ticker(context.args[0])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT ticker, drop_pct, entry_price, tp_pct, sl_pct, dca_rules
            FROM alerts
            WHERE telegram_id=%s AND ticker=%s;
            """, (tg_id, ticker))
            a = cur.fetchone()

    if not a:
        return await update.message.reply_text(f"No encuentro {ticker}. Crea primero con /add {ticker} 10")

    dca = a["dca_rules"] or []
    dca_lines = []
    if dca:
        dca_lines.append("DCA:")
        for r in dca:
            dca_lines.append(f"≥{r['drop']}% → ${fmt_money(r['amount'])}")
    else:
        dca_lines.append("DCA: (none)")

    msg = (
        f"{ticker}\n"
        f"BUY ≥ {fmt_pct(a['drop_pct'])}\n"
        f"Entry: {fmt_money(a['entry_price'])}\n"
        f"TP: {fmt_pct(a['tp_pct'])}\n"
        f"SL: {fmt_pct(a['sl_pct'])}\n"
        + "\n".join(dca_lines)
    )
    await update.message.reply_text(msg)


async def cmd_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /entry TICKER PRICE\nEj: /entry QQQ 450")

    ticker = normalize_ticker(context.args[0])
    price = float(context.args[1])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO alerts (telegram_id, ticker, entry_price)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id, ticker)
            DO UPDATE SET entry_price = EXCLUDED.entry_price;
            """, (tg_id, ticker, price))
            conn.commit()

    await update.message.reply_text(f"📌 Entry guardado {ticker} @ ${price:.2f}")


async def cmd_setsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 3:
        return await update.message.reply_text("Uso: /setsell TICKER TP% SL%\nEj: /setsell QQQ 10 7")

    ticker = normalize_ticker(context.args[0])
    tp = float(context.args[1])
    sl = float(context.args[2])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO alerts (telegram_id, ticker, tp_pct, sl_pct)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_id, ticker)
            DO UPDATE SET tp_pct = EXCLUDED.tp_pct, sl_pct = EXCLUDED.sl_pct;
            """, (tg_id, ticker, tp, sl))
            conn.commit()

    await update.message.reply_text(f"🧾 {ticker} TP={tp:.1f}% | SL={sl:.1f}% (solo alerta)")


async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /dca TICKER 10:15 15:25 20:40\nEj: /dca QQQ 10:15 15:25")

    ticker = normalize_ticker(context.args[0])
    rules = parse_dca_rules(context.args[1:])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO alerts (telegram_id, ticker, dca_rules)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (telegram_id, ticker)
            DO UPDATE SET dca_rules = EXCLUDED.dca_rules;
            """, (tg_id, ticker, json.dumps(rules)))
            conn.commit()

    await update.message.reply_text(f"🧠 DCA guardado para {ticker}: {', '.join(context.args[1:])}")


async def cmd_setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /setbudget WEEKLY DIPS\nEj: /setbudget 70 40")

    weekly = float(context.args[0])
    dips = float(context.args[1])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO budgets (telegram_id, weekly_budget, dips_budget, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (telegram_id)
            DO UPDATE SET weekly_budget=EXCLUDED.weekly_budget, dips_budget=EXCLUDED.dips_budget, updated_at=NOW();
            """, (tg_id, weekly, dips))
            conn.commit()

    await update.message.reply_text(f"💰 Budget guardado: semanal=${weekly:.2f} | dips=${dips:.2f}")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    if len(context.args) < 2 or len(context.args) % 2 != 0:
        return await update.message.reply_text("Uso: /plan TICKER AMOUNT TICKER AMOUNT...\nEj: /plan QQQ 30 SCHD 20 JEPQ 20")

    pairs = []
    for i in range(0, len(context.args), 2):
        t = normalize_ticker(context.args[i])
        amt = float(context.args[i+1])
        pairs.append((t, amt))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM plans WHERE telegram_id=%s;", (tg_id,))
            for t, amt in pairs:
                cur.execute("""
                INSERT INTO plans (telegram_id, ticker, amount)
                VALUES (%s, %s, %s);
                """, (tg_id, t, amt))
            conn.commit()

    pretty = " | ".join([f"{t} ${amt:.2f}" for t, amt in pairs])
    await update.message.reply_text(f"📅 Plan lunes guardado: {pretty}")


async def cmd_monday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    tg_id = update.effective_user.id

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT weekly_budget, dips_budget FROM budgets WHERE telegram_id=%s;", (tg_id,))
            b = cur.fetchone()
            cur.execute("SELECT ticker, amount FROM plans WHERE telegram_id=%s ORDER BY ticker;", (tg_id,))
            p = cur.fetchall()

    weekly = float(b["weekly_budget"]) if b else 0.0
    dips = float(b["dips_budget"]) if b else 0.0

    if not p:
        return await update.message.reply_text("No tienes plan. Crea uno con /plan QQQ 30 SCHD 20 JEPQ 20")

    total = sum(float(x["amount"]) for x in p)
    lines = [
        "📅 Plan de compra (Lunes):",
        *[f"• {x['ticker']}: ${float(x['amount']):.2f}" for x in p],
        f"\nTotal plan: ${total:.2f}",
        f"Budget semanal: ${weekly:.2f}",
        f"Budget dips: ${dips:.2f}",
    ]
    if weekly > 0 and total > weekly + 1e-9:
        lines.append("⚠️ Tu plan está por encima del budget semanal. Ajusta /plan o /setbudget.")
    await update.message.reply_text("\n".join(lines))


# =========================
# BACKGROUND CHECKER
# =========================
BUY_COOLDOWN = timedelta(hours=6)
TP_SL_COOLDOWN = timedelta(hours=3)

async def check_jobs(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs periodically. Checks all alerts for all users.
    Sends:
      - BUY alert when drop >= drop_pct
      - TP/SL alerts when entry_price defined and target hit
      - DCA suggestion (if rules exist)
    """
    app = context.application
    now = now_utc()

    # Load budgets into dict
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id, weekly_budget, dips_budget FROM budgets;")
            budget_rows = cur.fetchall() or []
            budgets = {r["telegram_id"]: r for r in budget_rows}

            cur.execute("""
            SELECT
              id, telegram_id, ticker, drop_pct, entry_price, tp_pct, sl_pct, dca_rules,
              last_buy_alert_at, last_tp_alert_at, last_sl_alert_at, last_buy_drop_sent
            FROM alerts;
            """)
            alerts = cur.fetchall() or []

    # Group by ticker to reduce yfinance calls
    tickers = sorted({a["ticker"] for a in alerts})
    prices = {}

    # fetch prices in thread to avoid blocking event loop too hard
    def fetch_all():
        out = {}
        for t in tickers:
            curp, high60 = fetch_price_and_60d_high(t)
            out[t] = (curp, high60)
        return out

    loop = asyncio.get_running_loop()
    prices = await loop.run_in_executor(None, fetch_all)

    # Process each alert
    for a in alerts:
        tg_id = a["telegram_id"]
        ticker = a["ticker"]
        current, high60 = prices.get(ticker, (None, None))
        if current is None or high60 is None or high60 <= 0:
            continue

        drop_pct = (high60 - current) / high60 * 100.0

        dips_budget = float(budgets.get(tg_id, {}).get("dips_budget", 0) or 0)

        # ---------- BUY drop alert ----------
        if a["drop_pct"] is not None:
            threshold = float(a["drop_pct"])
            last_at = a["last_buy_alert_at"]
            last_sent_drop = a["last_buy_drop_sent"]

            cooldown_ok = (last_at is None) or ((now - last_at) >= BUY_COOLDOWN)
            deeper_drop = (last_sent_drop is None) or (drop_pct >= float(last_sent_drop) + 2.0)

            if drop_pct >= threshold and (cooldown_ok or deeper_drop):
                # DCA suggestion
                dca_rules = a["dca_rules"] or []
                suggested = dca_suggest_amount(dca_rules, drop_pct, dips_budget)

                msg = (
                    f"📉 ALERTA: {ticker}\n"
                    f"Caída: {drop_pct:.1f}% desde el máximo 60d\n"
                    f"Precio aprox: ${current:.2f}\n"
                    f"Máximo 60d: ${high60:.2f}\n"
                )
                if suggested:
                    msg += f"\n🧠 DCA sugerido (según tu regla): ${suggested:.2f}"
                else:
                    msg += "\n🧠 DCA: (sin regla o no aplica todavía)"

                msg += "\n\n👉 Si vas a comprar, dime cuánto quieres meter y te digo cómo repartirlo."

                try:
                    await app.bot.send_message(chat_id=tg_id, text=msg)
                except:
                    pass

                # update spam control
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                        UPDATE alerts
                        SET last_buy_alert_at=%s, last_buy_drop_sent=%s
                        WHERE id=%s;
                        """, (now, drop_pct, a["id"]))
                        conn.commit()

        # ---------- TP/SL alerts ----------
        entry = a["entry_price"]
        if entry is not None and (a["tp_pct"] is not None or a["sl_pct"] is not None):
            entry = float(entry)
            if entry > 0:
                # TP
                if a["tp_pct"] is not None:
                    tp = float(a["tp_pct"])
                    tp_price = entry * (1.0 + tp / 100.0)
                    last_tp = a["last_tp_alert_at"]
                    if current >= tp_price and ((last_tp is None) or ((now - last_tp) >= TP_SL_COOLDOWN)):
                        msg = (
                            f"✅ TP ALERTA: {ticker}\n"
                            f"Entry: ${entry:.2f}\n"
                            f"TP: {tp:.1f}% → objetivo ${tp_price:.2f}\n"
                            f"Precio actual: ${current:.2f}\n\n"
                            "⚠️ Solo alerta. Tú decides vender."
                        )
                        try:
                            await app.bot.send_message(chat_id=tg_id, text=msg)
                        except:
                            pass
                        with db_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE alerts SET last_tp_alert_at=%s WHERE id=%s;", (now, a["id"]))
                                conn.commit()

                # SL
                if a["sl_pct"] is not None:
                    sl = float(a["sl_pct"])
                    sl_price = entry * (1.0 - sl / 100.0)
                    last_sl = a["last_sl_alert_at"]
                    if current <= sl_price and ((last_sl is None) or ((now - last_sl) >= TP_SL_COOLDOWN)):
                        msg = (
                            f"🛑 SL ALERTA: {ticker}\n"
                            f"Entry: ${entry:.2f}\n"
                            f"SL: {sl:.1f}% → nivel ${sl_price:.2f}\n"
                            f"Precio actual: ${current:.2f}\n\n"
                            "⚠️ Solo alerta. Tú decides qué hacer."
                        )
                        try:
                            await app.bot.send_message(chat_id=tg_id, text=msg)
                        except:
                            pass
                        with db_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE alerts SET last_sl_alert_at=%s WHERE id=%s;", (now, a["id"]))
                                conn.commit()


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Render (Environment Variables).")

    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("setsell", cmd_setsell))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("setbudget", cmd_setbudget))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("monday", cmd_monday))

    # Job queue: cada 5 minutos (puedes cambiar a 300, 600, etc.)
    app.job_queue.run_repeating(check_jobs, interval=300, first=15)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
