import os,json,time,math,hashlib,traceback,requests

BASE="https://api.dexscreener.com"; TIMEOUT=20
DIR=os.path.dirname(os.path.abspath(__file__))
STATE=os.path.join(DIR,"scanner_state.json"); TGSTATE=os.path.join(DIR,"telegram_alert_state.json")
CHAIN="solana"; MIN_LIQ=20000; MIN_MC=20000; MAX_MC=10000000; MIN_VOL24=20000; TOP_N=15
RPC_BATCH_SIZE=8; RPC_RETRIES=4
BOT=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()
HELIUS_API_KEY=os.getenv("HELIUS_API_KEY","").strip()
SOLANA_RPC=f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
JUPITER_API_KEY=os.getenv("JUPITER_API_KEY","").strip()
JUPITER_BASE="https://api.jup.ag" if JUPITER_API_KEY else "https://lite-api.jup.ag"
JUPITER_QUOTE_AMOUNT=10_000_000  # 0.01 SOL, solo per verificare che esista una rotta
S=requests.Session(); S.headers.update({"User-Agent":"MemecoinScanner/4.0"})

def n(x):
    try:return float(x or 0)
    except:return 0.0

def get(url,**kw):
    last=None
    for i in range(3):
        try:
            r=S.get(url,timeout=TIMEOUT,**kw)
            if r.status_code==429:
                retry_after=n(r.headers.get("Retry-After"))
                time.sleep(retry_after if retry_after>0 else 1.5*(i+1))
                continue
            r.raise_for_status(); return r.json()
        except Exception as e:
            last=e
            if i<2: time.sleep(.8*(2**i))
    raise last

def load(path,default):
    try:
        with open(path,encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,type(default)) else default
    except:return default

def save(path,data):
    try:
        tmp=path+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump(data,f,indent=2)
        os.replace(tmp,path)
    except Exception as e:print("[STATE]",e)

def bp(p):
    t=(p.get("txns") or {}).get("h1") or {}; b=n(t.get("buys"));s=n(t.get("sells"))
    return b/(b+s) if b+s else 0

def burst(p):
    v1=n((p.get("volume") or {}).get("h1"));v24=n((p.get("volume") or {}).get("h24"))
    return v1*24/v24 if v24 else 0

def age(p):
    c=n(p.get("pairCreatedAt"));return max(0,(time.time()*1000-c)/3600000) if c else 999999

def score(p,old=None):
    liq=n((p.get("liquidity") or {}).get("usd"));mc=n(p.get("marketCap") or p.get("fdv"))
    v1=n((p.get("volume") or {}).get("h1"));v24=n((p.get("volume") or {}).get("h24"))
    pc=p.get("priceChange") or {};p1=n(pc.get("h1"));p6=n(pc.get("h6"));p24=n(pc.get("h24"))
    pressure=bp(p);bu=burst(p);s=0
    if mc<=500000:s+=15
    elif mc<=2000000:s+=11
    else:s+=6
    if liq>=250000:s+=20
    elif liq>=100000:s+=17
    elif liq>=50000:s+=14
    else:s+=9
    vm=v24/mc if mc else 0
    if vm>=2:s+=15
    elif vm>=1:s+=12
    elif vm>=.5:s+=8
    elif vm>=.2:s+=4
    if v1>=150000:s+=15
    elif v1>=75000:s+=12
    elif v1>=25000:s+=9
    elif v1>=10000:s+=5
    if bu>=3:s+=10
    elif bu>=2:s+=8
    elif bu>=1.5:s+=6
    elif bu>=1.25:s+=3
    if pressure>=.68:s+=10
    elif pressure>=.60:s+=8
    elif pressure>=.55:s+=6
    elif pressure>=.50:s+=3
    elif pressure<.42:s-=5
    t=(p.get("txns") or {}).get("h1") or {};tx=n(t.get("buys"))+n(t.get("sells"))
    if tx>=250:s+=5
    elif tx>=100:s+=4
    elif tx>=40:s+=2
    elif tx<20:s-=3
    if 5<=p1<=30:s+=5
    elif 0<p1<5:s+=2
    elif p1<-20:s-=5
    if 10<=p6<=80:s+=5
    elif p6<-25:s-=4
    a=age(p)
    if a<=3:s+=5
    elif a<=12:s+=4
    elif a<=24:s+=3
    elif a<=72:s+=1
    vl=v1/liq if liq else 0
    if vl>=25:s-=25
    elif vl>=15:s-=18
    elif vl>=10:s-=10
    if p1>100:s-=20
    elif p1>60:s-=12
    if p24>800:s-=35
    elif p24>500:s-=30
    elif p24>300:s-=22
    elif p24>150:s-=15
    elif p24>100:s-=9
    elif p24>60:s-=5
    acc=0;has=False
    if old and n(old.get("v1")):
        has=True;acc=v1/n(old["v1"])
        if acc>=3:s+=7
        elif acc>=2:s+=5
        elif acc>=1.5:s+=3
        elif acc<.65:s-=2
    return {"score":max(0,min(100,round(s))),"burst":bu,"accel":acc,"has_accel":has,"bp":pressure}

