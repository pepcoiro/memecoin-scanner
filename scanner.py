
import os, json, time, traceback, requests
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

WATCH_CHAINS = {
    "ethereum", "bsc", "solana", "base", "arbitrum", "polygon",
    "avalanche", "optimism", "linea", "zksync", "monad"
}

# V3: smart-wallet discovery currently uses Alchemy on EVM chains.
# Solana remains supported by the market scanner; wallet intelligence for Solana
# will be added with Helius in the next module.
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

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", _CFG.get("ALCHEMY_API_KEY", "")).strip()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", _CFG.get("HELIUS_API_KEY", "")).strip()
GOPLUS_APP_KEY = os.getenv("GOPLUS_APP_KEY", _CFG.get("GOPLUS_APP_KEY", "")).strip()
GOPLUS_APP_SECRET = os.getenv("GOPLUS_APP_SECRET", _CFG.get("GOPLUS_APP_SECRET", "")).strip()
GOPLUS_ACCESS_TOKEN = None
GOPLUS_ACCESS_EXPIRES = 0
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", _CFG.get("TELEGRAM_BOT_TOKEN", "")).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", _CFG.get("TELEGRAM_CHAT_ID", "")).strip()

MIN_LIQ = 20_000
MAX_MC = 10_000_000
MIN_MC = 20_000
MIN_VOL_24H = 20_000
TOP_N = 15

SMART_WALLET_BONUS_MAX = 22
EARLY_WINDOW_MIN = 60
MAX_WALLETS_PER_TOKEN = 30
MIN_WALLET_WINS = 2
WALLET_PENDING_HOURS = 48
MAX_ENTRY_P1_FOR_SMART = 60
MAX_ENTRY_P24_FOR_SMART = 150


session = requests.Session()
session.headers.update({"User-Agent": "MemecoinScanner/3.0"})

ALCHEMY_HOSTS = {
    "ethereum": "eth-mainnet.g.alchemy.com",
    "base": "base-mainnet.g.alchemy.com",
    "arbitrum": "arb-mainnet.g.alchemy.com",
    "polygon": "polygon-mainnet.g.alchemy.com",
    "optimism": "opt-mainnet.g.alchemy.com",
    "bsc": "bnb-mainnet.g.alchemy.com",
    "avalanche": "avax-mainnet.g.alchemy.com",
    "linea": "linea-mainnet.g.alchemy.com",
}

# Approximate block times, only used to choose a recent starting block.
BLOCK_SECONDS = {
    "ethereum": 12,
    "base": 2,
    "arbitrum": 1,
    "polygon": 2,
    "optimism": 2,
    "bsc": 1.5,
    "avalanche": 2,
    "linea": 3,
}

def num(x):
    try:
        return float(x or 0)
    except:
        return 0.0

