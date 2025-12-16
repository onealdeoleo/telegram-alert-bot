import os
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

import pandas as pd
import yfinance as yf

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DATA_FILE = "alerts.json"


# ----------------------------
# Storage helpers
# ----------------------------
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_ts() -> int:
    return int(time.time())


def monday_of_week(dt: datetime) -> datetime:
    start = dt - timedelta(days=dt.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def get_user(data: Dict[str, Any], chat_id: str) -> Dict[str, Any]:
    users = data.setdefault("users", {})
    u = users.setdefault(chat_id, {})
    u.setdefault("alerts", {})              # ticker -> cfg
    u.setdefault("cooldown_min", 360)       # min cooldown per ticker alert
    u.setdefault("weekly_budget", 70)       # fixed Monday money (manual)
    u.setdefault("dip_budget", 40)          # extra dips money (smart)
    u.setdefault("weekly_plan", {})         # ticker -> dollars (Monday plan)
    u.setdefault("week_state", {"week_monday_ts": 0, "dip_spent": 0, "weekly_spent": 0})
    u.setdefault("trial", {"start_ts": 0, "days": 7})
    u.setdefault("premium", {"enabled": False, "until_ts": 0})
    return u


# ----------------------------
# Premium / Trial
# ----------------------------
def is_admin(chat_id: str) -> bool:
    raw = os.getenv("ADMIN_CHAT_IDS", "").strip()
    if not raw:
        return False
    admins = {x.strip() for x in raw.split(",") if x.strip()}
    return chat_id in admins


def premium_active(u: Dict[str, Any]) -> bool:
    prem = u.get("premium", {})
    if prem.get("enabled") and int(prem.get("until_ts", 0) or 0) > now_ts():
        return True

    trial = u.get("trial", {"start_ts": 0, "days": 7})
    start = int(trial.get("start_ts", 0) or 0)
    days = int(trial.get("days", 7) or 7)
    if start <= 0:
        return False
    return now_ts() < start + days * 86400


def ensure_trial(u: Dict[str, Any]) -> None:
    trial = u.get("trial", {})
    if int(trial.get("start_ts", 0) or 0) <= 0:
        trial["start_ts"] = now_ts()
        trial.setdefault("days", 7)
        u["trial"] = trial


def free_limits_ok(u: Dict[str, Any]) -> Tuple[bool, str]:
    # FREE: max 2 tickers
    if premium_active(u):
        return True, ""
    if len(u.get("alerts", {})) >= 2:
        return False, "⚠️ En FREE solo puedes tener 2 tickers. Para más: /premium"
    return True, ""


# ----------------------------
# Week reset (IMPORTANT)
# ----------------------------
def ensure_week_reset(u: Dict[str, Any]) -> None:
    ws = u.get("week_state", {"week_monday_ts": 0, "dip_spent": 0, "weekly_spent": 0})
    current_monday = int(monday_of_week(datetime.now(timezone.utc)).timestamp())

    if int(ws.get("week_monday_ts", 0) or 0) != current_monday:
        ws["week_monday_ts"] = current_monday
        ws["dip_spent"] = 0
        ws["weekly_spent"] = 0

    ws.setdefault("dip_spent", 0)
    ws.setdefault("weekly_spent", 0)
    u["week_state"] = ws


# ----------------------------
# Market data + indicators
# ----------------------------
def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def compute_rsi(series: pd.Series, period: int = 14) -> Optional[float]:
    if series is None or len(series) < period + 2:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return None if pd.isna(val) else float(val)


@dataclass
class Snapshot:
    price: float
    recent_high: float
    drop_pct: float
    sma50: Optional[float]
    sma200: Optional[float]
    rsi14: Optional[float]
    vol_ratio: Optional[float]  # last vol / avg20 vol


def fetch_snapshot(ticker: str, lookback_days: int = 220) -> Optional[Snapshot]:
    t = yf.Ticker(ticker)
    hist = t.history(period=f"{lookback_days}d", interval="1d")
    if hist is None or hist.empty:
        return None

    close = hist["Close"].dropna()
    if close.empty:
        return None

    price = float(close.iloc[-1])
    recent = hist.tail(60)["Close"].dropna()
    recent_high = float(recent.max()) if not recent.empty else float(close.max())
    drop_pct = ((recent_high - price) / recent_high * 100.0) if recent_high > 0 else 0.0

    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    rsi14 = compute_rsi(close, 14)

    vol = hist["Volume"].dropna()
    vol_ratio = None
    if len(vol) >= 21:
        avg20 = float(vol.tail(21).head(20).mean())
        lastv = float(vol.iloc[-1])
        if avg20 > 0:
            vol_ratio = lastv / avg20

    return Snapshot(
        price=price,
        recent_high=recent_high,
        drop_pct=drop_pct,
        sma50=sma50,
        sma200=sma200,
        rsi14=rsi14,
        vol_ratio=vol_ratio,
    )


def opportunity_score(s: Snapshot) -> float:
    score = 0.0

    # drop (0..25%) -> up to 4 pts
    score += min(max(s.drop_pct, 0.0), 25.0) / 25.0 * 4.0

    # RSI
    if s.rsi14 is not None:
        if s.rsi14 <= 30:
            score += 2.5
        elif s.rsi14 <= 40:
            score += 1.8
        elif s.rsi14 <= 50:
            score += 1.0

    # SMA50
    if s.sma50 is not None and s.sma50 > 0:
        score += 1.5 if s.price < s.sma50 else 0.5

    # Volume ratio
    if s.vol_ratio is not None:
        if s.vol_ratio >= 1.5:
            score += 1.2
        elif s.vol_ratio >= 1.1:
            score += 0.7

    # SMA200 (trend)
    if s.sma200 is not None and s.sma200 > 0:
        score -= 0.8 if s.price < s.sma200 else -0.4  # (below penalize, above bonus)

    return float(max(0.0, min(10.0, score)))


# ----------------------------
# DCA parsing
# ----------------------------
def parse_dca(dca_str: str) -> List[Tuple[float, float]]:
    tiers: List[Tuple[float, float]] = []
    if not dca_str:
        return tiers
    for p in dca_str.split():
        if ":" not in p:
            continue
        a, b = p.split(":", 1)
        drop = safe_float(a)
        amt = safe_float(b)
        if drop is None or amt is None:
            continue
        if drop <= 0 or amt <= 0:
            continue
        tiers.append((drop, amt))
    tiers.sort(key=lambda x: x[0])
    return tiers


def dca_suggestion(drop_pct: float, tiers: List[Tuple[float, float]]) -> Optional[float]:
    amt = None
    for d, a in tiers:
        if drop_pct >= d:
            amt = a
    return amt


# ----------------------------
# Commands
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    ensure_week_reset(u)
    save_data(data)

    msg = (
        "✅ Bot activo.\n\n"
        "📌 Ver todo: /list\n"
        "📌 Ver detalle: /show TICKER\n\n"
        "🟦 BUY por caída desde máximo 60d:\n"
        "  /add QQQ 10\n\n"
        "📍 Guardar precio de entrada:\n"
        "  /entry QQQ 450\n\n"
        "🧾 Reglas TP/SL (solo alerta, NO opera):\n"
        "  /setsell QQQ 10 7\n\n"
        "🧠 DCA inteligente:\n"
        "  /dca QQQ 10:15 15:25\n\n"
        "💰 Presupuesto:\n"
        "  /setbudget 70 40\n"
        "  /plan QQQ 30 SCHD 20 JEPQ 20\n"
        "  /monday\n\n"
        "✅ Confirmar compras (para contar presupuesto):\n"
        "  /bought dips NVDA 15\n"
        "  /bought weekly QQQ 30\n"
        "  /bought status\n"
        "  /bought reset\n\n"
        "📊 Score (Premium/Trial):\n"
        "  /score QQQ\n\n"
        "📦 Copy strategies (Premium/Trial):\n"
        "  /copy aggressive | income | conservative\n\n"
        "💎 Premium/Trial:\n"
        "  /premium\n\n"
        "⚠️ Nota: este bot solo envía alertas/sugerencias. Tú decides comprar/vender."
    )

    alerts = u.get("alerts", {})
    ws = u.get("week_state", {})
    weekly_budget = float(u.get("weekly_budget", 70))
    dip_budget = float(u.get("dip_budget", 40))
    weekly_spent = float(ws.get("weekly_spent", 0))
    dip_spent = float(ws.get("dip_spent", 0))

    if alerts:
        lines = ["\n\n📍 Tus tickers activos ahora:"]
        for tkr, cfg in alerts.items():
            buy = cfg.get("buy_drop_pct", None)
            entryv = cfg.get("entry", None)
            tp = cfg.get("tp", None)
            sl = cfg.get("sl", None)
            dca_str = cfg.get("dca", "")
            dca_on = "ON" if dca_str else "OFF"
            lines.append(
                f"• {tkr} | BUY≥{buy}% | Entry:{entryv} | TP:{tp}% SL:{sl}% | DCA:{dca_on}"
            )
        lines.append(f"\n💰 Weekly usado: ${weekly_spent:.0f}/${weekly_budget:.0f}")
        lines.append(f"💰 Dips usado: ${dip_spent:.0f}/${dip_budget:.0f}")
        msg += "\n".join(lines)
    else:
        msg += "\n\n📍 No tienes tickers activos aún. Ej: /add QQQ 10"

    await update.message.reply_text(msg)


async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    save_data(data)

    if premium_active(u):
        prem = u.get("premium", {})
        until = int(prem.get("until_ts", 0) or 0)
        if until > now_ts():
            dt = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            await update.message.reply_text(f"💎 Premium activo hasta: {dt}")
        else:
            await update.message.reply_text("🆓 Estás en TRIAL/Premium activo.")
    else:
        await update.message.reply_text("🆓 Estás en FREE. Límite 2 tickers. (Trial puede expirar).")


async def grantpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not is_admin(chat_id):
        return await update.message.reply_text("⛔ Solo admin.")
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /grantpremium CHAT_ID DAYS")

    target = context.args[0].strip()
    days = safe_float(context.args[1])
    if days is None or days <= 0:
        return await update.message.reply_text("DAYS inválido.")

    data = load_data()
    u = get_user(data, target)
    u["premium"] = {"enabled": True, "until_ts": now_ts() + int(days * 86400)}
    save_data(data)
    await update.message.reply_text(f"✅ Premium activado para {target} por {int(days)} días.")


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /add TICKER %   (ej: /add QQQ 10)")
    ticker = context.args[0].upper().strip()
    pct = safe_float(context.args[1])
    if pct is None or pct <= 0 or pct > 80:
        return await update.message.reply_text("El % debe ser válido (ej: 10, 15).")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    ok, msg = free_limits_ok(u)
    if not ok:
        return await update.message.reply_text(msg)

    cfg = u["alerts"].setdefault(ticker, {})
    cfg.setdefault("entry", None)
    cfg.setdefault("tp", 0)
    cfg.setdefault("sl", 0)
    cfg.setdefault("dca", "")
    cfg.setdefault("last_sent_ts", 0)
    cfg.setdefault("last_dca_sent_week", 0)

    cfg["buy_drop_pct"] = float(pct)
    save_data(data)
    await update.message.reply_text(f"✅ BUY creado: {ticker} caída ≥ {pct:.1f}% (desde máximo 60d).")


async def setsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        return await update.message.reply_text("Uso: /setsell TICKER TP% SL%   (ej: /setsell QQQ 10 7)")
    ticker = context.args[0].upper().strip()
    tp = safe_float(context.args[1])
    sl = safe_float(context.args[2])
    if tp is None or sl is None or tp <= 0 or sl <= 0:
        return await update.message.reply_text("TP y SL deben ser números > 0.")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    cfg = u["alerts"].setdefault(ticker, {})
    cfg["tp"] = float(tp)
    cfg["sl"] = float(sl)
    save_data(data)
    await update.message.reply_text(f"✅ Reglas guardadas {ticker}: TP {tp:.1f}% / SL {sl:.1f}%.")


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /entry TICKER PRICE   (ej: /entry NVDA 175)")
    ticker = context.args[0].upper().strip()
    price = safe_float(context.args[1])
    if price is None or price <= 0:
        return await update.message.reply_text("PRICE inválido.")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    cfg = u["alerts"].setdefault(ticker, {})
    cfg["entry"] = float(price)
    save_data(data)
    await update.message.reply_text(f"📌 Entry guardado {ticker} @ ${price:.2f}")


async def dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /dca TICKER 10:15 15:25 ...")
    ticker = context.args[0].upper().strip()
    dca_str = " ".join(context.args[1:]).strip()
    tiers = parse_dca(dca_str)
    if not tiers:
        return await update.message.reply_text("Formato inválido. Ej: /dca QQQ 10:15 15:25")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    cfg = u["alerts"].setdefault(ticker, {})
    cfg["dca"] = dca_str
    save_data(data)
    await update.message.reply_text(f"🧠 DCA guardado {ticker}: {dca_str}")


async def setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /setbudget WEEKLY DIP   (ej: /setbudget 70 40)")
    weekly = safe_float(context.args[0])
    dip = safe_float(context.args[1])
    if weekly is None or dip is None or weekly <= 0 or dip < 0:
        return await update.message.reply_text("Valores inválidos.")

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    u["weekly_budget"] = float(weekly)
    u["dip_budget"] = float(dip)
    ensure_week_reset(u)
    save_data(data)
    await update.message.reply_text(f"💰 Presupuesto guardado: ${weekly:.0f}/semana + ${dip:.0f} dips.")


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or len(context.args) % 2 != 0:
        return await update.message.reply_text("Uso: /plan TICKER AMT TICKER AMT ...")
    pairs = context.args
    plan_map: Dict[str, float] = {}
    for i in range(0, len(pairs), 2):
        tkr = pairs[i].upper().strip()
        amt = safe_float(pairs[i + 1])
        if amt is None or amt <= 0:
            return await update.message.reply_text(f"Monto inválido para {tkr}")
        plan_map[tkr] = float(amt)

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    u["weekly_plan"] = plan_map
    save_data(data)
    await update.message.reply_text("✅ Plan lunes guardado. Usa /monday para verlo.")


async def monday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    ensure_week_reset(u)
    save_data(data)

    plan_map = u.get("weekly_plan", {})
    weekly = float(u.get("weekly_budget", 70))
    dip = float(u.get("dip_budget", 40))

    if not plan_map:
        return await update.message.reply_text("No tienes plan. Ej: /plan QQQ 30 SCHD 20 JEPQ 20")

    total = sum(plan_map.values())
    lines = ["📆 Plan fijo de lunes (manual):"]
    for tkr, amt in plan_map.items():
        lines.append(f"• {tkr}: ${amt:.0f}")
    lines.append(f"\nTotal plan: ${total:.0f} (tu semanal: ${weekly:.0f})")
    if abs(total - weekly) > 0.01:
        lines.append("⚠️ Tu plan no suma exactamente tu presupuesto semanal.")
    lines.append(f"\n📉 Dips (DCA inteligente): ${dip:.0f} por semana (solo si hay caída).")
    await update.message.reply_text("\n".join(lines))


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    ensure_week_reset(u)
    save_data(data)

    alerts = u.get("alerts", {})
    if not alerts:
        return await update.message.reply_text("No tienes alertas. Usa /add QQQ 10")

    ws = u.get("week_state", {})
    dip_budget = float(u.get("dip_budget", 40))
    weekly_budget = float(u.get("weekly_budget", 70))
    dip_spent = float(ws.get("dip_spent", 0))
    weekly_spent = float(ws.get("weekly_spent", 0))

    lines = ["📌 Tus alertas:"]
    for tkr, cfg in alerts.items():
        buy = cfg.get("buy_drop_pct", None)
        entryv = cfg.get("entry", None)
        tp = cfg.get("tp", 0)
        sl = cfg.get("sl", 0)
        dca_str = cfg.get("dca", "")
        lines.append(f"\n🔹 {tkr}")
        if buy:
            lines.append(f"  BUY ≥ {buy}%")
        lines.append(f"  Entry: {entryv}")
        if tp and sl:
            lines.append(f"  TP: {tp}% / SL: {sl}%")
        lines.append(f"  DCA: {dca_str if dca_str else 'OFF'}")

    lines.append(f"\n💰 Weekly usado: ${weekly_spent:.0f}/${weekly_budget:.0f}")
    lines.append(f"💰 Dips usado: ${dip_spent:.0f}/${dip_budget:.0f}")
    await update.message.reply_text("\n".join(lines))


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /show TICKER")
    ticker = context.args[0].upper().strip()

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    ensure_week_reset(u)
    save_data(data)

    cfg = u.get("alerts", {}).get(ticker)
    if not cfg:
        return await update.message.reply_text("Ese ticker no está configurado. Usa /add TICKER %")

    lines = [ticker]
    lines.append(f"BUY ≥ {cfg.get('buy_drop_pct', 'None')}%")
    lines.append(f"Entry: {cfg.get('entry', 'None')}")
    lines.append(f"TP: {cfg.get('tp', 'None')}%")
    lines.append(f"SL: {cfg.get('sl', 'None')}%")

    dca_str = cfg.get("dca", "")
    if dca_str:
        lines.append("DCA:")
        for d, a in parse_dca(dca_str):
            lines.append(f"  ≥{d:.1f}% → ${a:.0f}")
    else:
        lines.append("DCA: OFF")

    await update.message.reply_text("\n".join(lines))


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /remove TICKER")
    ticker = context.args[0].upper().strip()

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    if ticker in u["alerts"]:
        del u["alerts"][ticker]
        save_data(data)
        return await update.message.reply_text(f"🗑️ Eliminado: {ticker}")

    await update.message.reply_text("Ese ticker no estaba en tu lista.")


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /score TICKER")
    ticker = context.args[0].upper().strip()

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    save_data(data)

    if not premium_active(u):
        return await update.message.reply_text("🔒 Score es Premium/Trial. Usa /premium")

    s = fetch_snapshot(ticker)
    if not s:
        return await update.message.reply_text("No pude obtener datos ahora. Intenta luego.")

    sc = opportunity_score(s)
    lines = [
        f"📊 {ticker} Opportunity Score: {sc:.1f}/10",
        f"Precio: ${s.price:.2f}",
        f"Caída desde máx 60d: {s.drop_pct:.1f}% (máx: ${s.recent_high:.2f})",
    ]
    if s.rsi14 is not None:
        lines.append(f"RSI(14): {s.rsi14:.1f}")
    if s.sma50 is not None:
        lines.append(f"SMA50: {s.sma50:.2f}")
    if s.sma200 is not None:
        lines.append(f"SMA200: {s.sma200:.2f}")
    if s.vol_ratio is not None:
        lines.append(f"Vol vs avg20: {s.vol_ratio:.2f}x")

    await update.message.reply_text("\n".join(lines))


async def cashflow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    save_data(data)

    if not premium_active(u):
        return await update.message.reply_text("🔒 Cashflow planner es Premium/Trial. Usa /premium")

    tickers = [a.upper().strip() for a in context.args] if context.args else ["JEPQ", "SCHD"]

    lines = ["💵 Cash Flow Planner (estimado por $1,000 invertidos)"]
    for tkr in tickers:
        t = yf.Ticker(tkr)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}

        dy = safe_float(info.get("dividendYield", None))
        if dy is None or dy <= 0:
            lines.append(f"\n• {tkr}: (sin datos de dividendos ahora)")
            continue

        annual_cash = 1000.0 * dy
        monthly_cash = annual_cash / 12.0
        lines.append(
            f"\n• {tkr}: yield aprox {dy*100:.2f}%"
            f"\n  ≈ ${monthly_cash:.2f}/mes por cada $1,000"
            f"\n  ≈ ${annual_cash:.2f}/año por cada $1,000"
        )

    await update.message.reply_text("\n".join(lines))