def max_position(p):
    liq=n((p.get("liquidity") or {}).get("usd"));mc=n(p.get("marketCap") or p.get("fdv"))
    if not liq or not mc:return 0,0
    impact=(liq/2)*(1-1/math.sqrt(1.02));mc_cap=mc*.0025;usd=min(impact,mc_cap)
    pu=n(p.get("priceUsd"));ps=n(p.get("priceNative"));solusd=pu/ps if pu and ps else 0
    return usd,usd/solusd if solusd else 0

def discover():
    tokens={}
    for ep in ("/token-profiles/latest/v1","/token-boosts/latest/v1"):
        try:data=get(BASE+ep)
        except Exception as e:print("[DISCOVERY]",e);continue
        for x in data if isinstance(data,list) else []:
            if str(x.get("chainId","")).lower()==CHAIN and x.get("tokenAddress"):tokens[x["tokenAddress"]]=1
    pairs=[]
    for token in list(tokens)[:150]:
        try:
            d=get(f"{BASE}/token-pairs/v1/{CHAIN}/{token}")
            if isinstance(d,list):pairs+=d
        except:pass
    old=load(STATE,{});new={};rows=[];seen=set()
    for p in pairs:
        if str(p.get("chainId","")).lower()!=CHAIN:continue
        pair=p.get("pairAddress")
        if not pair or pair in seen:continue
        seen.add(pair);liq=n((p.get("liquidity") or {}).get("usd"));mc=n(p.get("marketCap") or p.get("fdv"));v24=n((p.get("volume") or {}).get("h24"))
        if liq<MIN_LIQ or mc<MIN_MC or mc>MAX_MC or v24<MIN_VOL24:continue
        p["_m"]=score(p,old.get(pair));p["_score"]=p["_m"]["score"];rows.append(p);new[pair]={"ts":time.time(),"v1":n((p.get("volume") or {}).get("h1"))}
    save(STATE,new);rows.sort(key=lambda p:(p["_score"],p["_m"]["accel"],p["_m"]["burst"]),reverse=True)
    return rows[:TOP_N]


