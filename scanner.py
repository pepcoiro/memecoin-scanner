import os, json, time, traceback, requests, math, base64, ast
from datetime import datetime, timezone
from collections import defaultdict

RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"; BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"; WHITE="\033[97m"
def enable_ansi():
    if os.name == "nt":
        try:
            import ctypes
            h=ctypes.windll.kernel32.GetStdHandle(-11); m=ctypes.c_ulong()
            if ctypes.windll.kernel32.GetConsoleMode(h,ctypes.byref(m)): ctypes.windll.kernel32.SetConsoleMode(h,m.value|4)
        except Exception: pass
def c(text,color=RESET,bold=False): return f"{BOLD if bold else ''}{color}{text}{RESET}"
def score_color(score):
    score=float(score or 0)
    return GREEN if score>=75 else YELLOW if score>=60 else CYAN if score>=45 else DIM

BASE = "https://api.dexscreener.com"
TIMEOUT = 20
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "scanner_state.json")
WALLET_FILE = os.path.join(SCRIPT_DIR, "wallet_state.json")
TELEGRAM_ALERT_FILE = os.path.join(SCRIPT_DIR, "telegram_alert_state.json")
OUTCOME_FILE = os.path.join(SCRIPT_DIR, "alert_outcomes.json")
ANALYTICS_FILE = os.path.join(SCRIPT_DIR, "scanner_analytics.json")

WATCH_CHAINS = {"solana"}

# Solana-only scanner. Market intelligence and wallet intelligence are scoped to Solana.
def load_config_file():
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CONFIGURAZIONE.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return cfg

_CFG = load_config_file()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", _CFG.get("HELIUS_API_KEY", "")).strip()
GOPLUS_APP_KEY = os.getenv("GOPLUS_APP_KEY", _CFG.get("GOPLUS_APP_KEY", "")).strip()
GOPLUS_APP_SECRET = os.getenv("GOPLUS_APP_SECRET", _CFG.get("GOPLUS_APP_SECRET", "")).strip()
GOPLUS_ACCESS_TOKEN = None
GOPLUS_ACCESS_EXPIRES = 0
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", _CFG.get("TELEGRAM_BOT_TOKEN", "")).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", _CFG.get("TELEGRAM_CHAT_ID", "")).strip()
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", _CFG.get("JUPITER_API_KEY", "")).strip()
JUPITER_API_BASE = "https://api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"

MIN_LIQ = 20_000
MAX_MC = 10_000_000
MIN_MC = 20_000
MIN_VOL_24H = 20_000
TOP_N = 15

# Learning / validation layer.
# It records alert-time features and later outcomes without changing the scanner
# until enough independent observations exist.
OUTCOME_TRACK_HOURS = (1, 6, 24, 168)
LEARNING_MIN_RESOLVED = 50
ADAPTIVE_BONUS_MAX = 12
ADAPTIVE_MIN_LIFT = 1.20
ADAPTIVE_MAX_LIFT = 3.00
TRACK_MAX_RECORDS = 5000
TRACK_UPDATE_LIMIT = 300

# Position sizing: estimated maximum entry size for ~2% AMM price impact.
MAX_POSITION_PRICE_IMPACT = 0.02

# Automated exit strategy (applied after an entry is executed).
# Sell 30% of the original position at +30%, another 30% at +60%,
# another 30% at +90%, and keep the final 10% as a runner.
TP1_PCT = 0.30
TP2_PCT = 0.60
TP3_PCT = 0.90
TP1_SELL_FRACTION = 0.30
TP2_SELL_FRACTION = 0.30
TP3_SELL_FRACTION = 0.30
RUNNER_FRACTION = 0.10
TRAILING_STOP_PCT = 0.30

AUTO_TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
POSITIONS_FILE = os.path.join(SCRIPT_DIR, "positions.json")
WALLET_BUDGET_FRACTION = 0.50
SOL_RESERVE = 0.03
MC_BUDGET_FRACTION = 0.0025
LIQ_BUDGET_FRACTION = 0.01
MAX_EXECUTION_PRICE_IMPACT = 0.02
MAX_ENTRY_SLIPPAGE_BPS = 500
DEFAULT_USD_TO_EUR = 0.892  # fallback; refreshed from ECB when available
NATIVE_SYMBOLS = {"solana": "SOL"}
_USD_TO_EUR_CACHE = {"rate": None, "ts": 0}

SMART_WALLET_BONUS_MAX = 22
EARLY_WINDOW_MIN = 60
MAX_WALLETS_PER_TOKEN = 30
MIN_WALLET_WINS = 2
WALLET_PENDING_HOURS = 48
MAX_ENTRY_P1_FOR_SMART = 60
MAX_ENTRY_P24_FOR_SMART = 150
ALERT_MAX_P1 = 60
ALERT_MAX_P24 = 150


session = requests.Session()
session.headers.update({"User-Agent": "MemecoinScanner/3.0"})


def num(x):
    try:
        return float(x or 0)
    except:
        return 0.0

def get_json(url, **kwargs):
    last_error = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=TIMEOUT, **kwargs)
            if r.status_code == 429:
                retry_after = num(r.headers.get("Retry-After")) or (1.5 * (attempt + 1))
                time.sleep(min(8, retry_after))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.8 * (2 ** attempt))
    raise last_error

def get_usd_to_eur_rate():
    """Return USD→EUR using latest ECB observation, with a safe fallback."""
    now = time.time()
    if _USD_TO_EUR_CACHE["rate"] and now - _USD_TO_EUR_CACHE["ts"] < 6 * 3600:
        return _USD_TO_EUR_CACHE["rate"]
    try:
        url = (
            "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
            "?format=csvdata&lastNObservations=1"
        )
        r = session.get(url, timeout=10)
        r.raise_for_status()
        lines = [line.strip() for line in r.text.splitlines() if line.strip()]
        if len(lines) >= 2:
            headers = [h.strip().strip('"') for h in lines[0].split(',')]
            values = [v.strip().strip('"') for v in lines[-1].split(',')]
            rate = float(values[headers.index("OBS_VALUE")])
            if rate > 0:
                _USD_TO_EUR_CACHE.update(rate=rate, ts=now)
                return rate
    except Exception:
        pass
    return DEFAULT_USD_TO_EUR

def native_symbol(chain):
    return NATIVE_SYMBOLS.get(str(chain or "").lower(), "NATIVE")

def estimate_max_position(p):
    """Estimate max entry for about 2% AMM price impact.

    This uses a conservative constant-product approximation from the reported
    pool liquidity. Real execution can differ because of concentrated liquidity,
    fees, routing, MEV and other market conditions.
    """
    liq_usd = num((p.get("liquidity") or {}).get("usd"))
    if liq_usd <= 0:
        return {"usd": 0.0, "eur": 0.0, "native": 0.0,
                "native_symbol": native_symbol(p.get("chainId")),
                "impact": MAX_POSITION_PRICE_IMPACT}

    impact = MAX_POSITION_PRICE_IMPACT
    # For a constant-product pool with equal USD reserves, a buy that moves
    # price by +impact uses quote reserve x = 1 - 1/sqrt(1+impact).
    # This remains only an approximation for concentrated-liquidity pools.
    reserve_quote = liq_usd / 2.0
    max_usd = reserve_quote * (1.0 - 1.0 / math.sqrt(1.0 + impact))
    max_eur = max_usd * get_usd_to_eur_rate()

    price_usd = num(p.get("priceUsd"))
    price_native = num(p.get("priceNative"))
    native_usd = (price_usd / price_native) if price_usd > 0 and price_native > 0 else 0.0
    max_native = max_usd / native_usd if native_usd > 0 else 0.0

    return {"usd": max_usd, "eur": max_eur, "native": max_native,
            "native_symbol": native_symbol(p.get("chainId")),
            "native_usd": native_usd, "impact": impact}

def format_position_size(p):
    size = estimate_max_position(p)
    native = size["native"]
    if native >= 100:
        native_text = f"{native:,.0f} {size['native_symbol']}"
    elif native >= 1:
        native_text = f"{native:,.2f} {size['native_symbol']}"
    else:
        native_text = f"{native:,.4f} {size['native_symbol']}"
    return f"€{size['eur']:,.0f}  |  {native_text}", size

def suggested_position_size(p):
    """Suggest a conservative position using market cap + liquidity.

    The suggestion is intentionally separate from the technical ~2% AMM-impact
    ceiling. It uses 0.25% of market cap and 1% of reported liquidity, then
    caps the result at the existing technical maximum.
    """
    liq_usd = num((p.get("liquidity") or {}).get("usd"))
    mc_usd = num(p.get("marketCap") or p.get("fdv"))
    max_size = estimate_max_position(p)

    if liq_usd <= 0 or mc_usd <= 0:
        return {
            "usd": 0.0,
            "eur": 0.0,
            "native": 0.0,
            "native_symbol": max_size["native_symbol"],
        }

    mc_cap = mc_usd * 0.0025
    liq_cap = liq_usd * 0.01
    suggested_usd = min(mc_cap, liq_cap, max_size["usd"])

    rate = get_usd_to_eur_rate()
    suggested_eur = suggested_usd * rate
    native_usd = max_size.get("native_usd", 0.0)
    suggested_native = suggested_usd / native_usd if native_usd > 0 else 0.0

    return {
        "usd": suggested_usd,
        "eur": suggested_eur,
        "native": suggested_native,
        "native_symbol": max_size["native_symbol"],
    }