async def copy_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /copy aggressive | income | conservative")

    name = context.args[0].lower().strip()

    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)

    if not premium_active(u):
        return await update.message.reply_text("🔒 Copy strategies es Premium/Trial. Usa /premium")

    if name == "aggressive":
        u["alerts"]["QQQ"] = {"buy_drop_pct": 10, "entry": None, "tp": 10, "sl": 7, "dca": "10:15 15:25", "last_sent_ts": 0, "last_dca_sent_week": 0}
        u["alerts"]["NVDA"] = {"buy_drop_pct": 15, "entry": None, "tp": 20, "sl": 10, "dca": "10:10 15:15 20:15", "last_sent_ts": 0, "last_dca_sent_week": 0}
        u["alerts"]["MSFT"] = {"buy_drop_pct": 12, "entry": None, "tp": 15, "sl": 8, "dca": "12:20", "last_sent_ts": 0, "last_dca_sent_week": 0}
        u["weekly_plan"] = {"QQQ": 30, "SCHD": 20, "JEPQ": 20}
        u["weekly_budget"] = 70
        u["dip_budget"] = 40
        msg = "✅ Copy aplicado: AGGRESSIVE."
    elif name == "income":
        u["alerts"].clear()
        u["alerts"]["QQQ"] = {"buy_drop_pct": 12, "entry": None, "tp": 10, "sl": 7, "dca": "12:20", "last_sent_ts": 0, "last_dca_sent_week": 0}
        u["weekly_plan"] = {"JEPQ": 35, "SCHD": 25, "QQQ": 10}
        u["weekly_budget"] = 70
        u["dip_budget"] = 40
        msg = "✅ Copy aplicado: INCOME."
    elif name == "conservative":
        u["alerts"].clear()
        u["alerts"]["QQQ"] = {"buy_drop_pct": 12, "entry": None, "tp": 8, "sl": 6, "dca": "12:20 18:20", "last_sent_ts": 0, "last_dca_sent_week": 0}
        u["weekly_plan"] = {"QQQ": 25, "SCHD": 25, "JEPQ": 20}
        u["weekly_budget"] = 70
        u["dip_budget"] = 40
        msg = "✅ Copy aplicado: CONSERVATIVE."
    else:
        return await update.message.reply_text("Estrategia no válida: aggressive | income | conservative")

    ensure_week_reset(u)
    save_data(data)
    await update.message.reply_text(msg + " Usa /list para ver todo.")