def rpc_batch(calls):
    if not SOLANA_RPC:
        raise RuntimeError("HELIUS_API_KEY non configurata: impossibile usare Solana RPC")
    out={}
    print(f"[ONCHAIN] RPC: Helius | calls={len(calls)} | batch={RPC_BATCH_SIZE}")
    for start in range(0,len(calls),RPC_BATCH_SIZE):
        chunk=calls[start:start+RPC_BATCH_SIZE]
        payload=[
            {"jsonrpc":"2.0","id":start+i+1,"method":m,"params":p}
            for i,(m,p) in enumerate(chunk)
        ]
        success=False
        for attempt in range(RPC_RETRIES):
            try:
                r=S.post(
                    SOLANA_RPC,
                    json=payload,
                    headers={"Content-Type":"application/json"},
                    timeout=TIMEOUT
                )
                if r.status_code==429:
                    retry_after=n(r.headers.get("Retry-After"))
                    wait=retry_after if retry_after>0 else min(2**attempt,12)
                    print(f"[ONCHAIN] RPC 429 | batch={start//RPC_BATCH_SIZE+1} | retry in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                body=r.json()
                if not isinstance(body,list):
                    raise ValueError("Risposta RPC non valida")
                for x in body:
                    if isinstance(x,dict) and x.get("id") is not None:
                        out[x["id"]]=x
                success=True
                break
            except Exception as e:
                print(f"[ONCHAIN] RPC ERROR | batch={start//RPC_BATCH_SIZE+1} attempt={attempt+1}/{RPC_RETRIES}: {type(e).__name__}: {e}")
                if attempt<RPC_RETRIES-1:
                    time.sleep(min(.8*(2**attempt),8))
        if not success:
            print(f"[ONCHAIN] RPC BATCH FALLITO | batch={start//RPC_BATCH_SIZE+1}")
    return out

def security_batch(rows):
    addrs=[]
    for p in rows:
        addr=(p.get("baseToken") or {}).get("address")
        if addr and addr not in addrs:addrs.append(addr)
    out={a:{"status":"UNVERIFIED","bad":[],"reason":"not_checked"} for a in addrs}
    if not addrs:return out

    calls=[]
    for a in addrs:
        calls.append(("getAccountInfo",[a,{"encoding":"jsonParsed","commitment":"finalized"}]))
    for a in addrs:
        calls.append(("getTokenLargestAccounts",[a,{"commitment":"finalized"}]))
    data=rpc_batch(calls)

    for i,addr in enumerate(addrs):
        mint_response=data.get(i+1)
        largest_response=data.get(len(addrs)+i+1)
        if not mint_response or "error" in mint_response:
            out[addr]={"status":"UNVERIFIED","bad":[],"reason":"rpc_error"}
            print(f"[ONCHAIN] {addr} => UNVERIFIED rpc_error")
            continue
        if not largest_response or "error" in largest_response:
            out[addr]={"status":"UNVERIFIED","bad":[],"reason":"largest_accounts_unavailable"}
            print(f"[ONCHAIN] {addr} => UNVERIFIED largest_accounts_unavailable")
            continue
        mint=mint_response.get("result",{}).get("value")
        largest=largest_response.get("result",{}).get("value") or []
        try:
            info=((mint or {}).get("data") or {}).get("parsed",{}).get("info",{})
            if not info:
                out[addr]={"status":"UNVERIFIED","bad":[],"reason":"invalid_mint_data"}
                print(f"[ONCHAIN] {addr} => UNVERIFIED invalid_mint_data")
                continue
            bad=[]
            if info.get("mintAuthority"):bad.append("mint_authority")
            if info.get("freezeAuthority"):bad.append("freeze_authority")

            if (mint or {}).get("owner")=="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPx":
                for ext in info.get("extensions") or []:
                    et=str(ext.get("extension","")).lower()
                    if et in ("transferhook","permanentdelegate","pausable","nontransferable","mintcloseauthority"):
                        bad.append(et)
                    elif et=="defaultaccountstate":
                        state=str((ext.get("state") or {}).get("state","")).lower()
                        if "frozen" in state:bad.append("default_frozen")
                    elif et=="transferfeeconfig":
                        cfg=ext.get("transferFeeConfig") or ext.get("transfer_fee_config") or {}
                        bps=n((cfg.get("newerTransferFee") or {}).get("transferFeeBasisPoints") or cfg.get("transfer_fee_basis_points"))
                        if bps>1000:bad.append("transfer_fee")

            supply=n(info.get("supply"))
            if supply and largest:
                top10=sum(sorted([n(x.get("amount")) for x in largest],reverse=True)[:10])
                if top10/supply>0.60:bad.append("holder_concentration")

            status="RISK" if bad else "PASS"
            out[addr]={"status":status,"bad":bad,"reason":"security_checks_ok"}
            print(f"[ONCHAIN] {addr} => {status} {bad}")
        except Exception as e:
            out[addr]={"status":"UNVERIFIED","bad":[],"reason":"parse_error"}
            print(f"[ONCHAIN] {addr} PARSE ERROR: {type(e).__name__}: {e}")
    return out

JUPITER_SOL="So11111111111111111111111111111111111111112"

def jupiter_quote(p):
    addr=(p.get("baseToken") or {}).get("address","").strip()
    if not addr:
        return {"status":"UNVERIFIED","reason":"missing_mint","route":False}
    try:
        params={
            "inputMint":JUPITER_SOL,
            "outputMint":addr,
            "amount":JUPITER_QUOTE_AMOUNT,
            "slippageBps":100
        }
        headers={"x-api-key":JUPITER_API_KEY} if JUPITER_API_KEY else {}
        r=S.get(f"{JUPITER_BASE}/swap/v1/quote",params=params,headers=headers,timeout=TIMEOUT)
        if r.status_code==429:
            return {"status":"UNVERIFIED","reason":"rate_limited","route":False}
        if r.status_code>=400:
            try:
                detail=r.json().get("error") or r.json().get("message") or f"http_{r.status_code}"
            except Exception:
                detail=f"http_{r.status_code}"
            return {"status":"NO_ROUTE","reason":str(detail)[:160],"route":False}
        data=r.json()
        route=bool(data.get("routePlan")) and n(data.get("outAmount"))>0
        if not route:
            return {"status":"NO_ROUTE","reason":str(data.get("error") or "no_route"),"route":False}
        return {
            "status":"PASS",
            "reason":"route_found",
            "route":True,
            "out_amount":str(data.get("outAmount","")),
            "route_plan":len(data.get("routePlan") or [])
        }
    except Exception as e:
        return {"status":"UNVERIFIED","reason":f"{type(e).__name__}: {e}"[:160],"route":False}

def jupiter_url(p):
    addr=(p.get("baseToken") or {}).get("address","").strip()
    if not addr:
        return ""
    return f"https://jup.ag/?buy={addr}&sell={JUPITER_SOL}"

def write_site_data(rows,alerts):
    site_dir=os.path.join(DIR,"site")
    os.makedirs(site_dir,exist_ok=True)
    alert_levels={p.get("pairAddress"):level for p,level in alerts}
    tokens=[]
    for p in rows:
        m=p.get("_m") or {}
        sec=p.get("_security") or {"status":"UNVERIFIED","bad":[],"reason":"not_checked"}
        jup=p.get("_jupiter") or {"status":"NOT_CHECKED","reason":"not_checked","route":False}
        usd,sol=max_position(p)
        tokens.append({
            "address":(p.get("baseToken") or {}).get("address",""),
            "symbol":(p.get("baseToken") or {}).get("symbol","?"),
            "name":(p.get("baseToken") or {}).get("name","?"),
            "pair":p.get("pairAddress",""),
            "dex":p.get("dexId","?"),
            "url":p.get("url",""),
            "score":p.get("_score",0),
            "level":alert_levels.get(p.get("pairAddress"),""),
            "burst":m.get("burst",0),
            "accel":m.get("accel",0),
            "has_accel":m.get("has_accel",False),
            "buy_pressure":m.get("bp",0)*100,
            "market_cap":n(p.get("marketCap") or p.get("fdv")),
            "liquidity":n((p.get("liquidity") or {}).get("usd")),
            "volume_1h":n((p.get("volume") or {}).get("h1")),
            "volume_24h":n((p.get("volume") or {}).get("h24")),
            "price_1h":n((p.get("priceChange") or {}).get("h1")),
            "price_6h":n((p.get("priceChange") or {}).get("h6")),
            "price_24h":n((p.get("priceChange") or {}).get("h24")),
            "age_hours":round(age(p),2),
            "security":sec,
            "jupiter":jup,
            "max_position_usd":usd,
            "max_position_sol":sol,
            "jupiter_url":jupiter_url(p),
        })
    payload={"generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"candidate_count":len(rows),"alert_count":len(alerts),"tokens":tokens}
    save(os.path.join(site_dir,"data.json"),payload)
def alert(p,level):
    t=p.get("baseToken") or {};m=p["_m"];pc=p.get("priceChange") or {};usd,sol=max_position(p)
    return "\n".join([
        f"🚨 MEMECOIN SCANNER — {level}","",f"🪙 {t.get('symbol','?')} — {t.get('name','?')}",
        f"⛓️ Solana / {p.get('dexId','?')}","",f"🎯 SCORE: {p['_score']}/100",
        f"⚡ VOLUME BURST: {m['burst']:.2f}x",f"📈 ACCELERAZIONE: {m['accel']:.2f}x" if m["has_accel"] else "📈 ACCELERAZIONE: N/D",
        f"📊 BUY PRESSURE: {m['bp']*100:.1f}%","",
        f"💰 MARKET CAP: {n(p.get('marketCap') or p.get('fdv')):,.0f}",
        f"💧 LIQUIDITY: {n((p.get('liquidity') or {}).get('usd')):,.0f}",
        f"🔥 VOLUME 1H: {n((p.get('volume') or {}).get('h1')):,.0f}",
        f"📊 VOLUME 24H: {n((p.get('volume') or {}).get('h24')):,.0f}",
        f"📈 PRICE: 1h {n(pc.get('h1')):+.1f}% | 6h {n(pc.get('h6')):+.1f}% | 24h {n(pc.get('h24')):+.1f}%",
        "",f"🟣 JUPITER: {p.get('_jupiter',{}).get('status','UNVERIFIED')}",
        "","🎯 MAX PUNTATA TEORICA: {}".format(sol if sol>=1 else sol),
        "   USD stimati: {:.0f} | price impact teorico <=2%".format(usd),"","🛡️ SECURITY: PASS",f"🔗 {p.get('url','')}"])

def send(text,pair,p=None):
    if not BOT or not CHAT:return False
    st=load(TGSTATE,{})
    if n(st.get(pair)) and time.time()-n(st[pair])<3600:return False
    try:
        payload={"chat_id":CHAT,"text":text,"disable_web_page_preview":False}
        jup=jupiter_url(p or {})
        if jup:
            payload["reply_markup"]={"inline_keyboard":[[{"text":"🟣 COMPRA SU JUPITER","url":jup}]]}
        r=S.post(f"https://api.telegram.org/bot{BOT}/sendMessage",json=payload,timeout=TIMEOUT)
        if r.status_code!=200:return False
        if not r.json().get("ok"):return False
        st[pair]=time.time();save(TGSTATE,st);print("[Telegram] Alert inviato.");return True
    except Exception as e:print("[Telegram]",e);return False

def main():
    print("="*80);print(" MEMECOIN SCANNER — SOLANA");print(" MARKET DATA + SECURITY + MAX POSITION");print("="*80)
    rows=discover()
    if not rows:print("Nessun candidato.");return
    alerts=[]
    secmap=security_batch(rows)
    for i,p in enumerate(rows,1):
        addr=(p.get("baseToken") or {}).get("address")
        sec=secmap.get(addr,{"status":"UNVERIFIED","bad":[],"reason":"not_checked"});p["_security"]=sec;m=p["_m"];sym=(p.get("baseToken") or {}).get("symbol","?")
        p1=n((p.get("priceChange") or {}).get("h1"));p24=n((p.get("priceChange") or {}).get("h24"));b=m["bp"]*100
        sec_label=sec["status"] if sec["status"]=="PASS" else f"{sec['status']}:{sec.get('reason','unknown')}"
        print("#{:<2} {:<10} score={:>3} burst={:>4.1f}x buy={:>5.1f}% 1h={:+6.1f}% 24h={:+7.1f}% security={}".format(i,sym,p["_score"],m["burst"],b,p1,p24,sec_label))
        if sec["status"]!="PASS" or p1>60 or p24>150 or p1<=-30 or p24<=-50:
            p["_jupiter"]={"status":"NOT_CHECKED","reason":"filtered_before_jupiter","route":False}
            continue
        jup=jupiter_quote(p);p["_jupiter"]=jup
        print(f"[JUPITER] {sym} => {jup['status']} ({jup.get('reason','')})")
        if not jup["route"]:continue
        if p["_score"]>=75 and (m["burst"]>=2 or m["has_accel"]):level="🔥 STRONG"
        elif p["_score"]>=60 and (m["burst"]>=1.5 or b>=55):level="🟢 INTERESTING"
        else:continue
        alerts.append((p,level))
    alerts.sort(key=lambda x:(x[0]["_score"],x[0]["_m"]["burst"]),reverse=True)
    write_site_data(rows,alerts)
    if not alerts:print("\nNessun alert. Dashboard aggiornata.");return
    for p,level in alerts:
        print("\n"+"-"*80);print(alert(p,level));print("-"*80)

if __name__=="__main__":
    try:main()
    except Exception:traceback.print_exc();raise