def get_json(url, **kwargs):
    r = session.get(url, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()

def age_hours(p):
    c = p.get("pairCreatedAt")
    if not c:
        return 999999
    return max(0, (time.time()*1000-c)/3600000)

def raw_score(p, previous=None):
    """V8 EARLY SCORE.
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

    # 4) VOLUME BURST — new in V8.
    if burst >= 10: s += 15
    elif burst >= 6: s += 12
    elif burst >= 3: s += 9
    elif burst >= 2: s += 6
    elif burst >= 1.25: s += 3

    # Extremely high volume relative to liquidity is not automatically bullish:
    # it can be bot/wash-trading activity or a dangerously thin pool.
    liq_ratio = (v1 / liq) if liq > 0 else 0
    if liq_ratio >= 50: s -= 15
    elif liq_ratio >= 25: s -= 10
    elif liq_ratio >= 12: s -= 5

    # 5) BUY PRESSURE
    total = buys+sells
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

def alchemy_rpc(chain, method, params):
    host = ALCHEMY_HOSTS.get(chain)
    if not host or not ALCHEMY_API_KEY:
        return None
    url = f"https://{host}/v2/{ALCHEMY_API_KEY}"
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    try:
        r = session.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("error"):
            return None
        return data.get("result")
    except:
        return None

def hex_block(n):
    return hex(max(0, int(n)))

def early_buyers(p):
    """Find wallets receiving the token directly from the DEX pair during its first hour."""
    if not ALCHEMY_API_KEY:
        return []

    chain = str(p.get("chainId","")).lower()
    if chain not in ALCHEMY_HOSTS:
        return []

    token = (p.get("baseToken") or {}).get("address")
    pair = p.get("pairAddress")
    created_ms = p.get("pairCreatedAt")
    if not token or not pair or not created_ms:
        return []

    age = age_hours(p)
    # Only inspect young-ish tokens. Older tokens need a historical indexer/window
    # and would be too expensive to scan indiscriminately.
    if age > 72:
        return []

    block_sec = BLOCK_SECONDS.get(chain, 3)
    latest_hex = alchemy_rpc(chain, "eth_blockNumber", [])
    if not latest_hex:
        return []

    latest = int(latest_hex, 16)
    lookback = int((age * 3600 + 3600) / block_sec)
    start = max(0, latest - lookback)

    params = [{
        "fromBlock": hex_block(start),
        "toBlock": "latest",
        "contractAddresses": [token],
        "category": ["erc20"],
        "withMetadata": True,
        "excludeZeroValue": True,
        "maxCount": "0x3e8"
    }]

    result = alchemy_rpc(chain, "alchemy_getAssetTransfers", params)
    if not result:
        return []

    transfers = result.get("transfers", [])
    buyers = []
    seen = set()
    end_ms = created_ms + EARLY_WINDOW_MIN*60*1000

    for t in transfers:
        frm = (t.get("from") or "").lower()
        to = (t.get("to") or "").lower()
        if frm != str(pair).lower() or not to:
            continue
        meta = t.get("metadata") or {}
        ts = meta.get("blockTimestamp")
        if ts:
            try:
                tms = int(float(ts)*1000) if isinstance(ts,(int,float)) else int(ts.replace("Z","+00:00").replace("T"," ").split("+")[0].replace("-",""))
            except:
                tms = None
        else:
            tms = None

        # If metadata parsing is unavailable, keep the transfer only for very young
        # pairs; Alchemy's indexed ordering still gives a useful approximation.
        if tms is not None and not (created_ms <= tms <= end_ms):
            continue

        wallet = to
        if wallet in seen:
            continue
        # Ignore obvious burn/zero addresses.
        if wallet in {
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dead"
        }:
            continue
        seen.add(wallet)
        buyers.append(wallet)
        if len(buyers) >= MAX_WALLETS_PER_TOKEN:
            break

    return buyers

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
    """V9 Smart Wallet Engine.
    Wallet calls are first stored as PENDING at the observed entry MC. They only
    become wins after the token later reaches 2x. This prevents the old V8 bug
    where a wallet could be labelled smart merely because the token was already
    pumping when we observed it.
    """
    wallets=load_wallets(); evidence={}

    for p in rows:
        chain=str(p.get("chainId","")).lower()
        if chain in ALCHEMY_HOSTS and ALCHEMY_API_KEY:
            buyers=early_buyers(p)
            token=(p.get("baseToken") or {}).get("address")
            if buyers and token:
                for w in buyers:
                    _update_wallet_record(wallets,chain,w,token,p)
                evidence[p.get("pairAddress")]=rank_wallets_for_token(wallets,chain,buyers)

    if HELIUS_API_KEY:
        for p in rows:
            if str(p.get("chainId","")).lower()!="solana": continue
            buyers=solana_early_buyers(p)
            mint=(p.get("baseToken") or {}).get("address")
            if not buyers or not mint: continue
            for w in buyers:
                _update_wallet_record(wallets,"solana",w,mint,p)
            evidence[p.get("pairAddress")]=rank_wallets_for_token(wallets,"solana",buyers)

    save_wallets(wallets)
    return evidence,wallets

def smart_bonus(p,evidence):
    ranked=evidence.get(p.get("pairAddress"),[])
    if not ranked: return 0,[]
    bonus=0
    for _,wallet,wins,calls,wr,early_rate,avg_peak,resolved in ranked[:3]:
        # Require actual resolved wins. Multiple independent strong wallets stack.
        per=min(8.0, 1.0 + wins*0.9 + wr*3.0 + early_rate*1.5)
        if resolved < 2: per *= 0.55
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

def goplus_evm(chain_id, address):
    if not chain_id or not address:
        return None
    result = goplus_request(
        f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
        {"contract_addresses": address}
    )
    return _extract_goplus_token(result, address)

def goplus_solana(address):
    if not address:
        return None
    result = goplus_request(
        "https://api.gopluslabs.io/api/v1/solana/token_security",
        {"contract_addresses": address}
    )
    return _extract_goplus_token(result, address)


def security(p):
    chain = str(p.get("chainId","")).lower()
    address = (p.get("baseToken") or {}).get("address")
    if not address:
        return {"status":"UNKNOWN","reason":"contract missing"}

    chain_map = {
        "ethereum":"1", "bsc":"56", "polygon":"137", "arbitrum":"42161",
        "optimism":"10", "base":"8453", "avalanche":"43114",
        "linea":"59144", "zksync":"324", "monad":"143"
    }

    if chain == "solana":
        data = goplus_solana(address)
    elif chain in chain_map:
        data = goplus_evm(chain_map[chain], address)
    else:
        data = None

    if not data:
        return {"status":"UNVERIFIED", "reason":"GoPlus: nessun dato per questo token"}

    bad = []
    for k,label in [
        ("is_honeypot","honeypot"), ("cannot_sell","cannot_sell"),
        ("blacklist","blacklist"), ("is_mintable","mintable"),
        ("transfer_pausable","transfer_pausable")
    ]:
        if str(data.get(k,"0")) == "1":
            bad.append(label)

    for k,label in [("buy_tax","buy_tax"), ("sell_tax","sell_tax")]:
        try:
            if float(data.get(k,0) or 0) > 0.10:
                bad.append(label)
        except Exception:
            pass

    return {"status":"RISK" if bad else "PASS", "bad":bad, "raw":data}


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
        f"Volume 1h: ${v1:,.0f} | 24h: ${v24:,.0f}\n"
        f"Buy/Sell 1h: {buys}/{sells} ({bp:.1f}% buy)\n"
        f"Price: 1h {num(pc.get('h1')):+.1f}% | 6h {num(pc.get('h6')):+.1f}% | 24h {num(pc.get('h24')):+.1f}%\n"
        f"{wallet_line}\n"
        f"Security: {sec_text} {sec.get('bad','')}\n"
        f"DEX: {p.get('url')}"
    )


GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {"Accept": "application/json;version=20230203"}

# GeckoTerminal uses its own network slugs.
GECKO_NETWORKS = {
    "ethereum":"eth", "bsc":"bsc", "solana":"solana", "base":"base",
    "arbitrum":"arbitrum", "polygon":"polygon_pos", "avalanche":"avax",
    "optimism":"optimism", "linea":"linea", "zksync":"zksync",
}

def gecko_get(path, params=None):
    try:
        r = session.get(GECKO + path, params=params, headers=GECKO_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None

def historical_ohlcv(chain, pool, limit=168):
    network = GECKO_NETWORKS.get(chain)
    if not network:
        return []
    data = gecko_get(
        f"/networks/{network}/pools/{pool}/ohlcv/hour",
        {"aggregate":1, "limit":limit, "currency":"usd"}
    )
    try:
        return data["data"]["attributes"]["ohlcv_list"]
    except:
        return []

def historical_multiple():
    """
    Seed the wallet database from recent pools whose price history contains
    a large expansion. GeckoTerminal exposes on-chain OHLCV for pools, so this
    gives us a defensible historical winner filter without pretending we have
    an all-time database of every dead memecoin.
    """
    winners = []
    # Keep this deliberately small because the public GeckoTerminal API is
    # rate-limited. We use the current new/trending pools as the search universe.
    for chain in WATCH_CHAINS:
        network = GECKO_NETWORKS.get(chain)
        if not network:
            continue

        for endpoint in ("new_pools", "trending_pools"):
            params = {"page":1}
            if endpoint == "trending_pools":
                params["duration"] = "24h"
            data = gecko_get(f"/networks/{network}/{endpoint}", params)
            if not data:
                continue

            for item in (data.get("data") or [])[:10]:
                attr = item.get("attributes") or {}
                pool = attr.get("address")
                created = attr.get("pool_created_at")
                if not pool:
                    continue

                candles = historical_ohlcv(chain, pool, 168)
                if len(candles) < 6:
                    continue

                # GeckoTerminal normally returns newest-first.
                candles = sorted(candles, key=lambda x:x[0])
                first = float(candles[0][1] or 0)
                if first <= 0:
                    continue
                peak = max(float(c[2] or 0) for c in candles)
                current = float(candles[-1][4] or 0)
                peak_mult = peak/first if first else 0
                current_mult = current/first if first else 0

                if peak_mult >= 5:
                    winners.append({
                        "chain":chain,
                        "pool":pool,
                        "created":created,
                        "first":first,
                        "peak":peak,
                        "current":current,
                        "peak_mult":peak_mult,
                        "current_mult":current_mult,
                    })

    # Best historical expansions first.
    winners.sort(key=lambda x:x["peak_mult"], reverse=True)
    # De-duplicate pool addresses.
    seen=set()
    out=[]
    for w in winners:
        key=(w["chain"],w["pool"])
        if key in seen: continue
        seen.add(key)
        out.append(w)
        if len(out)>=20: break
    return out

def seed_evm_wallets_from_pool(chain, pool, token=None):
    """
    Historical seed for EVM. We ask Alchemy for ERC-20 transfers involving
    the pool over its indexed history. The pool-to-user transfers of the
    candidate token are used as early-holder candidates.
    """
    if not ALCHEMY_API_KEY or chain not in ALCHEMY_HOSTS:
        return []

    latest_hex = alchemy_rpc(chain, "eth_blockNumber", [])
    if not latest_hex:
        return []
    latest = int(latest_hex,16)

    # Use a bounded historical window (~30 days) to keep the hunter practical.
    secs = 30*24*3600
    block_sec = BLOCK_SECONDS.get(chain, 3)
    start = max(0, latest-int(secs/block_sec))

    contracts = [token] if token else None
    params=[{
        "fromBlock":hex_block(start),
        "toBlock":"latest",
        "fromAddress":pool,
        "contractAddresses":contracts,
        "category":["erc20"],
        "withMetadata":True,
        "excludeZeroValue":True,
        "maxCount":"0x3e8"
    }]
    result=alchemy_rpc(chain,"alchemy_getAssetTransfers",params)
    if not result:
        return []

    wallets=[]
    seen=set()
    for t in result.get("transfers",[]):
        to=(t.get("to") or "").lower()
        if not to or to in seen:
            continue
        if to in {
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dead"
        }:
            continue
        seen.add(to)
        wallets.append(to)
        if len(wallets)>=MAX_WALLETS_PER_TOKEN:
            break
    return wallets

def seed_historical_wallets(winners):
    """
    Build a seed reputation using recent 5x+ pools.
    This is a learning phase: it does not call these wallets 'smart' until
    they repeat across multiple independent winners.
    """
    wallets=load_wallets()
    seeded=0

    for w in winners:
        chain=w["chain"]
        pool=w["pool"]
        buyers=[]

        if chain=="solana" and HELIUS_API_KEY:
            # Helius can parse address history for the pool.
            txs=helius_transactions(pool, 1000)
            for tx in txs:
                if tx.get("transactionError"):
                    continue
                fee=(tx.get("feePayer") or "").strip()
                if fee:
                    buyers.append(fee)
                if len(buyers)>=MAX_WALLETS_PER_TOKEN:
                    break
        elif chain in ALCHEMY_HOSTS and ALCHEMY_API_KEY:
            buyers=seed_evm_wallets_from_pool(chain,pool)

        if not buyers:
            continue

        # Weight only pools that really expanded.  A 10x peak is stronger
        # evidence than a 5x peak.
        mult=w["peak_mult"]
        success_weight=2 if mult>=10 else 1

        for wallet in buyers:
            key=f"{chain}:{wallet}"
            rec=wallets.setdefault(key,{
                "chain":chain,"wallet":wallet,
                "calls":0,"wins":0,"tokens":[],
                "peak_mults":[],"seeded":True,"last_seen":0
            })
            token_id=f"pool:{pool}"
            if token_id in rec.get("tokens",[]):
                continue
            rec["calls"]+=1
            rec["wins"]+=success_weight
            rec.setdefault("peak_mults",[]).append(round(mult,2))
            rec["tokens"]=(rec.get("tokens",[])+[token_id])[-100:]
            rec["last_seen"]=time.time()
            seeded+=1

    save_wallets(wallets)
    return wallets, seeded

def wallet_report(wallets):
    ranked=[]
    for key,rec in wallets.items():
        calls=int(rec.get("calls",0)); wins=int(rec.get("wins",0)); losses=int(rec.get("losses",0))
        resolved=wins+losses
        if calls<2 or wins<2: continue
        wr=wins/resolved if resolved else 0
        early=int(rec.get("early_entries",0)); late=int(rec.get("late_entries",0))
        early_rate=early/max(1,early+late)
        mults=rec.get("peak_mults",[])
        avg=sum(mults)/len(mults) if mults else 0
        score=min(100,round(wr*55 + min(early_rate,1)*25 + min(avg,10)*2 + min(wins,10)*2))
        ranked.append((score,rec.get("chain"),rec.get("wallet"),calls,wins,losses,wr,early_rate,avg))
    ranked.sort(reverse=True)
    return ranked[:30]

def wallet_stats(wallets):
    calls=sum(int(r.get("calls",0)) for r in wallets.values())
    wins=sum(int(r.get("wins",0)) for r in wallets.values())
    pending=sum(len(r.get("pending",[])) for r in wallets.values())
    return calls,wins,pending

def _load_telegram_alerts():
    try:
        with open(TELEGRAM_ALERT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_telegram_alerts(data):
    try:
        tmp = TELEGRAM_ALERT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TELEGRAM_ALERT_FILE)
    except Exception:
        pass


def telegram_send(text, pair_address=None):
    """Send a Telegram alert. Duplicate alerts for the same pair are suppressed for 60 minutes."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    alerts = _load_telegram_alerts()
    now = time.time()
    if pair_address:
        last = num(alerts.get(pair_address))
        if last and now - last < 3600:
            return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        r = session.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"[Telegram] HTTP {r.status_code}: {r.text[:200]}")
            return False
        body = r.json()
        if not body.get("ok"):
            print(f"[Telegram] API error: {body}")
            return False
        if pair_address:
            alerts[pair_address] = now
            # Keep the state small.
            cutoff = now - 7 * 86400
            alerts = {k: v for k, v in alerts.items() if num(v) >= cutoff}
            _save_telegram_alerts(alerts)
        print("[Telegram] Alert inviato.")
        return True
    except Exception as e:
        print(f"[Telegram] errore: {e}")
        return False