def format_suggested_position(p):
    size = suggested_position_size(p)
    native = size["native"]
    if native >= 100:
        native_text = f"{native:,.0f} {size['native_symbol']}"
    elif native >= 1:
        native_text = f"{native:,.2f} {size['native_symbol']}"
    else:
        native_text = f"{native:,.4f} {size['native_symbol']}"
    return f"€{size['eur']:,.0f}  |  ${size['usd']:,.0f}  |  {native_text}", size

def age_hours(p):
    c = p.get("pairCreatedAt")
    if not c:
        return 999999
    return max(0, (time.time()*1000-c)/3600000)

def raw_score(p, previous=None):
    """V11 EARLY SCORE + empirical learning layer.
    Focuses on early setups rather than tokens that already made a huge move.
    Adds Volume Burst (current 1h activity versus the token's 24h hourly pace)
    and treats scan-to-scan acceleration as a secondary signal.
    """
    liq = num((p.get("liquidity") or {}).get("usd"))
    mc = num(p.get("marketCap") or p.get("fdv"))
    v1 = num((p.get("volume") or {}).get("h1"))
    v6 = num((p.get("volume") or {}).get("h6"))
    v24 = num((p.get("volume") or {}).get("h24"))
    tx = (p.get("txns") or {}).get("h1") or {}
    buys = num(tx.get("buys")); sells = num(tx.get("sells"))
    pc = p.get("priceChange") or {}
    p1 = num(pc.get("h1")); p6 = num(pc.get("h6")); p24 = num(pc.get("h24"))
    age = age_hours(p)

    # Volume Burst: 1.0x means the last hour is exactly the average hourly
    # pace of the last 24h. It is intentionally capped for scoring so a single
    # extreme hour cannot dominate the entire ranking.
    burst = 0.0
    if v24 > 0:
        burst = (v1 * 24.0) / v24

    s = 0

    # 1) MICROCAP / liquidity
    if 20_000 <= mc <= 500_000: s += 16
    elif mc <= 2_000_000: s += 11
    elif mc <= 10_000_000: s += 5

    if liq >= 75_000: s += 13
    elif liq >= 40_000: s += 10
    elif liq >= 20_000: s += 6

    # 2) VOLUME RELATIVE TO MC
    if mc > 0:
        vm = v24/mc
        if vm >= 2: s += 14
        elif vm >= 1: s += 11
        elif vm >= .5: s += 7
        elif vm >= .2: s += 3

    # 3) CURRENT 1H VOLUME
    if v1 >= 150_000: s += 13
    elif v1 >= 75_000: s += 10
    elif v1 >= 25_000: s += 7
    elif v1 >= 10_000: s += 3

    # 4) VOLUME BURST.
    # A burst is useful, but extreme values can also be caused by bots,
    # wash-trading or a thin pool. Do not reward increasingly extreme bursts
    # indefinitely.
    if burst >= 20: s -= 10
    elif burst >= 15: s -= 5
    elif burst >= 10: s += 15
    elif burst >= 6: s += 12
    elif burst >= 3: s += 9
    elif burst >= 2: s += 6
    elif burst >= 1.25: s += 3

    # Extremely high volume relative to liquidity is not automatically bullish:
    # it can be bot/wash-trading activity or a dangerously thin pool.
    # Stronger penalties prevent thin pools from being promoted by volume alone.
    liq_ratio = (v1 / liq) if liq > 0 else 0
    if liq_ratio >= 40: s -= 40
    elif liq_ratio >= 25: s -= 30
    elif liq_ratio >= 15: s -= 20
    elif liq_ratio >= 10: s -= 10

    # 5) TRANSACTION QUALITY
    # Buy pressure alone is easy to fake; activity breadth is a second, weaker signal.
    total = buys + sells
    if total >= 250: s += 4
    elif total >= 100: s += 2
    elif total < 20: s -= 2

    # 6) BUY PRESSURE
    if total:
        bp = buys/total
        if bp >= .68: s += 12
        elif bp >= .60: s += 9
        elif bp >= .55: s += 6
        elif bp >= .50: s += 2
        elif bp < .42: s -= 5

    # 6) FRESH MOMENTUM
    if 8 <= p1 <= 40: s += 8
    elif 3 <= p1 < 8: s += 5
    elif 0 < p1 < 3: s += 2
    elif p1 < -15: s -= 5

    if 15 <= p6 <= 80: s += 7
    elif 5 <= p6 < 15: s += 4
    elif p6 < -20: s -= 4

    # 7) FRESHNESS
    if age <= 3: s += 8
    elif age <= 12: s += 7
    elif age <= 24: s += 5
    elif age <= 72: s += 2
    else: s -= 2

    # 8) SCAN-TO-SCAN ACCELERATION — secondary signal, not a hard gate.
    accel = 0
    has_history = False
    if previous:
        old_v1 = num(previous.get("v1"))
        if old_v1 > 0:
            has_history = True
            accel = v1/old_v1
            if accel >= 4: s += 12
            elif accel >= 3: s += 10
            elif accel >= 2: s += 8
            elif accel >= 1.5: s += 5
            elif accel >= 1.25: s += 2
            elif accel < .65: s -= 3

    # 9) CRITICAL: penalize pumps that already happened.
    if p6 >= 800 or p24 >= 800: s -= 40
    elif p6 >= 500 or p24 >= 500: s -= 35
    elif p6 >= 300 or p24 >= 300: s -= 25
    elif p6 >= 150 or p24 >= 150: s -= 17
    elif p6 >= 100 or p24 >= 100: s -= 10
    elif p6 >= 60 or p24 >= 60: s -= 5

    if p24 >= 300 and p1 <= 2:
        s -= 8

    # Very vertical 1h candles are usually late-entry risk.
    if p1 > 100: s -= 18
    elif p1 > 60: s -= 10

    return min(100, max(0, s)), accel, has_history, burst, liq_ratio

def discover():
    candidates = {}
    for endpoint in ["/token-profiles/latest/v1", "/token-boosts/latest/v1"]:
        try:
            data = get_json(BASE+endpoint)
        except Exception as e:
            print("Discovery error:", e)
            continue
        for item in data if isinstance(data, list) else []:
            chain = str(item.get("chainId","")).lower()
            token = item.get("tokenAddress")
            if chain in WATCH_CHAINS and token:
                candidates[(chain, token)] = item

    pairs = []
    keys = list(candidates)[:150]
    for i, (chain, token) in enumerate(keys, 1):
        try:
            data = get_json(f"{BASE}/token-pairs/v1/{chain}/{token}")
            if isinstance(data, list):
                pairs.extend(data)
        except:
            pass
        if i % 25 == 0:
            print(f"Analizzati {i}/{len(keys)} token...")

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except:
        state = {}

    rows, newstate, seen = [], {}, set()

    for p in pairs:
        chain = str(p.get("chainId","")).lower()
        pair = p.get("pairAddress")
        if chain not in WATCH_CHAINS or not pair or pair in seen:
            continue
        seen.add(pair)

        liq = num((p.get("liquidity") or {}).get("usd"))
        mc = num(p.get("marketCap") or p.get("fdv"))
        v24 = num((p.get("volume") or {}).get("h24"))
        if liq < MIN_LIQ or not(MIN_MC <= mc <= MAX_MC) or v24 < MIN_VOL_24H:
            continue

        old = state.get(pair)
        score, accel, has_history, burst, liq_ratio = raw_score(p, old)
        p["_score"] = score
        p["_accel"] = accel
        p["_has_accel_history"] = has_history
        p["_burst"] = burst
        p["_liq_ratio"] = liq_ratio
        rows.append(p)

        newstate[pair] = {
            "ts": time.time(),
            "v1": num((p.get("volume") or {}).get("h1")),
            "v24": v24,
            "mc": mc,
            "liq": liq,
            "price": num(p.get("priceUsd"))
        }

    # Atomic-ish write: avoids leaving a half-written state file and gives a
    # useful fallback if Windows temporarily locks the old file.
    try:
        tmp_state = STATE_FILE + ".tmp"
        with open(tmp_state, "w", encoding="utf-8") as f:
            json.dump(newstate, f, indent=2)
        try:
            os.replace(tmp_state, STATE_FILE)
        except PermissionError:
            # If another process has the old state file open, keep scanning
            # without destroying the existing state.
            try:
                os.remove(tmp_state)
            except Exception:
                pass
            print("[!] scanner_state.json e' bloccato: continuo senza aggiornare lo storico.")
    except PermissionError:
        print("[!] Impossibile scrivere scanner_state.json: continuo la scansione.")

    rows.sort(key=lambda x: (x["_score"], x["_accel"]), reverse=True)
    return rows[:TOP_N]

def wallet_entry_quality(p):
    """Classify whether a wallet entry looks genuinely early.
    This is intentionally conservative: a wallet found in a token that is already
    vertical is not allowed to become a strong smart-wallet signal.
    """
    pc = p.get("priceChange") or {}
    p1 = num(pc.get("h1")); p24 = num(pc.get("h24"))
    age = age_hours(p)
    if p1 > MAX_ENTRY_P1_FOR_SMART or p24 > MAX_ENTRY_P24_FOR_SMART:
        return "late"
    if p1 > 35 or p24 > 80:
        return "early-ish"
    if age <= 3:
        return "very-early"
    return "early"