async def bought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bought dips NVDA 15
    /bought weekly QQQ 30
    /bought status
    /bought reset
    """
    data = load_data()
    u = get_user(data, str(update.effective_chat.id))
    ensure_trial(u)
    ensure_week_reset(u)

    ws = u.get("week_state", {})
    weekly_budget = float(u.get("weekly_budget", 70))
    dip_budget = float(u.get("dip_budget", 40))
    weekly_spent = float(ws.get("weekly_spent", 0))
    dip_spent = float(ws.get("dip_spent", 0))

    if len(context.args) < 1:
        return await update.message.reply_text(
            "Uso:\n"
            "• /bought dips TICKER AMT\n"
            "• /bought weekly TICKER AMT\n"
            "• /bought status\n"
            "• /bought reset"
        )

    mode = context.args[0].lower().strip()

    if mode == "status":
        return await update.message.reply_text(
            f"📊 Presupuesto semanal:\n"
            f"• Weekly: ${weekly_spent:.0f}/${weekly_budget:.0f}\n"
            f"• Dips: ${dip_spent:.0f}/${dip_budget:.0f}"
        )

    if mode == "reset":
        ws["weekly_spent"] = 0
        ws["dip_spent"] = 0
        u["week_state"] = ws
        save_data(data)
        return await update.message.reply_text("✅ Contadores reseteados para esta semana (weekly y dips).")

    if mode not in ("weekly", "dips"):
        return await update.message.reply_text("Modo inválido. Usa: weekly | dips | status | reset")

    if len(context.args) < 3:
        return await update.message.reply_text("Uso: /bought weekly QQQ 30  ó  /bought dips NVDA 15")

    ticker = context.args[1].upper().strip()
    try:
        amt = float(context.args[2])
        if amt <= 0:
            raise ValueError()
    except Exception:
        return await update.message.reply_text("Monto inválido. Ej: /bought dips NVDA 15")

    if mode == "weekly":
        ws["weekly_spent"] = float(ws.get("weekly_spent", 0)) + amt
        remaining = max(0.0, weekly_budget - float(ws["weekly_spent"]))
        save_data(data)
        return await update.message.reply_text(
            f"✅ Registrado WEEKLY: {ticker} ${amt:.0f}\n"
            f"Weekly usado: ${ws['weekly_spent']:.0f}/${weekly_budget:.0f} | restante: ${remaining:.0f}"
        )

    ws["dip_spent"] = float(ws.get("dip_spent", 0)) + amt
    remaining = max(0.0, dip_budget - float(ws["dip_spent"]))
    save_data(data)
    return await update.message.reply_text(
        f"✅ Registrado DIPS: {ticker} ${amt:.0f}\n"
        f"Dips usado: ${ws['dip_spent']:.0f}/${dip_budget:.0f} | restante: ${remaining:.0f}"
    )


# ----------------------------
# Background checker
# ----------------------------
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    data = load_data()

    for chat_id, u in data.get("users", {}).items():
        ensure_week_reset(u)

        alerts = u.get("alerts", {})
        if not alerts:
            continue

        cooldown_sec = int(u.get("cooldown_min", 360)) * 60
        ws = u.get("week_state", {})
        dip_budget = float(u.get("dip_budget", 40))
        dip_spent = float(ws.get("dip_spent", 0))
        remaining_dips = max(0.0, dip_budget - dip_spent)

        for ticker, cfg in list(alerts.items()):
            buy_drop = float(cfg.get("buy_drop_pct", 0) or 0)
            last_sent = int(cfg.get("last_sent_ts", 0) or 0)

            # cooldown per ticker
            if now_ts() - last_sent < cooldown_sec:
                continue

            s = fetch_snapshot(ticker)
            if not s:
                continue

            if buy_drop > 0 and s.drop_pct >= buy_drop:
                base_text = (
                    f"📉 ALERTA: {ticker}\n"
                    f"Caída: {s.drop_pct:.1f}% desde el máximo 60d\n"
                    f"Precio aprox: ${s.price:.2f}\n"
                    f"Máximo 60d: ${s.recent_high:.2f}"
                )

                extra = ""
                if premium_active(u):
                    sc = opportunity_score(s)
                    extra_lines = [f"\n📊 Score: {sc:.1f}/10"]
                    if s.rsi14 is not None:
                        extra_lines.append(f"RSI: {s.rsi14:.1f}")
                    if s.sma50 is not None:
                        extra_lines.append(f"SMA50: {s.sma50:.2f}")
                    if s.sma200 is not None:
                        extra_lines.append(f"SMA200: {s.sma200:.2f}")
                    if s.vol_ratio is not None:
                        extra_lines.append(f"Vol ratio: {s.vol_ratio:.2f}x")
                    extra = "\n" + "\n".join(extra_lines)

                # DCA suggestion (uses remaining dips; user confirms with /bought)
                dca_note = ""
                dca_str = cfg.get("dca", "") or ""
                tiers = parse_dca(dca_str) if dca_str else []
                suggested = dca_suggestion(s.drop_pct, tiers) if tiers else None

                current_week = int(ws.get("week_monday_ts", 0))
                last_week_sent = int(cfg.get("last_dca_sent_week", 0) or 0)

                if suggested is not None and remaining_dips > 0 and current_week != last_week_sent:
                    buy_amt = min(float(suggested), remaining_dips)
                    if buy_amt > 0:
                        dca_note = (
                            f"\n\n🧠 DCA sugerido (dips): ${buy_amt:.0f} "
                            f"(restante dips: ${remaining_dips:.0f}/${dip_budget:.0f})"
                            f"\n✅ Confirma si compras: /bought dips {ticker} {int(buy_amt)}"
                        )
                        cfg["last_dca_sent_week"] = current_week
                elif remaining_dips <= 0:
                    dca_note = f"\n\n⚠️ Dips semanal agotado: ${dip_spent:.0f}/${dip_budget:.0f}"

                cfg["last_sent_ts"] = now_ts()
                alerts[ticker] = cfg

                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=base_text + extra + dca_note
                )

    save_data(data)


def build_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno (Render).")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("grantpremium", grantpremium))

    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("setsell", setsell))
    app.add_handler(CommandHandler("entry", entry))
    app.add_handler(CommandHandler("dca", dca))

    app.add_handler(CommandHandler("setbudget", setbudget))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("monday", monday))

    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("show", show))
    app.add_handler(CommandHandler("remove", remove))

    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("cashflow", cashflow))
    app.add_handler(CommandHandler("copy", copy_strategy))

    # NEW:
    app.add_handler(CommandHandler("bought", bought))

    # Checker every 5 minutes
    app.job_queue.run_repeating(check_job, interval=5 * 60, first=15)

    return app


def main():
    app = build_app()
    # keep only 1 instance running in Render to avoid Conflict getUpdates
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