def build_telegram_alert(p, sec, smart_bonus_value, ranked):
    b = p.get("baseToken") or {}
    liq = num((p.get("liquidity") or {}).get("usd"))
    mc = num(p.get("marketCap") or p.get("fdv"))
    v1 = num((p.get("volume") or {}).get("h1"))
    tx = (p.get("txns") or {}).get("h1") or {}
    buys = int(num(tx.get("buys"))); sells = int(num(tx.get("sells")))
    bp = 100 * buys / (buys + sells) if buys + sells else 0
    pc = p.get("priceChange") or {}
    accel = f"{p.get('_accel', 0):.1f}x" if p.get("_has_accel_history") else "N/D"

    lines = [
        "🚨 MEMECOIN SCANNER — FORTE OPPORTUNITÀ",
        "",
        f"🪙 {b.get('symbol','?')} — {b.get('name','?')}",
        f"⛓️ {p.get('chainId')} / {p.get('dexId')}",
        "",
        f"🎯 EARLY SCORE: {p.get('_final_score', 0)}/100",
        f"⚡ BURST: {p.get('_burst', 0):.1f}x",
        f"📈 ACCEL: {accel}",
        f"📊 BUY PRESSURE: {bp:.1f}%",
        f"💰 MC: ${mc:,.0f}",
        f"💧 LIQUIDITY: ${liq:,.0f}",
        f"🔥 VOLUME 1H: ${v1:,.0f}",
        f"📈 PRICE 1H: {num(pc.get('h1')):+.1f}%",
        "",
        f"🧠 SMART WALLET BONUS: +{smart_bonus_value}",
    ]

    if ranked:
        lines.append(f"👛 Wallet qualificati: {len(ranked)}")
        for _, wallet, wins, calls, wr, early_rate, avg_peak, resolved in ranked[:3]:
            lines.append(f"  • {wallet[:8]}…{wallet[-6:]} — {wins}W/{calls} call | WR {wr*100:.0f}% | early {early_rate*100:.0f}%")
    else:
        lines.append("👛 Smart wallet: nessuno")

    lines += [
        "",
        "🛡️ SECURITY: PASS",
        "",
        f"🔗 {p.get('url')}",
    ]
    return "\n".join(lines)