def wallet_return(p, entry_mc):
    """Approximate current multiple versus the MC captured when the wallet entered."""
    mc = num(p.get("marketCap") or p.get("fdv"))
    return (mc / entry_mc) if entry_mc > 0 and mc > 0 else 0.0

def settle_pending_for_token(rec, token_id, current_mc):
    """Settle a previously observed wallet call once enough movement is visible.
    A call is a WIN only if the token reached at least 2x from the observed entry.
    A failed call is counted after 48h or if the token clearly collapsed.
    """
    pending = rec.get("pending", [])
    changed = False
    keep = []
    now = time.time()
    for item in pending:
        if item.get("token") != token_id:
            keep.append(item); continue
        entry = num(item.get("entry_mc"))
        mult = current_mc/entry if entry > 0 else 0
        age_h = max(0, (now-num(item.get("ts")))/3600)
        if mult >= 2.0:
            rec["wins"] = int(rec.get("wins",0)) + 1
            rec.setdefault("peak_mults",[]).append(round(mult,2))
            changed = True
        elif age_h >= WALLET_PENDING_HOURS or (age_h >= 6 and mult > 0 and mult < 0.55):
            rec["losses"] = int(rec.get("losses",0)) + 1
            changed = True
        else:
            keep.append(item)
    rec["pending"] = keep
    return changed

