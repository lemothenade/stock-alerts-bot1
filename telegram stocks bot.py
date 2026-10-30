"""Telegram TA bot — ONE Lambda, all features.

A single AWS Lambda function that does three jobs, deciding which by looking at
what triggered it:

  1. INTERACTIVE  — you message the bot and it replies.
                    Triggered by a Telegram webhook (API Gateway or Function URL).
                    Commands: /help /ping /analyze <TICKER> /signals <TICKER> /daily

  2. DAILY DIGEST — an automatic once-a-day summary of your watchlist.
                    Triggered by an EventBridge schedule sending {"daily": true}.

  3. NEW-SIGNAL ALERTS — instant push when a brand-new signal appears, compared
                    to the last run (state stored in S3). Triggered by any
                    scheduled run that is NOT a forced digest.

  (bonus) {"test_hello": true}  -> sends an "armed" confirmation.

Environment variables (set these on the Lambda):
  TELEGRAM_TOKEN     token from BotFather                       (required)
  TELEGRAM_CHAT_ID   your numeric chat id                       (required)
  STATE_BUCKET       S3 bucket name for remembering signals     (optional*)
  WATCHLIST          comma-separated tickers for the digest,
                     e.g. "SPY,QQQ,AAPL,BTC-USD". Default "SPY,QQQ".

  *If STATE_BUCKET is empty, the bot still works but can't remember signals
   between runs, so the "new signal" alerts are skipped (digest still fires).

Data: Yahoo Finance. Works for stocks, crypto (BTC-USD), forex (EURUSD=X),
indices (^GSPC, ^VIX), non-US (SAP.DE, 7203.T).
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import boto3
    _S3 = boto3.client("s3")
except Exception:  # boto3 missing or no creds; bot still runs without state
    _S3 = None

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BUCKET = os.environ.get("STATE_BUCKET", "")
WATCHLIST = [t.strip() for t in os.environ.get("WATCHLIST", "SPY,QQQ").split(",") if t.strip()]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Signals that are statistically weak — shown as "watch only", never "buy".
WATCH = {"vix25", "rsi2_below200", "dd5"}

LABELS = {
    "rsi2_oversold_uptrend": "RSI(2)<10 above 200SMA (86%) - SMALL tranche",
    "lowerBB_uptrend": "Below lower Bollinger + above 200SMA (87%) - SMALL tranche",
    "rsi3_oversold": "RSI(3) < 20 (72%) - SMALL tranche",
    "sma50_touch": "Touched 50-day SMA (75%) - SMALL tranche",
    "sma200_touch": "Crossed BELOW 200-day SMA (72%) - MEDIUM tranche",
    "sma200_reclaim": "Reclaimed 200-day SMA (76%) - MEDIUM tranche",
    "rsi14_oversold": "RSI(14) < 30 (74%) - MEDIUM tranche",
    "macd_below_zero": "MACD crossed below zero (76%) - MEDIUM tranche",
    "macd_buy": "MACD crossed above signal (73%) - MEDIUM tranche",
    "golden_cross": "GOLDEN CROSS 50>200 SMA (77%) - MEDIUM tranche",
    "three_down": "3 consecutive down days (73%) - SMALL tranche",
    "dd10": "Drawdown >10% (61%) - MEDIUM tranche",
    "vix30": "VIX crossed 30 (74%) - LARGE tranche",
    "vix35": "VIX crossed 35 (80%) - LARGE tranche",
    "vix40": "VIX crossed 40 (85-94%) - MAX tranche",
    "dd10_vix30": "Drawdown >10% + VIX>30 (80%) - LARGE tranche",
    "deep15": "15%+ below 200SMA (67%) - LARGE tranche",
    "deep20": "20%+ below 200SMA (92%) - MAX tranche",
    "vix25": "VIX touched 25 (53% - weak!) - GET READY, wait for 30+",
    "rsi2_below200": "RSI(2)<10 but BELOW 200SMA (61%) - weak, be careful",
    "dd5": "Dip -5% from high (54% - basically noise) - ignore or tiny",
}

# ============================================================
#  DATA + MATH
# ============================================================

def fetch(ticker, rng="3y"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    res = d["chart"]["result"][0]
    vals = (res["indicators"].get("adjclose", [{}])[0].get("adjclose")
            or res["indicators"]["quote"][0]["close"])
    return [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), float(v))
            for t, v in zip(res["timestamp"], vals) if v is not None]


def sma(s, n):
    return sum(s[-n:]) / n if len(s) >= n else None


def ema_series(s, n):
    k = 2 / (n + 1)
    out = [s[0]]
    for x in s[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(s, n):
    if len(s) < n + 1:
        return None
    g = [max(s[i] - s[i - 1], 0) for i in range(-n, 0)]
    l = [max(s[i - 1] - s[i], 0) for i in range(-n, 0)]
    ag, al = sum(g) / n, sum(l) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def get_vix_last():
    try:
        return fetch("%5EVIX")[-1][1]
    except Exception:
        return 0.0


def signals_at(closes, vix_last):
    """Return (active_set, close, s200, s50, dd, r2, r14)."""
    close = closes[-1]
    prev = closes[-2]
    s200 = sma(closes, 200)
    s50 = sma(closes, 50)
    p200 = sma(closes[:-1], 200)
    p50 = sma(closes[:-1], 50)
    w = closes[-20:]
    m = sum(w) / 20
    lower_bb = m - 2 * (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    r2, r3, r14 = rsi(closes, 2), rsi(closes, 3), rsi(closes, 14)
    macd_full = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    msig = ema_series(macd_full, 9)
    macd, pmacd = macd_full[-1], macd_full[-2]
    ms, pms = msig[-1], msig[-2]
    dd = close / max(closes) - 1
    a = set()
    above200 = s200 is not None and close > s200
    if r2 is not None and r2 < 10 and above200:
        a.add("rsi2_oversold_uptrend")
    if close < lower_bb and above200:
        a.add("lowerBB_uptrend")
    if r3 is not None and r3 < 20:
        a.add("rsi3_oversold")
    if s50 is not None and p50 is not None and prev >= p50 and close < s50:
        a.add("sma50_touch")
    if s200 is not None and p200 is not None and prev >= p200 and close < s200:
        a.add("sma200_touch")
    if s200 is not None and p200 is not None and prev <= p200 and close > s200:
        a.add("sma200_reclaim")
    if r14 is not None and r14 < 30:
        a.add("rsi14_oversold")
    if pmacd >= 0 and macd < 0:
        a.add("macd_below_zero")
    if pmacd <= pms and macd > ms:
        a.add("macd_buy")
    if s50 is not None and s200 is not None and p50 <= p200 and s50 > s200:
        a.add("golden_cross")
    if len(closes) >= 4 and all(closes[-i] < closes[-i - 1] for i in (1, 2, 3)):
        a.add("three_down")
    if dd <= -0.10:
        a.add("dd10")
    if vix_last >= 30:
        a.add("vix30")
    if vix_last >= 35:
        a.add("vix35")
    if vix_last >= 40:
        a.add("vix40")
    if dd <= -0.10 and vix_last >= 30:
        a.add("dd10_vix30")
    if s200 is not None and close < s200 * 0.85:
        a.add("deep15")
    if s200 is not None and close < s200 * 0.80:
        a.add("deep20")
    if vix_last >= 25:
        a.add("vix25")
    if r2 is not None and r2 < 10 and not above200:
        a.add("rsi2_below200")
    if dd <= -0.05:
        a.add("dd5")
    return a, close, s200, s50, dd, r2, r14

# ============================================================
#  TEXT BUILDERS
# ============================================================

def analyze_text(ticker):
    """Full human-readable analysis for one ticker (used by /analyze)."""
    try:
        data = fetch(ticker)
    except Exception:
        return (f"Couldn't find '{ticker}'. Check the symbol — e.g. AAPL, "
                f"BTC-USD, EURUSD=X, ^GSPC, SAP.DE.")
    if len(data) < 30:
        return f"Not enough price history for '{ticker}'."
    closes = [v for _, v in data]
    last_date = data[-1][0]
    vix_last = get_vix_last()
    active, close, s200, s50, dd, r2, r14 = signals_at(closes, vix_last)

    action = sorted(k for k in active if k not in WATCH)
    watch = sorted(k for k in active if k in WATCH)

    if s200 is None:
        trend = "insufficient history"
    elif close > s200 and s50 is not None and s50 > s200:
        trend = "UPTREND (above 200SMA, 50>200)"
    elif close > s200:
        trend = "above 200SMA"
    elif s50 is not None and s50 < s200:
        trend = "DOWNTREND (below 200SMA, 50<200)"
    else:
        trend = "below 200SMA"

    lines = [f"📈 {ticker.upper()}  ({last_date})", f"Price: {close:.2f}"]
    if s200 is not None:
        rel = "above" if close > s200 else "BELOW"
        lines.append(f"200SMA: {s200:.2f} ({rel})")
    if s50 is not None:
        lines.append(f"50SMA: {s50:.2f}")
    lines.append(f"Drawdown from ATH: {dd * 100:.1f}%")
    if r14 is not None:
        extra = f"   RSI(2): {r2:.1f}" if r2 is not None else ""
        lines.append(f"RSI(14): {r14:.1f}{extra}")
    if vix_last:
        lines.append(f"VIX: {vix_last:.1f}")
    lines.append(f"\nVerdict: {trend}")

    if action:
        lines.append("\n🚨 ACTION signals:")
        lines += [f"• {LABELS[k]}" for k in action]
    if watch:
        lines.append("\n👀 WATCH only (weak, don't buy yet):")
        lines += [f"• {LABELS[k]}" for k in watch]
    if not action and not watch:
        lines.append("\nNo active signals — nothing actionable right now.")
    return "\n".join(lines)


def signals_text(ticker):
    """Just the active signals for one ticker (used by /signals)."""
    full = analyze_text(ticker)
    if "ACTION signals" in full or "WATCH only" in full:
        head = full.split("\n")[0]
        sig = [ln for ln in full.split("\n") if ln.startswith(("•", "🚨", "👀"))]
        return head + "\n" + "\n".join(sig)
    return full.split("Verdict")[0].strip() + "\nNo active signals."


def _digest_block(ticker):
    """Compact multi-line block for one ticker in the daily digest."""
    try:
        data = fetch(ticker)
        if len(data) < 30:
            return (f"📈 {ticker.upper()}: not enough history", set())
        closes = [v for _, v in data]
        vix_last = get_vix_last()
        active, close, s200, s50, dd, r2, r14 = signals_at(closes, vix_last)
        rel = "above" if (s200 is not None and close > s200) else "BELOW"
        block = [f"📈 {ticker.upper()}  {close:.2f}  ({data[-1][0]})"]
        if s200 is not None:
            block.append(f"   200SMA {s200:.2f} ({rel})")
        if s50 is not None:
            block.append(f"   50SMA {s50:.2f}")
        block.append(f"   Drawdown from ATH {dd * 100:.1f}%")
        rsi_bits = []
        if r14 is not None:
            rsi_bits.append(f"RSI(14) {r14:.1f}")
        if r2 is not None:
            rsi_bits.append(f"RSI(2) {r2:.1f}")
        if rsi_bits:
            block.append("   " + "  ".join(rsi_bits))
        if active:
            block.append("   signals: " + ", ".join(sorted(active)))
        keyed = {f"{ticker.upper()}:{k}" for k in active}
        return "\n".join(block), keyed
    except Exception:
        return (f"📈 {ticker.upper()}: error fetching data", set())


def daily_report_text():
    """Full daily digest across the watchlist. Returns (text, active_keys)."""
    today = datetime.now(timezone.utc).date().isoformat()
    vix_last = get_vix_last()
    lines = [f"📊 DAILY DIGEST — {today}"]
    if vix_last:
        lines.append(f"VIX: {vix_last:.1f}")
    lines.append("")
    active_keys = set()
    if not WATCHLIST:
        lines.append("No WATCHLIST configured. Set the WATCHLIST env var.")
    else:
        blocks = []
        for t in WATCHLIST:
            block, keyed = _digest_block(t)
            blocks.append(block)
            active_keys |= keyed
        lines.append("\n\n".join(blocks))
    lines.append("\nUse /analyze <TICKER> for a full breakdown.")
    return "\n".join(lines), active_keys

# ============================================================
#  S3 STATE (for new-signal alerts)
# ============================================================

def load_state():
    if not (_S3 and BUCKET):
        return None
    try:
        obj = _S3.get_object(Bucket=BUCKET, Key="state.json")
        return set(json.loads(obj["Body"].read().decode()).get("active", []))
    except Exception:
        return set()


def save_state(active):
    if not (_S3 and BUCKET):
        return
    try:
        _S3.put_object(Bucket=BUCKET, Key="state.json", Body=json.dumps({
            "active": sorted(active),
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }).encode())
    except Exception:
        pass

# ============================================================
#  TELEGRAM
# ============================================================

def send(text, chat_id=None):
    payload = urllib.parse.urlencode({
        "chat_id": chat_id or CHAT_ID,
        "text": text,
    }).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    req = urllib.request.Request(url, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:
        return False

# ============================================================
#  COMMAND ROUTER (interactive)
# ============================================================

HELP = (
    "📊 TA bot — commands:\n"
    "/ping — quick alive check\n"
    "/analyze <TICKER> — full technical analysis\n"
    "/signals <TICKER> — active signals only\n"
    "/daily — daily digest of your watchlist now\n"
    "/help — this message\n\n"
    "Works with any Yahoo symbol: AAPL, BTC-USD, EURUSD=X, ^GSPC, SAP.DE"
)


def handle_text(text):
    text = (text or "").strip()
    if not text.startswith("/"):
        return "Send a command. Try /analyze AAPL or /help."
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    if cmd in ("/help", "/start"):
        return HELP
    if cmd == "/ping":
        return "pong ✅ bot is alive"
    if cmd == "/analyze":
        return analyze_text(args[0]) if args else "Usage: /analyze <TICKER>"
    if cmd == "/signals":
        return signals_text(args[0]) if args else "Usage: /signals <TICKER>"
    if cmd == "/daily":
        return daily_report_text()[0]
    return f"Unknown command {cmd}. Try /help."

# ============================================================
#  HANDLERS FOR EACH TRIGGER TYPE
# ============================================================

def handle_telegram(event):
    """Someone messaged the bot (webhook). Reply to them."""
    body = event.get("body") or "{}"
    try:
        update = json.loads(body)
    except Exception:
        return {"statusCode": 200, "body": "ok"}

    msg = update.get("message") or update.get("edited_message") or {}
    text = msg.get("text")
    chat_id = str((msg.get("chat") or {}).get("id", ""))

    # If a chat id is configured, only answer that chat.
    if CHAT_ID and chat_id and chat_id != str(CHAT_ID):
        return {"statusCode": 200, "body": "ok"}

    if text:
        send(handle_text(text), chat_id=chat_id or CHAT_ID)
    return {"statusCode": 200, "body": "ok"}


def handle_scan(event):
    """Scheduled run or manual test: daily digest + new-signal alerts."""
    # Manual confirmation ping.
    if isinstance(event, dict) and event.get("test_hello"):
        digest, active = daily_report_text()
        send("✅ Bot ARMED — interactive + daily digest + alerts live.\n\n" + digest)
        save_state(active)
        return {"status": "armed"}

    digest, active = daily_report_text()

    # Decide if this is a forced daily digest.
    force_digest = bool(isinstance(event, dict) and event.get("daily"))
    is_scheduled = bool(isinstance(event, dict)
                        and (event.get("source") == "aws.events"
                             or event.get("detail-type") == "Scheduled Event"))

    previous = load_state()
    if previous is not None:
        new = active - previous
    else:
        new = set()
    save_state(active)

    # New-signal alert (only on a plain scheduled run, not a forced digest).
    if new and not force_digest:
        action, watch = [], []
        for key in sorted(new):
            tkr, k = key.split(":", 1)
            label = LABELS.get(k, k)
            (watch if k in WATCH else action).append(f"[{tkr}] {label}")
        parts = []
        if action:
            parts.append("🚨 ACTION - deploy tranche:\n"
                         + "\n".join("• " + a for a in action))
        if watch:
            parts.append("👀 WATCH only (weak stats, do NOT buy yet):\n"
                         + "\n".join("• " + w for w in watch))
        send("MARKET ALERT\n\n" + "\n\n".join(parts))
        return {"status": "alerted", "new": sorted(new)}

    # Daily digest when forced, or on a scheduled run with no new signals.
    if force_digest or is_scheduled:
        send(digest)
        return {"status": "digest"}

    return {"status": "quiet"}

# ============================================================
#  SINGLE ENTRY POINT — routes by what triggered the Lambda
# ============================================================

def lambda_handler(event, context):
    # A Telegram webhook (via API Gateway / Function URL) always has "body".
    if isinstance(event, dict) and event.get("body") is not None:
        return handle_telegram(event)
    # Everything else is a schedule or a manual test.
    return handle_scan(event or {})