def print_trade_plan():
    # Piano già pronto da replicare nella sezione Exit Strategy di Terminal.
    # TP: percentuale di profitto + percentuale Amount.
    # SL: -20% con Amount 100% per chiudere il residuo della posizione.
    print(c("\n  TERMINAL — EXIT STRATEGY", CYAN, True))
    print(c("  ├─ TP1         +50%   → Amount 20%", GREEN, True))
    print(c("  ├─ TP2        +100%   → Amount 30%", GREEN))
    print(c("  ├─ TP3        +200%   → Amount 25%", GREEN))
    print(c("  ├─ TP4        +400%   → Amount 15%", GREEN))
    print(c("  ├─ SL          -20%   → Amount 100% (residuo)", RED, True))
    print(c("  └─ RUNNER       10%   → lasciato correre", YELLOW))
    print(c("  [Terminal] Exit Strategy → Add → 4 TP + 1 SL", WHITE))

def main():
    enable_ansi()
    print(c("═"*80, CYAN))
    print(c("  MEMECOIN SCANNER V10", CYAN, True))
    print(c("  SMART WALLET  +  EARLY SCORE  +  BURST  +  GOPLUS  +  TERMINAL EXIT", WHITE))
    print(c("═"*80, CYAN))

    print(c("\n[1/3] Cerco pool recenti e storico OHLCV...",BLUE,True))
    try:
        winners = historical_multiple()
    except Exception as e:
        winners=[]
        print("Historical hunter error:",e)

    if winners:
        print(f"Trovati {len(winners)} pool con espansione storica >=5x.")
        print("Esempi:")
        for w in winners[:5]:
            print(f"  {w['chain']} | {w['pool'][:12]}... | peak {w['peak_mult']:.1f}x | current {w['current_mult']:.1f}x")
        print(c("\n[2/3] Estraggo wallet dai vincitori storici...",BLUE,True))
        try:
            wallets, seeded = seed_historical_wallets(winners)
            print(f"Seed wallet aggiornati: {seeded}")
        except Exception as e:
            wallets=load_wallets()
            print("Wallet hunter error:",e)
    else:
        wallets=load_wallets()
        print("Nessun vincitore storico disponibile in questa scansione.")
        print("Nota: questo non blocca la scansione; il market scanner continua normalmente.")

    report=wallet_report(wallets)
    total_calls,total_wins,total_pending=wallet_stats(wallets)
    print(c(f"\nSMART-WALLET ENGINE  •  {len(wallets)} wallet | {total_calls} call | {total_pending} pending | {total_wins} win risolte",MAGENTA,True))
    print(c("TOP SMART-WALLET DATABASE",MAGENTA,True))
    if report:
        for i,(score,chain,w,calls,wins,losses,wr,early_rate,avg) in enumerate(report[:10],1):
            print(f"#{i} {chain} {w[:10]}... score {score}/100 | {wins}W/{losses}L | WR {wr*100:.0f}% | early {early_rate*100:.0f}% | avg peak {avg:.1f}x")
    else:
        print("Database in apprendimento: servono più chiamate risolte per costruire reputazione.")

    print(c("\n[3/3] Scansione mercato corrente...",BLUE,True))
    rows=discover()
    if not rows:
        print("Nessun candidato.")
        input("\nPremi INVIO per chiudere...")
        return

    evidence, wallets = update_smart_wallets(rows)

    for p in rows:
        bonus, ranked = smart_bonus(p, evidence)
        p["_smart_bonus"]=bonus
        p["_smart_ranked"]=ranked
        p["_final_score"]=min(100,round(p["_score"]+bonus))

    rows.sort(key=lambda x:(x["_final_score"],x["_accel"]),reverse=True)

    for i,p in enumerate(rows,1):
        sec=security(p)
        print(c(f"\n#{i}  EARLY FINAL  {p['_final_score']}/100  •  SMART +{p['_smart_bonus']}",score_color(p['_final_score']),True))
        print(fmt(p,sec,p["_smart_bonus"],p["_smart_ranked"]))
        if p["_final_score"] >= 60 and sec["status"] == "PASS":
            print_trade_plan()

    strong=[]
    for p in rows:
        # V8: acceleration is supportive, not mandatory. A strong candidate
        # needs a high early score, meaningful burst/activity, smart-wallet
        # evidence and a clean security result.
        burst_ok = p.get("_burst",0) >= 1.5
        accel_ok = (p.get("_accel",0) >= 1.15) if p.get("_has_accel_history") else False
        p1=num((p.get("priceChange") or {}).get("h1")); p24=num((p.get("priceChange") or {}).get("h24"))
        not_too_late = p1 <= 80 and p24 <= 180
        if p["_final_score"]>=78 and (burst_ok or accel_ok) and p["_smart_bonus"]>=4 and not_too_late:
            sec=security(p)
            if sec["status"]=="PASS":
                strong.append((p,sec))

    if strong:
        p,sec=strong[0]
        print(c("\n"+"═"*80,GREEN,True))
        print(c("  ★ CANDIDATO FORTE ★",GREEN,True))
        print(fmt(p,sec,p["_smart_bonus"],p["_smart_ranked"]))
        print_trade_plan()
        print(c("═"*80,GREEN,True))

        pair_address = p.get("pairAddress")
        alert_text = build_telegram_alert(p, sec, p["_smart_bonus"], p["_smart_ranked"])
        telegram_send(alert_text, pair_address=pair_address)
    else:
        print("\nNessun candidato ha superato score + accelerazione + smart-wallet + sicurezza.")

if __name__=="__main__":
    try:
        main()
    except Exception:
        print("\nERRORE NON GESTITO:\n")
        traceback.print_exc()
        print("\nIl terminale restera' aperto per permetterti di leggere l'errore.")
        input("\nPremi INVIO per chiudere...")