def load_wallets():
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_wallets(data):
    try:
        tmp_wallet = WALLET_FILE + ".tmp"
        with open(tmp_wallet, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.replace(tmp_wallet, WALLET_FILE)
        except PermissionError:
            try:
                os.remove(tmp_wallet)
            except Exception:
                pass
            print("[!] wallet_state.json e' bloccato: continuo senza aggiornare il database.")
    except PermissionError:
        print("[!] Impossibile scrivere wallet_state.json: continuo la scansione.")

def _update_wallet_record(wallets, chain, wallet, token_id, p):
    if not wallet_is_seed_eligible(wallet):
        return wallets.get(f"{chain}:{wallet}", {})
    key=f"{chain}:{wallet}"
    rec=wallets.setdefault(key, {
        "chain":chain, "wallet":wallet, "calls":0, "wins":0, "losses":0,
        "tokens":[], "pending":[], "peak_mults":[], "last_seen":0,
        "early_entries":0, "late_entries":0
    })
    # Keep the strongest/cleanest early evidence separate from the raw call count.
    quality=wallet_entry_quality(p)
    if quality == "late":
        rec["late_entries"]=int(rec.get("late_entries",0))+1
        rec["last_seen"]=time.time()
        return rec
    rec["early_entries"]=int(rec.get("early_entries",0))+1
    token_seen = token_id in rec.get("tokens",[])
    current_mc=num(p.get("marketCap") or p.get("fdv"))
    if not token_seen:
        rec["calls"] = int(rec.get("calls",0))+1
        rec["tokens"]=(rec.get("tokens",[])+[token_id])[-100:]
        rec.setdefault("pending",[]).append({
            "token":token_id, "ts":time.time(),
            "entry_mc":current_mc, "entry_p1":num((p.get("priceChange") or {}).get("h1")),
            "entry_p24":num((p.get("priceChange") or {}).get("h24")),
            "quality":quality
        })
    # If this exact token is seen again, use its current MC to settle the pending call.
    settle_pending_for_token(rec, token_id, current_mc)
    rec["last_seen"]=time.time()
    return rec

def rank_wallets_for_token(wallets, chain, buyers):
    ranked=[]
    for w in buyers:
        rec=wallets.get(f"{chain}:{w}")
        if not rec or int(rec.get("calls",0)) < MIN_WALLET_WINS:
            continue
        if int(rec.get("wins",0)) + int(rec.get("losses",0)) < 2:
            continue
        calls=int(rec.get("calls",0)); wins=int(rec.get("wins",0)); losses=int(rec.get("losses",0))
        resolved=wins+losses
        wr=wins/resolved if resolved else 0
        early=int(rec.get("early_entries",0)); late=int(rec.get("late_entries",0))
        early_rate=early/max(1,early+late)
        avg_peak=sum(rec.get("peak_mults",[]))/max(1,len(rec.get("peak_mults",[]))) if rec.get("peak_mults") else 0
        quality_score=min(10, round((wr*7) + min(early_rate,1)*2 + min(avg_peak,10)*0.1,1))
        confidence=min(1.0, resolved/5)
        ranked.append((quality_score*confidence,w,wins,calls,wr,early_rate,avg_peak,resolved))
    ranked.sort(reverse=True)
    return ranked[:5]

def update_smart_wallets(rows):
    """Solana smart-wallet engine using Helius only."""
    wallets = load_wallets()
    evidence = {}
    if not HELIUS_API_KEY:
        save_wallets(wallets)
        return evidence, wallets
    for p in rows:
        if str(p.get("chainId", "")).lower() != "solana":
            continue
        buyers = solana_early_buyers(p)
        mint = (p.get("baseToken") or {}).get("address")
        if not buyers or not mint:
            continue
        for w in buyers:
            _update_wallet_record(wallets, "solana", w, mint, p)
        evidence[p.get("pairAddress")] = rank_wallets_for_token(wallets, "solana", buyers)
    save_wallets(wallets)
    return evidence, wallets

def smart_bonus(p,evidence):
    ranked=evidence.get(p.get("pairAddress"),[])
    if not ranked: return 0,[]
    bonus=0
    for _,wallet,wins,calls,wr,early_rate,avg_peak,resolved in ranked[:3]:
        # Require actual resolved wins. Multiple independent strong wallets stack.
        # A single marginal reputation record must never contribute a large
        # score jump. Require at least two resolved outcomes for full weight.
        if resolved < 2:
            continue
        per=min(6.0, 0.8 + wins*0.8 + wr*2.5 + early_rate*1.2)
        bonus += per
    return min(SMART_WALLET_BONUS_MAX,round(bonus,1)),ranked

def helius_transactions(address, limit=100):
    """Recent parsed Solana transactions for an address."""
    if not HELIUS_API_KEY or not address:
        return []
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    try:
        r = session.get(url, params={"api-key": HELIUS_API_KEY, "limit": limit},
                        timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except:
        return []

def solana_early_buyers(p):
    """
    For a young Solana pair, inspect parsed transactions involving the pair.
    Helius Enhanced Transactions expose feePayer + tokenTransfers, which lets
    us approximate the wallet that initiated the swap when the pool sent the
    candidate token out.
    """
    if not HELIUS_API_KEY:
        return []

    if str(p.get("chainId","")).lower() != "solana":
        return []

    pair = p.get("pairAddress")
    mint = (p.get("baseToken") or {}).get("address")
    created_ms = p.get("pairCreatedAt")
    if not pair or not mint or not created_ms:
        return []

    age = age_hours(p)
    if age > 72:
        return []

    txs = helius_transactions(pair, 100)
    if not txs:
        return []

    end_ts = created_ms/1000 + EARLY_WINDOW_MIN*60
    buyers = []
    seen = set()

    for tx in reversed(txs):
        ts = tx.get("timestamp")
        if not ts:
            continue
        if ts < created_ms/1000:
            continue
        if ts > end_ts:
            continue
        if tx.get("transactionError"):
            continue

        transfers = tx.get("tokenTransfers") or []
        # We want the candidate mint moving into a user-controlled account.
        hit = False
        for tr in transfers:
            if tr.get("mint") != mint:
                continue
            frm = (tr.get("fromUserAccount") or "").lower()
            to = (tr.get("toUserAccount") or "").lower()
            if to and frm != pair.lower():
                # In aggregators the fromUserAccount can be an intermediary;
                # feePayer is a better first approximation of the initiator.
                hit = True
                break

        if not hit:
            continue

        wallet = (tx.get("feePayer") or "").strip()
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        buyers.append(wallet)
        if len(buyers) >= MAX_WALLETS_PER_TOKEN:
            break

    return buyers


def goplus_access_token(force=False):
    global GOPLUS_ACCESS_TOKEN, GOPLUS_ACCESS_EXPIRES

    if not GOPLUS_APP_KEY or not GOPLUS_APP_SECRET:
        return None

    now = int(time.time())
    if not force and GOPLUS_ACCESS_TOKEN and now < GOPLUS_ACCESS_EXPIRES - 60:
        return GOPLUS_ACCESS_TOKEN

    import hashlib
    raw = f"{GOPLUS_APP_KEY}{now}{GOPLUS_APP_SECRET}".encode("utf-8")
    sign = hashlib.sha1(raw).hexdigest()

    try:
        r = session.post(
            "https://api.gopluslabs.io/api/v1/token",
            json={"app_key": GOPLUS_APP_KEY, "time": now, "sign": sign},
            timeout=TIMEOUT
        )

        if r.status_code not in (200, 201):
            return None

        body = r.json()
        result = body.get("result") or {}
        token = result.get("access_token")
        expires = int(result.get("expires_in") or 3600)

        if not token:
            return None

        GOPLUS_ACCESS_TOKEN = token
        GOPLUS_ACCESS_EXPIRES = now + expires
        return token

    except Exception as e:
        return None

def goplus_request(url, params):
    token = goplus_access_token()
    if not token:
        return None
    headers = {"Authorization": token}
    try:
        r = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
        if r.status_code == 401:
            token = goplus_access_token(force=True)
            if not token:
                return None
            headers = {"Authorization": token}
            r = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("code") != 1:
            return None
        return body.get("result")
    except Exception:
        return None

def _extract_goplus_token(result, address):
    """Extract a token-security record from GoPlus dict/list response."""
    if not result or not address:
        return None
    target = str(address).lower()
    if isinstance(result, dict):
        if address in result and isinstance(result[address], dict):
            return result[address]
        for key, value in result.items():
            if str(key).lower() == target and isinstance(value, dict):
                return value
        # Some API variants return the token record directly.
        if any(k in result for k in ("token_name","token_symbol","is_honeypot","is_open_source","holders")):
            return result
        dict_values = [v for v in result.values() if isinstance(v, dict)]
        if len(dict_values) == 1:
            return dict_values[0]
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                a = str(item.get("contract_address") or item.get("token_address") or "").lower()
                if a == target or not a:
                    return item
    return None

def goplus_solana(address):
    if not address:
        return None
    result = goplus_request(
        "https://api.gopluslabs.io/api/v1/solana/token_security",
        {"contract_addresses": address}
    )
    return _extract_goplus_token(result, address)


def security(p):
    chain = str(p.get("chainId", "")).lower()
    address = (p.get("baseToken") or {}).get("address")
    if chain != "solana" or not address:
        return {"status": "UNKNOWN", "reason": "Solana contract missing"}
    data = goplus_solana(address)
    if not data:
        return {"status": "UNVERIFIED", "reason": "GoPlus: nessun dato per questo token"}
    bad = []
    for k, label in [
        ("is_honeypot", "honeypot"),
        ("cannot_sell", "cannot_sell"),
        ("blacklist", "blacklist"),
        ("is_mintable", "mintable"),
        ("transfer_pausable", "transfer_pausable"),
    ]:
        if str(data.get(k, "0")) == "1":
            bad.append(label)
    for k, label in [("buy_tax", "buy_tax"), ("sell_tax", "sell_tax")]:
        try:
            if float(data.get(k, 0) or 0) > 0.10:
                bad.append(label)
        except Exception:
            pass
    result = {"status": "RISK" if bad else "PASS", "bad": bad, "raw": data}
    quality_penalty, quality_flags = security_quality(p, result)
    result["quality_penalty"] = quality_penalty
    result["quality_flags"] = quality_flags
    return result


def security_quality(p, sec):
    """Normalize GoPlus structural risk into a small independent quality penalty."""
    raw = sec.get("raw") or {}
    risk = 0
    flags = []

    def pct_value(*keys):
        for key in keys:
            if key in raw and raw.get(key) not in (None, ""):
                try:
                    v = float(raw.get(key))
                    # GoPlus sometimes returns fractions and sometimes percentages.
                    return v * 100 if 0 <= v <= 1 else v
                except Exception:
                    pass
        return None

    # Creator/owner concentration. Field names differ by chain/version, so inspect
    # only values that are actually present.
    creator = pct_value("creator_percent", "creator_percentage", "creator_holding_percent")
    owner = pct_value("owner_percent", "owner_percentage", "owner_holding_percent")
    top10 = pct_value("top10_holder_percent", "top10_holders_percent", "holders_percent")

    for value, label, threshold in [
        (creator, "creator concentration", 20),
        (owner, "owner concentration", 20),
        (top10, "top10 concentration", 60),
    ]:
        if value is not None and value >= threshold:
            risk += 4 if value < threshold * 1.5 else 8
            flags.append(f"{label} {value:.1f}%")

    # LP / DEX structure where GoPlus exposes it.
    is_dex = str(raw.get("is_in_dex", "1")).lower()
    if is_dex in {"0", "false"}:
        risk += 6
        flags.append("not in recognised DEX")
    lp_locked = raw.get("lp_holders")
    if isinstance(lp_locked, list) and lp_locked:
        # If every known LP holder is clearly unlocked, treat as a warning.
        unlocked = 0
        for holder in lp_locked:
            if isinstance(holder, dict):
                lock = holder.get("is_locked") or holder.get("locked")
                if str(lock).lower() in {"0", "false", "none", ""}:
                    unlocked += 1
        if unlocked and unlocked == len(lp_locked):
            risk += 6
            flags.append("LP appears unlocked")

    return min(20, risk), flags


def fmt(p, sec, smart_bonus_value=0, ranked=None):
    b = p.get("baseToken") or {}
    liq = num((p.get("liquidity") or {}).get("usd"))
    mc = num(p.get("marketCap") or p.get("fdv"))
    v1 = num((p.get("volume") or {}).get("h1"))
    v24 = num((p.get("volume") or {}).get("h24"))
    tx = (p.get("txns") or {}).get("h1") or {}
    buys = int(num(tx.get("buys"))); sells = int(num(tx.get("sells")))
    bp = 100*buys/(buys+sells) if buys+sells else 0
    pc = p.get("priceChange") or {}

    wallet_line = "Smart wallets: none"
    if ranked:
        wallet_line = f"Smart wallets: {len(ranked)} | bonus +{smart_bonus_value}"
        wallet_line += "\n" + "\n".join(
            f"  {w[:8]}…{w[-6:]} | {wins}W/{calls} calls | WR {wr*100:.0f}% | early {early_rate*100:.0f}% | avg peak {avg_peak:.1f}x"
            for _,w,wins,calls,wr,early_rate,avg_peak,resolved in ranked[:3]
        )

    sec_color=GREEN if sec.get("status")=="PASS" else RED if sec.get("status")=="RISK" else YELLOW
    sec_text=c(sec.get("status","UNKNOWN"),sec_color,True)
    return (
        f"{c(b.get('symbol','?'),WHITE,True)} — {b.get('name','?')}\n"
        f"Chain: {p.get('chainId')} | DEX: {p.get('dexId')}\n"
        f"Contract: {b.get('address')}\n"
        f"Score: {c(str(p.get('_final_score',p.get('_score')))+'/100',score_color(p.get('_final_score',p.get('_score'))),True)} | Base {p.get('_score')}/100 | Accel {((format(p.get('_accel',0), '.1f') + 'x') if p.get('_has_accel_history') else 'N/D (prima scansione)')} | Burst {c(f"{p.get('_burst',0):.1f}x",MAGENTA)}\n"
        f"MC: ${mc:,.0f} | Liquidity: ${liq:,.0f} | Vol/Liq {p.get('_liq_ratio',0):.1f}x\n"
        f"Max position (~2% impact): {format_position_size(p)[0]}\n"
        f"Volume 1h: ${v1:,.0f} | 24h: ${v24:,.0f}\n"
        f"Buy/Sell 1h: {buys}/{sells} ({bp:.1f}% buy)\n"
        f"Price: 1h {num(pc.get('h1')):+.1f}% | 6h {num(pc.get('h6')):+.1f}% | 24h {num(pc.get('h24')):+.1f}%\n"
        f"{wallet_line}\n"
        f"Security: {sec_text} {sec.get('bad','')}\n"
        f"DEX: {p.get('url')}"
    )



def looks_like_wallet_address(wallet):
    w = str(wallet or "").lower()
    if not w:
        return False
    if w in KNOWN_INFRA_WALLETS:
        return False
    return True

def wallet_is_seed_eligible(wallet, tx_count=0):
    """Reject obvious infrastructure and extremely active bot-like addresses."""
    if not looks_like_wallet_address(wallet):
        return False
    # A huge transfer count is much more likely to be infrastructure/bot activity
    # than a useful independent early-wallet signal.
    if tx_count and tx_count > 5000:
        return False
    return True

def _load_json_state(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default

def _save_json_atomic(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[State] Impossibile salvare {os.path.basename(path)}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def _pair_snapshot(pair):
    return {
        "ts": time.time(),
        "price": num(pair.get("priceUsd")),
        "mc": num(pair.get("marketCap") or pair.get("fdv")),
        "liq": num((pair.get("liquidity") or {}).get("usd")),
        "volume_1h": num((pair.get("volume") or {}).get("h1")),
        "volume_24h": num((pair.get("volume") or {}).get("h24")),
        "p1": num((pair.get("priceChange") or {}).get("h1")),
        "p24": num((pair.get("priceChange") or {}).get("h24")),
    }

def feature_snapshot(p):
    tx = (p.get("txns") or {}).get("h1") or {}
    buys = num(tx.get("buys")); sells = num(tx.get("sells"))
    total = buys + sells
    mc = num(p.get("marketCap") or p.get("fdv"))
    liq = num((p.get("liquidity") or {}).get("usd"))
    v1 = num((p.get("volume") or {}).get("h1"))
    v24 = num((p.get("volume") or {}).get("h24"))
    return {
        "chain": str(p.get("chainId", "")),
        "mc": mc, "liq": liq, "v1": v1, "v24": v24,
        "vm": v24 / mc if mc else 0,
        "vol_liq": v1 / liq if liq else 0,
        "burst": num(p.get("_burst")),
        "accel": num(p.get("_accel")),
        "has_accel": bool(p.get("_has_accel_history")),
        "buy_pressure": buys / total if total else 0,
        "p1": num((p.get("priceChange") or {}).get("h1")),
        "p6": num((p.get("priceChange") or {}).get("h6")),
        "p24": num((p.get("priceChange") or {}).get("h24")),
        "age_hours": age_hours(p),
        "base_score": num(p.get("_score")),
        "smart_bonus": num(p.get("_smart_bonus")),
        "final_score": num(p.get("_final_score")),
    }

def register_alert_outcomes(alerts):
    """Create immutable-at-entry records for every Telegram alert."""
    data = _load_json_state(OUTCOME_FILE, [])
    if not isinstance(data, list):
        data = []
    existing = {x.get("id") for x in data if isinstance(x, dict)}
    now = time.time()
    for p, sec, level in alerts:
        pair = p.get("pairAddress")
        if not pair:
            continue
        record_id = f"{p.get('chainId')}:{pair}:{int(now // 300)}"
        if record_id in existing:
            continue
        snap = _pair_snapshot(p)
        if snap["price"] <= 0 and snap["mc"] <= 0:
            continue
        rec = {
            "id": record_id,
            "pair": pair,
            "chain": p.get("chainId"),
            "token": (p.get("baseToken") or {}).get("address"),
            "symbol": (p.get("baseToken") or {}).get("symbol"),
            "level": level,
            "created_at": now,
            "entry": snap,
            "features": feature_snapshot(p),
            "outcomes": {},
            "milestones": {"2x": None, "5x": None, "10x": None},
            "rug_like": False,
            "resolved": False,
        }
        data.append(rec)
        existing.add(record_id)
    data = data[-TRACK_MAX_RECORDS:]
    _save_json_atomic(OUTCOME_FILE, data)
    return data

def _fetch_pair_now(chain, pair):
    try:
        data = get_json(f"{BASE}/latest/dex/pairs/{chain}/{pair}")
        if isinstance(data, dict):
            if isinstance(data.get("pairs"), list) and data["pairs"]:
                return data["pairs"][0]
            if isinstance(data.get("pair"), dict):
                return data["pair"]
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def update_alert_outcomes():
    """Update tracked alerts with current market data and immutable milestones."""
    data = _load_json_state(OUTCOME_FILE, [])
    if not data:
        return data
    now = time.time()
    unresolved = [r for r in data if not r.get("resolved")]
    # Oldest first, but cap network work per scan.
    unresolved = sorted(unresolved, key=lambda r: r.get("created_at", now))[:TRACK_UPDATE_LIMIT]
    by_id = {r.get("id"): r for r in data}
    for rec in unresolved:
        age_h = max(0, (now - num(rec.get("created_at"))) / 3600)
        if age_h < 0.05:
            continue
        current = _fetch_pair_now(rec.get("chain"), rec.get("pair"))
        if not current:
            continue
        snap = _pair_snapshot(current)
        rec.setdefault("samples", []).append(snap)
        entry_price = num(rec.get("entry", {}).get("price"))
        entry_mc = num(rec.get("entry", {}).get("mc"))
        current_price = snap["price"]
        current_mc = snap["mc"]
        mult_price = current_price / entry_price if entry_price > 0 else 0
        mult_mc = current_mc / entry_mc if entry_mc > 0 else 0
        mult = max(mult_price, mult_mc)

        # Fixed evaluation horizons. We record the first observation available
        # after each horizon; the scanner therefore learns from comparable T+1h,
        # T+6h, T+24h and T+7d snapshots rather than from hindsight.
        rec.setdefault("outcomes", {})
        for label, horizon in (("1h",1),("6h",6),("24h",24),("7d",168)):
            if age_h >= horizon and label not in rec["outcomes"]:
                rec["outcomes"][label] = {
                    "ts": now,
                    "price_mult": round(mult_price,4),
                    "mc_mult": round(mult_mc,4),
                    "multiple": round(mult,4),
                    "price": current_price,
                    "mc": current_mc,
                    "liq": snap["liq"],
                }

        for label, threshold in (("2x",2),("5x",5),("10x",10)):
            if rec.get("milestones", {}).get(label) is None and mult >= threshold:
                rec["milestones"][label] = now
        # Detect severe collapse as a useful outcome label, not as proof of a rug.
        if entry_liq := num(rec.get("entry", {}).get("liq")):
            if snap["liq"] > 0 and snap["liq"] < entry_liq * 0.15:
                rec["rug_like"] = True
        rec["last_update"] = now
        # Resolve at 7d or earlier if 10x was reached.
        if age_h >= 168 or rec["milestones"].get("10x") is not None:
            rec["resolved"] = True
            rec["resolved_at"] = now
    _save_json_atomic(OUTCOME_FILE, data)
    return data

def outcome_analytics(data):
    """Print robust outcome statistics and feature lifts. Never invents labels."""
    resolved = [r for r in data if r.get("resolved") and isinstance(r.get("features"), dict)]
    if not resolved:
        return {"resolved": 0}
    def rate(label):
        return sum(1 for r in resolved if r.get("milestones", {}).get(label)) / len(resolved)
    stats = {
        "resolved": len(resolved),
        "2x_rate": rate("2x"), "5x_rate": rate("5x"), "10x_rate": rate("10x"),
        "rug_like_rate": sum(bool(r.get("rug_like")) for r in resolved)/len(resolved),
    }
    # Feature bins chosen for interpretability, not curve fitting.
    bins = {
        "base_score": [(0,59),(60,69),(70,79),(80,89),(90,100)],
        "burst": [(0,1.24),(1.25,1.99),(2,2.99),(3,5.99),(6,9.99),(10,19.99),(20,999)],
        "vol_liq": [(0,1),(1,3),(3,5),(5,10),(10,15),(15,25),(25,999)],
        "buy_pressure": [(0,.49),(.50,.54),(.55,.59),(.60,.67),(.68,1)],
        "p1": [(-999,-10),(-9.99,2.99),(3,7.99),(8,19.99),(20,40),(40.01,999)],
        "age_hours": [(0,3.99),(4,11.99),(12,23.99),(24,71.99),(72,9999)],
    }
    lifts = {}
    baseline = rate("5x")
    for feature, ranges in bins.items():
        vals=[]
        for lo,hi in ranges:
            group=[r for r in resolved if lo <= num(r["features"].get(feature)) <= hi]
            if len(group) < 10:
                continue
            wr=sum(1 for r in group if r.get("milestones",{}).get("5x"))/len(group)
            lift=(wr/baseline) if baseline > 0 else 0
            vals.append({"range":[lo,hi],"n":len(group),"5x_rate":wr,"lift":lift})
        lifts[feature]=vals
    stats["lifts"]=lifts
    _save_json_atomic(ANALYTICS_FILE, stats)
    return stats

def adaptive_learning_bonus(p, data):
    """
    Small, bounded empirical adjustment. It activates only after enough resolved
    observations exist and only uses bins with >=10 observations. This prevents
    the historical dataset from becoming an overfit trading oracle.
    """
    resolved=[r for r in data if r.get("resolved") and isinstance(r.get("features"),dict)]
    if len(resolved) < LEARNING_MIN_RESOLVED:
        return 0.0, "learning-off"
    baseline=sum(1 for r in resolved if r.get("milestones",{}).get("5x"))/len(resolved)
    if baseline <= 0:
        return 0.0, "learning-off"
    f=feature_snapshot(p)
    specs={
        "base_score":[(0,59),(60,69),(70,79),(80,89),(90,100)],
        "burst":[(0,1.24),(1.25,1.99),(2,2.99),(3,5.99),(6,9.99),(10,19.99),(20,999)],
        "vol_liq":[(0,1),(1,3),(3,5),(5,10),(10,15),(15,25),(25,999)],
        "buy_pressure":[(0,.49),(.50,.54),(.55,.59),(.60,.67),(.68,1)],
        "p1":[(-999,-10),(-9.99,2.99),(3,7.99),(8,19.99),(20,40),(40.01,999)],
        "age_hours":[(0,3.99),(4,11.99),(12,23.99),(24,71.99),(72,9999)],
    }
    score=0.0; used=0
    for feature,ranges in specs.items():
        value=num(f.get(feature))
        for lo,hi in ranges:
            if not (lo <= value <= hi): continue
            group=[r for r in resolved if lo <= num(r["features"].get(feature)) <= hi]
            if len(group) < 10: break
            wr=sum(1 for r in group if r.get("milestones",{}).get("5x"))/len(group)
            lift=wr/baseline
            if ADAPTIVE_MIN_LIFT <= lift <= ADAPTIVE_MAX_LIFT:
                score += min(2.0, math.log(lift, 2))
            elif 0 < lift < (1/ADAPTIVE_MIN_LIFT):
                score -= min(2.0, math.log(1/lift, 2))
            used += 1
            break
    return max(-ADAPTIVE_BONUS_MAX,min(ADAPTIVE_BONUS_MAX,round(score,1))), f"learning-on/{used}"

def _load_positions():
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_positions(data):
    try:
        tmp = POSITIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tmp and data, f, indent=2)
        os.replace(tmp, POSITIONS_FILE)
    except Exception as e:
        print(f"[TRADING] save positions error: {e}")

def _b58encode(raw):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(bytes(raw), "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out
    pad = len(bytes(raw)) - len(bytes(raw).lstrip(b"\\x00"))
    return "1" * pad + (out or "")

def _solana_keypair():
    if not SOLANA_PRIVATE_KEY:
        raise RuntimeError("SOLANA_PRIVATE_KEY non configurata")
    from solders.keypair import Keypair
    raw = SOLANA_PRIVATE_KEY.strip()
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(ast.literal_eval(raw)))
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) == 128:
        try:
            return Keypair.from_bytes(bytes.fromhex(raw))
        except Exception:
            pass
    return Keypair.from_base58_string(raw)

def solana_rpc(method, params=None):
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params or []}
    r = session.post(SOLANA_RPC_URL, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"Solana RPC {method}: {body['error']}")
    return body.get("result")

def solana_wallet_sol(pubkey):
    result = solana_rpc("getBalance",[str(pubkey),{"commitment":"confirmed"}])
    return num((result or {}).get("value")) / 1_000_000_000

def jupiter_headers(jwt=None):
    h={"Content-Type":"application/json"}
    if JUPITER_API_KEY:
        h["x-api-key"]=JUPITER_API_KEY
    if jwt:
        h["Authorization"]=f"Bearer {jwt}"
    return h

def jupiter_quote(input_mint,output_mint,amount_atomic,slippage_bps):
    r=session.get(
        f"{JUPITER_API_BASE}/swap/v1/quote",
        params={"inputMint":input_mint,"outputMint":output_mint,"amount":str(int(amount_atomic)),"slippageBps":int(slippage_bps),"restrictIntermediateTokens":"true"},
        headers=jupiter_headers(),timeout=TIMEOUT
    )
    r.raise_for_status()
    data=r.json()
    if data.get("error"):
        raise RuntimeError(f"Jupiter quote: {data['error']}")
    return data

def jupiter_execute_swap(quote_response,wallet):
    from solders.transaction import VersionedTransaction
    r=session.post(
        f"{JUPITER_API_BASE}/swap/v1/swap",
        json={"quoteResponse":quote_response,"userPublicKey":str(wallet.pubkey()),"wrapAndUnwrapSol":True,"dynamicComputeUnitLimit":True,"dynamicSlippage":True},
        headers=jupiter_headers(),timeout=TIMEOUT
    )
    r.raise_for_status()
    data=r.json()
    swap_tx=data.get("swapTransaction")
    if not swap_tx:
        raise RuntimeError(f"Jupiter swap transaction mancante: {data}")
    tx=VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
    signed=VersionedTransaction(tx.message,[wallet])
    raw=base64.b64encode(bytes(signed)).decode("ascii")
    result=solana_rpc("sendTransaction",[raw,{"encoding":"base64","skipPreflight":False,"preflightCommitment":"confirmed","maxRetries":3}])
    if not result:
        raise RuntimeError("Solana RPC non ha restituito la signature")
    return result

def _trigger_request(method,path,jwt,payload=None):
    url=f"{JUPITER_API_BASE}/trigger/v2{path}"
    if method=="GET":
        r=session.get(url,headers=jupiter_headers(jwt),timeout=TIMEOUT)
    else:
        r=session.request(method,url,json=payload,headers=jupiter_headers(jwt),timeout=TIMEOUT)
    r.raise_for_status()
    data=r.json()
    if isinstance(data,dict) and data.get("error"):
        raise RuntimeError(f"Jupiter Trigger: {data['error']}")
    return data

def trigger_jwt(wallet):
    pubkey=str(wallet.pubkey())
    challenge=_trigger_request("POST","/auth/challenge",None,{"walletPubkey":pubkey,"type":"message"})
    message=challenge.get("challenge")
    if not message:
        raise RuntimeError(f"Jupiter Trigger challenge mancante: {challenge}")
    signature=wallet.sign_message(message.encode("utf-8"))
    verified=_trigger_request("POST","/auth/verify",None,{"type":"message","walletPubkey":pubkey,"signature":_b58encode(signature)})
    token=verified.get("token")
    if not token:
        raise RuntimeError(f"Jupiter Trigger JWT mancante: {verified}")
    return token

def trigger_sign_transaction(wallet,tx_base64):
    from solders.transaction import VersionedTransaction
    tx=VersionedTransaction.from_bytes(base64.b64decode(tx_base64))
    tx.sign([wallet])
    return base64.b64encode(bytes(tx)).decode("ascii")

def trigger_deposit(wallet,jwt,input_mint,output_mint,amount_atomic,subtype):
    try:
        _trigger_request("GET","/vault/register",jwt)
    except Exception:
        pass
    deposit=_trigger_request(
        "POST","/deposit/craft",jwt,
        {
            "inputMint":input_mint,
            "outputMint":output_mint,
            "userAddress":str(wallet.pubkey()),
            "amount":str(int(amount_atomic)),
            "orderType":"price",
            "orderSubType":subtype,
        }
    )
    signed=trigger_sign_transaction(wallet,deposit["transaction"])
    return deposit["requestId"],signed

def trigger_create_single(wallet,jwt,input_mint,output_mint,amount_atomic,condition,trigger_price):
    request_id,signed=trigger_deposit(wallet,jwt,input_mint,output_mint,amount_atomic,"single")
    payload={
        "orderType":"single",
        "depositRequestId":request_id,
        "depositSignedTx":signed,
        "userPubkey":str(wallet.pubkey()),
        "inputMint":input_mint,
        "inputAmount":str(int(amount_atomic)),
        "outputMint":output_mint,
        "triggerMint":input_mint,
        "triggerCondition":condition,
        "triggerPriceUsd":float(trigger_price),
        "slippageBps":500,
        "expiresAt":int(time.time()*1000)+30*24*60*60*1000,
    }
    return _trigger_request("POST","/orders/price",jwt,payload)

def trigger_create_oco(wallet,jwt,input_mint,output_mint,amount_atomic,entry_price,tp_price,sl_price):
    request_id,signed=trigger_deposit(wallet,jwt,input_mint,output_mint,amount_atomic,"oco")
    payload={
        "orderType":"oco",
        "depositRequestId":request_id,
        "depositSignedTx":signed,
        "userPubkey":str(wallet.pubkey()),
        "inputMint":input_mint,
        "inputAmount":str(int(amount_atomic)),
        "outputMint":output_mint,
        "triggerMint":input_mint,
        "tpPriceUsd":float(tp_price),
        "slPriceUsd":float(sl_price),
        "tpSlippageBps":500,
        "slSlippageBps":500,
        "expiresAt":int(time.time()*1000)+30*24*60*60*1000,
    }
    return _trigger_request("POST","/orders/price",jwt,payload)

def trigger_create_trailing(wallet,jwt,input_mint,output_mint,amount_atomic):
    request_id,signed=trigger_deposit(wallet,jwt,input_mint,output_mint,amount_atomic,"single")
    payload={
        "orderType":"single",
        "depositRequestId":request_id,
        "depositSignedTx":signed,
        "userPubkey":str(wallet.pubkey()),
        "inputMint":input_mint,
        "inputAmount":str(int(amount_atomic)),
        "outputMint":output_mint,
        "triggerMint":input_mint,
        "triggerCondition":"below",
        "trailingBps":int(TRAILING_STOP_PCT*10000),
        "slippageBps":500,
        "expiresAt":int(time.time()*1000)+30*24*60*60*1000,
    }
    return _trigger_request("POST","/orders/price",jwt,payload)

def trigger_update_oco_sl(jwt,order_id,sl_price):
    payload={"orderType":"oco","slPriceUsd":float(sl_price),"slSlippageBps":500}
    return _trigger_request("PATCH",f"/orders/price/{order_id}",jwt,payload)

def _slippage_for_pool(liq_usd):
    liq=num(liq_usd)
    if liq>=500_000: return 100
    if liq>=200_000: return 150
    if liq>=100_000: return 200
    if liq>=50_000: return 300
    return MAX_ENTRY_SLIPPAGE_BPS

def _sol_usd_from_pair(p):
    usd=num(p.get("priceUsd")); native=num(p.get("priceNative"))
    return usd/native if usd>0 and native>0 else 0.0

def calculate_entry_budget(p,wallet_sol,sol_usd):
    if wallet_sol<=SOL_RESERVE or sol_usd<=0:
        return {"usd":0.0,"sol":0.0,"reason":"budget SOL insufficiente"}

    available_sol=max(0.0,wallet_sol-SOL_RESERVE)
    wallet_cap_usd=available_sol*WALLET_BUDGET_FRACTION*sol_usd
    mc=num(p.get("marketCap") or p.get("fdv"))
    liq=num((p.get("liquidity") or {}).get("usd"))
    impact_cap_usd=estimate_max_position(p).get("usd",0.0)

    # ALL sizing constraints are percentages. No fixed EUR ceiling/floor.
    caps=[
        wallet_cap_usd,
        mc*MC_BUDGET_FRACTION if mc>0 else 0.0,
        liq*LIQ_BUDGET_FRACTION if liq>0 else 0.0,
        impact_cap_usd,
    ]
    positive=[x for x in caps if x>0]
    usd=min(positive) if positive else 0.0

    # Jupiter Trigger V2 requires each price order to be >= $10.
    # Since the runner is 10%, the whole position must therefore be >= $100
    # for all four exit orders to be created. This is a Jupiter constraint,
    # not a user-defined € limit.
    return {
        "usd":usd,
        "sol":usd/sol_usd,
        "reason":"OK",
        "wallet_cap_usd":wallet_cap_usd,
        "mc_cap_usd":mc*MC_BUDGET_FRACTION,
        "liq_cap_usd":liq*LIQ_BUDGET_FRACTION,
        "impact_cap_usd":impact_cap_usd,
    }

def _token_balance_atomic(pubkey,mint):
    result=solana_rpc("getTokenAccountsByOwner",[str(pubkey),{"mint":mint},{"encoding":"jsonParsed","commitment":"confirmed"}])
    total=0
    decimals=0
    for item in (result or {}).get("value",[]):
        ta=((((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("tokenAmount") or {}
        if ta.get("amount") is not None:
            total+=int(ta["amount"])
            decimals=int(ta.get("decimals") or 0)
    return total,decimals

def _order_state(order):
    return str(order.get("state") or order.get("status") or "").lower()

def _next_exit(position,wallet,jwt,current_price):
    step=int(position.get("exit_step",0))
    initial=int(position.get("initial_tokens_atomic",0))
    remaining=int(position.get("remaining_tokens_atomic",initial))
    if step>=4 or remaining<=0:
        return None

    if step==0:
        amount=int(initial*0.30)
        label="tp1"
        price=position["entry_price_usd"]*1.30
        order=trigger_create_single(wallet,jwt,position["mint"],SOL_MINT,amount,"above",price)
    elif step==1:
        amount=int(initial*0.30)
        label="tp2"
        price=position["entry_price_usd"]*1.60
        order=trigger_create_single(wallet,jwt,position["mint"],SOL_MINT,amount,"above",price)
    elif step==2:
        amount=int(initial*0.30)
        label="tp3"
        price=position["entry_price_usd"]*1.90
        order=trigger_create_single(wallet,jwt,position["mint"],SOL_MINT,amount,"above",price)
    else:
        amount=remaining
        label="runner"
        high=max(num(position.get("highest_price_usd")),current_price)
        price=high*(1.0-TRAILING_STOP_PCT)
        order=trigger_create_single(wallet,jwt,position["mint"],SOL_MINT,amount,"below",price)

    oid=order.get("id")
    if not oid:
        raise RuntimeError(f"Jupiter non ha restituito order id per {label}: {order}")
    position.setdefault("orders",{})[label]=oid
    position["active_exit_label"]=label
    position["exit_step"]=step+1
    return order

def execute_entry(p):
    if not AUTO_TRADE_ENABLED:
        return None

    wallet=_solana_keypair()
    pubkey=wallet.pubkey()
    wallet_sol=solana_wallet_sol(pubkey)
    sol_usd=_sol_usd_from_pair(p)
    sizing=calculate_entry_budget(p,wallet_sol,sol_usd)
    if sizing.get("reason")!="OK" or sizing.get("usd",0)<=0:
        print(f"[TRADING] NO BUY {p.get('baseToken',{}).get('symbol','?')}: {sizing.get('reason')}")
        return None

    mint=(p.get("baseToken") or {}).get("address")
    if not mint:
        return None

    lamports=int(sizing["sol"]*1_000_000_000)
    quote=jupiter_quote(SOL_MINT,mint,lamports,_slippage_for_pool((p.get("liquidity") or {}).get("usd")))
    impact=num(quote.get("priceImpactPct"))
    if impact>MAX_EXECUTION_PRICE_IMPACT:
        print(f"[TRADING] NO BUY: Jupiter impact {impact*100:.2f}%")
        return None

    before_sol=solana_wallet_sol(pubkey)
    signature=jupiter_execute_swap(quote,wallet)
    time.sleep(2)
    after_sol=solana_wallet_sol(pubkey)
    spent_lamports=max(0,int((before_sol-after_sol)*1_000_000_000))
    actual_entry_sol=spent_lamports/1_000_000_000 if spent_lamports>0 else lamports
    actual_entry_usd=actual_entry_sol*sol_usd
    entry_price=num(p.get("priceUsd"))
    if entry_price<=0:
        raise RuntimeError("Prezzo token non disponibile")

    jwt=trigger_jwt(wallet)
    received_atomic,decimals=_token_balance_atomic(pubkey,mint)
    if received_atomic<=0:
        raise RuntimeError("BUY eseguito ma saldo token non rilevato")

    position={
        "mint":mint,
        "symbol":(p.get("baseToken") or {}).get("symbol","?"),
        "name":(p.get("baseToken") or {}).get("name","?"),
        "entry_ts":time.time(),
        "entry_signature":signature,
        "entry_sol":actual_entry_sol,
        "entry_usd":actual_entry_usd,
        "entry_price_usd":entry_price,
        "initial_tokens_atomic":received_atomic,
        "remaining_tokens_atomic":received_atomic,
        "token_decimals":decimals,
        "exit_step":0,
        "orders":{},
        "highest_price_usd":entry_price,
    }

    _next_exit(position,wallet,jwt,entry_price)

    positions=_load_positions()
    positions[mint]=position
    _save_positions(positions)
    print(f"[TRADING] BUY {position['symbol']} | Jupiter TP1 armed | tx {signature}")
    return position

def manage_open_positions():
    if not AUTO_TRADE_ENABLED or not SOLANA_PRIVATE_KEY:
        return

    positions=_load_positions()
    if not positions:
        return

    wallet=_solana_keypair()
    jwt=trigger_jwt(wallet)

    for mint,position in list(positions.items()):
        try:
            data=get_json(f"{BASE}/latest/dex/tokens/{mint}")
            pairs=data.get("pairs") or []
            if not pairs:
                continue
            pair=max(pairs,key=lambda x:num((x.get("liquidity") or {}).get("usd")))
            price=num(pair.get("priceUsd"))
            if price>0:
                position["highest_price_usd"]=max(num(position.get("highest_price_usd")),price)

            history=_trigger_request("GET","/orders/history?mint="+mint+"&limit=50",jwt)
            orders=history.get("orders") or []
            active=[o for o in orders if _order_state(o)=="active"]

            label=position.get("active_exit_label")
            active_id=(position.get("orders") or {}).get(label) if label else None
            current=next((o for o in orders if str(o.get("id"))==str(active_id)),None)

            if current and _order_state(current) in {"filled","executed","completed","success"}:
                remaining,decimals=_token_balance_atomic(wallet.pubkey(),mint)
                position["remaining_tokens_atomic"]=remaining
                if remaining<=0:
                    position["closed"]=True
                    position["closed_ts"]=time.time()
                    continue
                _next_exit(position,wallet,jwt,price)

            elif label=="runner" and current and _order_state(current)=="active" and price>0:
                new_trigger=position["highest_price_usd"]*(1.0-TRAILING_STOP_PCT)
                old_trigger=num(current.get("triggerPriceUsd"))
                if old_trigger<=0 or new_trigger>old_trigger*1.001:
                    _trigger_request("PATCH",f"/orders/price/{active_id}",jwt,{"triggerPriceUsd":float(new_trigger)})

            # Safety: never create a second exit while Jupiter still has one active.
            if len(active)>1:
                print(f"[TRADING] WARNING {mint}: Jupiter reports {len(active)} active exit orders")

        except Exception as e:
            print(f"[TRADING] Jupiter order-management error {position.get('symbol','?')}: {e}")

    _save_positions(positions)


def auto_trade_best_candidate(rows):
    if not AUTO_TRADE_ENABLED or not SOLANA_PRIVATE_KEY:
        return

    positions=_load_positions()
    if any(not p.get("closed") for p in positions.values()):
        print("[TRADING] posizione aperta: nessun nuovo BUY")
        return

    eligible=[]
    for p in rows:
        sec=p.get("_security_result") or {}
        if sec.get("status")!="PASS":
            continue
        if num(sec.get("quality_penalty"))>=12:
            continue

        score=num(p.get("_final_score"))
        burst=num(p.get("_burst"))
        accel=num(p.get("_accel"))
        has_accel=bool(p.get("_has_accel_history"))
        p1=num((p.get("priceChange") or {}).get("h1"))
        p24=num((p.get("priceChange") or {}).get("h24"))
        liq=num((p.get("liquidity") or {}).get("usd"))
        mc=num(p.get("marketCap") or p.get("fdv"))

        # Automatic BUY requires an actual acceleration signal; score alone
        # is never sufficient. This is deliberately tighter than Telegram.
        acceleration_ok=(has_accel and accel>=1.15) or burst>=2.0
        if not acceleration_ok:
            continue
        if score<75:
            continue
        if p1>ALERT_MAX_P1 or p24>ALERT_MAX_P24:
            continue
        if p1<=-30 or p24<=-50:
            continue
        if liq<10_000 or mc<MIN_MC or mc>MAX_MC:
            continue

        tx=(p.get("txns") or {}).get("h1") or {}
        buys=num(tx.get("buys")); sells=num(tx.get("sells"))
        total=buys+sells
        buy_pressure=buys/total if total else 0
        if buy_pressure<0.55:
            continue

        eligible.append(p)

    if not eligible:
        print("[TRADING] Nessun candidato con accelerazione sufficiente per BUY automatico.")
        return

    best=max(
        eligible,
        key=lambda x: (
            num(x.get("_final_score")),
            num(x.get("_accel")),
            num(x.get("_burst")),
            num(x.get("_smart_bonus"))
        )
    )
    print(
        f"[TRADING] BUY trigger: {best.get('baseToken',{}).get('symbol','?')} | "
        f"score={best.get('_final_score')} | burst={num(best.get('_burst')):.2f}x | "
        f"accel={num(best.get('_accel')):.2f}x"
    )
    execute_entry(best)


def main():
    enable_ansi()
    manage_open_positions()
    print(c("═"*80, CYAN))
    print(c("  MEMECOIN SCANNER V11", CYAN, True))
    print(c("  SOLANA  +  SMART WALLET  +  EARLY SCORE  +  LEARNING  +  GOPLUS  +  JUPITER", WHITE))
    print(c("═"*80, CYAN))

    print(c("\n[1/2] Scansione mercato Solana corrente...", BLUE, True))

    rows=discover()
    if not rows:
        print("Nessun candidato.")
        input("\nPremi INVIO per chiudere...")
        return

    evidence, wallets = update_smart_wallets(rows)

    # Update previously registered alerts before using historical learning.
    tracked_data = update_alert_outcomes()
    analytics = outcome_analytics(tracked_data)
    if analytics.get("resolved"):
        print(
            f"[LEARNING] {analytics['resolved']} alert risolti | "
            f"2x {analytics['2x_rate']*100:.1f}% | "
            f"5x {analytics['5x_rate']*100:.1f}% | "
            f"10x {analytics['10x_rate']*100:.1f}% | "
            f"rug-like {analytics['rug_like_rate']*100:.1f}%"
        )
    else:
        print(f"[LEARNING] Dataset in costruzione: servono almeno {LEARNING_MIN_RESOLVED} alert risolti.")

    if analytics.get("lifts"):
        for feature, entries in analytics["lifts"].items():
            best = max(entries, key=lambda x: x.get("lift", 0), default=None)
            if best and best.get("lift", 0) >= ADAPTIVE_MIN_LIFT:
                lo, hi = best["range"]
                print(
                    f"[LEARNING] {feature}: range {lo}–{hi} | "
                    f"n={best['n']} | 5x={best['5x_rate']*100:.1f}% | "
                    f"lift={best['lift']:.2f}x"
                )

    for p in rows:
        bonus, ranked = smart_bonus(p, evidence)
        p["_smart_bonus"]=bonus
        p["_smart_ranked"]=ranked
        adaptive, learning_state = adaptive_learning_bonus(p, tracked_data)
        p["_adaptive_bonus"] = adaptive
        p["_learning_state"] = learning_state
        p["_final_score"]=min(100,max(0,round(p["_score"]+bonus+adaptive)))

    rows.sort(key=lambda x:(x["_final_score"],x["_accel"]),reverse=True)

    for i,p in enumerate(rows,1):
        sec=security(p)
        p["_security_result"] = sec
        p["_security_status"] = sec.get("status", "UNVERIFIED")
        quality_penalty = num(sec.get("quality_penalty"))
        if quality_penalty:
            p["_final_score"] = max(0, p["_final_score"] - round(quality_penalty))
        print(c(
            f"\n#{i}  EARLY FINAL  {p['_final_score']}/100  •  "
            f"SMART +{p['_smart_bonus']} • LEARN {p.get('_adaptive_bonus',0):+.1f}",
            score_color(p['_final_score']), True
        ))
        print(fmt(p,sec,p["_smart_bonus"],p["_smart_ranked"]))
        if sec.get("quality_flags"):
            print("  Structural risk:", " | ".join(sec["quality_flags"]))

    # TELEGRAM ALERT ENGINE
    # Tre livelli. La Security viene riutilizzata da p["_security_result"]
    # per evitare seconde chiamate GoPlus.
    interesting = []
    strong = []
    smart = []

    for p in rows:
        burst = num(p.get("_burst", 0))
        accel = num(p.get("_accel", 0))
        has_accel = bool(p.get("_has_accel_history"))
        p1 = num((p.get("priceChange") or {}).get("h1"))
        p24 = num((p.get("priceChange") or {}).get("h24"))

        tx = (p.get("txns") or {}).get("h1") or {}
        buys = num(tx.get("buys"))
        sells = num(tx.get("sells"))
        total = buys + sells
        buy_pressure = (100 * buys / total) if total else 0.0

        sec = p.get("_security_result") or {}
        sec_status = sec.get("status", "UNVERIFIED")

        print(
            f"[Telegram Check] {p.get('baseToken', {}).get('symbol', '?')} | "
            f"Score={p.get('_final_score', 0)} | Burst={burst:.1f}x | "
            f"Accel={accel:.1f}x" + (" (history)" if has_accel else "") +
            f" | Security={sec_status} | 1h={p1:+.1f}% | "
            f"24h={p24:+.1f}% | Buy={buy_pressure:.1f}%"
        )

        if sec_status != "PASS":
            print("  → NO ALERT: Security non PASS")
            continue

        quality_penalty = num(sec.get("quality_penalty"))
        if quality_penalty >= 12:
            print("  → NO ALERT: concentrazione/struttura troppo rischiosa")
            continue

        # Evitiamo token già esplosi. Un +60% nell'ora o +150% nelle 24h
        # è il limite massimo per l'alert automatico.
        if p1 > ALERT_MAX_P1 or p24 > ALERT_MAX_P24:
            print("  → NO ALERT: token troppo esteso")
            continue

        # Evitiamo anche setup già in forte deterioramento.
        # Un burst enorme non deve compensare un crollo già in corso.
        if p1 <= -30 or p24 <= -50:
            print("  → NO ALERT: momentum fortemente negativo")
            continue

        # Conferma indipendente per i livelli alti:
        # 1) accelerazione osservata su più scansioni, oppure
        # 2) smart-wallet evidence qualificata, oppure
        # 3) struttura volume/liquidità non eccessivamente tirata.
        strong_confirmation = (
            (has_accel and accel >= 1.15)
            or p["_smart_bonus"] >= 4
            or p.get("_liq_ratio", 0) <= 10
        )

        # 🧠 SMART: livello più alto.
        if (
            p["_final_score"] >= 75
            and (burst >= 2.0 or (has_accel and accel >= 1.15))
            and p["_smart_bonus"] >= 4
            and strong_confirmation
        ):
            print("  → 🧠 SMART")
            smart.append((p, sec, "SMART"))
            continue

        # 🔥 STRONG: buon punteggio + forte attività.
        if (
            p["_final_score"] >= 75
            and (burst >= 2.0 or (has_accel and accel >= 1.15))
            and strong_confirmation
        ):
            print("  → 🔥 STRONG")
            strong.append((p, sec, "STRONG"))
            continue

        # 🟢 INTERESTING: rete più larga.
        if (
            (p["_final_score"] >= 60 and burst >= 1.5)
            or
            (p["_final_score"] >= 65 and buy_pressure >= 55)
        ):
            print("  → 🟢 INTERESTING")
            interesting.append((p, sec, "INTERESTING"))
        else:
            print("  → NO ALERT: criteri non raggiunti")

    # Un solo alert per token; il livello più alto prevale.
    alerts_to_send = {}
    for p, sec, level in interesting:
        alerts_to_send[p.get("pairAddress")] = (p, sec, level)
    for p, sec, level in strong:
        alerts_to_send[p.get("pairAddress")] = (p, sec, level)
    for p, sec, level in smart:
        alerts_to_send[p.get("pairAddress")] = (p, sec, level)

    if alerts_to_send:
        # Persist the exact T0 snapshot before sending. The outcome engine later
        # measures 1h/6h/24h/7d and 2x/5x/10x from this immutable entry.
        register_alert_outcomes(list(alerts_to_send.values()))
        print(f"\n[Telegram] {len(alerts_to_send)} candidato/i selezionato/i.")

        for pair_address, (p, sec, level) in sorted(
            alerts_to_send.items(),
            key=lambda item: (
                item[1][0].get("_final_score", 0),
                item[1][0].get("_burst", 0)
            ),
            reverse=True
        ):
            if level == "SMART":
                title = "🧠 SMART WALLET"
            elif level == "STRONG":
                title = "🔥 STRONG"
            else:
                title = "🟢 INTERESTING"

            print(c("\n" + "═"*80, GREEN, True))
            print(c(f"  {title}", GREEN, True))
            print(fmt(p, sec, p["_smart_bonus"], p["_smart_ranked"]))
            print(c("═"*80, GREEN, True))

            alert_text = build_telegram_alert(
                p,
                sec,
                p["_smart_bonus"],
                p["_smart_ranked"],
                alert_level=level
            )

            print(
                f"[Telegram DEBUG] Preparazione invio | "
                f"level={level} | symbol={p.get('baseToken', {}).get('symbol', '?')} | "
                f"chat_id_present={bool(TELEGRAM_CHAT_ID)} | "
                f"token_present={bool(TELEGRAM_BOT_TOKEN)} | "
                f"text_len={len(alert_text)}"
            )

            print(f"[Telegram] Invio {level} → {p.get('baseToken', {}).get('symbol', '?')}")
            ok = telegram_send(alert_text, pair_address=pair_address)

            if not ok:
                print("[Telegram] FALLITO: controlla il debug HTTP/API sopra.")
    else:
        print("\nNessun alert Telegram in questa scansione.")

    auto_trade_best_candidate(rows)

if __name__=="__main__":
    try:
        main()
    except Exception:
        print("\nERRORE NON GESTITO:\n")
        traceback.print_exc()
        print("\nIl terminale restera' aperto per permetterti di leggere l'errore.")
        input("\nPremi INVIO per chiudere...")
