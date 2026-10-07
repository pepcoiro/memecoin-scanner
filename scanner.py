import os,csv,json,time,math,hashlib,traceback,requests

BASE="https://api.dexscreener.com"; TIMEOUT=20
DIR=os.path.dirname(os.path.abspath(__file__))
STATE=os.path.join(DIR,"scanner_state.json"); TGSTATE=os.path.join(DIR,"telegram_alert_state.json")
CHAIN="solana"; MIN_LIQ=20000; MIN_MC=20000; MAX_MC=10000000; MIN_VOL24=20000; TOP_N=15
ACC_GAP=1800  # secondi: oltre questo gap tra due run l'accelerazione non viene calcolata
LOG=os.path.join(DIR,"alerts_log.csv"); COLS=["ts","pair","symbol","level","score","burst","accel","bp","price","r1h","r6h","r24h"]
SYS="11111111111111111111111111111111"; BURN="1nc1nerator11111111111111111111111111111111"
BOT=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()
HELIUS_API_KEY=os.getenv("HELIUS_API_KEY","").strip()
SOLANA_RPC=f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
S=requests.Session(); S.headers.update({"User-Agent":"MemecoinScanner/4.0"})

def n(x):
    try:return float(x or 0)
    except:return 0.0

def get(url,**kw):
    last=RuntimeError("429 persistente")
    for i in range(3):
        try:
            r=S.get(url,timeout=TIMEOUT,**kw)
            if r.status_code==429: time.sleep(1.5*(i+1)); continue
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
        except Exception as e:print("[DISCOVERY]",type(e).__name__);continue
        for x in data if isinstance(data,list) else []:
            if str(x.get("chainId","")).lower()==CHAIN and x.get("tokenAddress"):tokens[x["tokenAddress"]]=1
    addrs=list(tokens)[:150];pairs=[]
    for i in range(0,len(addrs),30):
        try:
            d=get(f"{BASE}/tokens/v1/{CHAIN}/{','.join(addrs[i:i+30])}")
            if isinstance(d,list):pairs+=d
        except Exception as e:print("[PAIRS]",type(e).__name__)
    old=load(STATE,{});new={};best={};wanted=set(addrs)
    for p in pairs:
        if str(p.get("chainId","")).lower()!=CHAIN:continue
        base=(p.get("baseToken") or {}).get("address");pair=p.get("pairAddress")
        if not pair or base not in wanted:continue  # scarta pair dove il token e' il quote
        liq=n((p.get("liquidity") or {}).get("usd"));mc=n(p.get("marketCap") or p.get("fdv"));v24=n((p.get("volume") or {}).get("h24"))
        if liq<MIN_LIQ or mc<MIN_MC or mc>MAX_MC or v24<MIN_VOL24:continue
        if base not in best or liq>n((best[base].get("liquidity") or {}).get("usd")):best[base]=p  # un solo pair per token
    rows=[]
    for p in best.values():
        pair=p["pairAddress"];o=old.get(pair)
        if o and time.time()-n(o.get("ts"))>ACC_GAP:o=None
        p["_m"]=score(p,o);p["_score"]=p["_m"]["score"];rows.append(p)
        new[pair]={"ts":time.time(),"v1":n((p.get("volume") or {}).get("h1"))}
    if pairs:save(STATE,new)  # non azzerare lo stato se DexScreener non ha risposto
    rows.sort(key=lambda p:(p["_score"],p["_m"]["accel"],p["_m"]["burst"]),reverse=True)
    return rows[:TOP_N]


def rpc_batch(calls):
    if not SOLANA_RPC:
        raise RuntimeError("HELIUS_API_KEY non configurata: impossibile usare Solana RPC")
    print("[ONCHAIN] RPC: Helius")
    body=[{"jsonrpc":"2.0","id":i+1,"method":m,"params":p} for i,(m,p) in enumerate(calls)]
    for i in range(3):
        try:
            r=S.post(SOLANA_RPC,json=body,headers={"Content-Type":"application/json"},timeout=TIMEOUT)
            r.raise_for_status()
            return {x.get("id"):x for x in r.json() if isinstance(x,dict)}
        except Exception as e:
            print(f"[ONCHAIN] RPC ERROR: {type(e).__name__}")  # niente str(e): contiene l'URL con la api-key
            if i<2:time.sleep(1+i)
    return {}

def _ow(acc):
    d=(acc or {}).get("data")
    return ((d.get("parsed") or {}).get("info") or {}).get("owner") if isinstance(d,dict) else None

def real_holders(lg):
    # token account -> owner -> tieni solo wallet veri (owner dell'owner = System Program), esclusi pool/curve (PDA) e burn
    ks=[a for a in lg if lg[a]]
    if not ks:return {}
    d=rpc_batch([("getMultipleAccounts",[[x["address"] for x in lg[a]],{"encoding":"jsonParsed","commitment":"confirmed"}]) for a in ks])
    own={}
    for i,a in enumerate(ks):
        v=((d.get(i+1) or {}).get("result") or {}).get("value")
        if isinstance(v,list) and len(v)==len(lg[a]):own[a]=[_ow(x) or SYS for x in v]
    if not own:return {}
    d=rpc_batch([("getMultipleAccounts",[own[a],{"encoding":"base64","dataSlice":{"offset":0,"length":0},"commitment":"confirmed"}]) for a in own])
    res={}
    for i,a in enumerate(own):
        v=((d.get(i+1) or {}).get("result") or {}).get("value")
        if isinstance(v,list) and len(v)==len(own[a]):
            res[a]=[n(x["amount"]) for x,o,acc in zip(lg[a],own[a],v) if acc and acc.get("owner")==SYS and o!=BURN]
    return res

def security_batch(rows):
    addrs=[]
    for p in rows:
        addr=(p.get("baseToken") or {}).get("address")
        if addr and addr not in addrs:addrs.append(addr)
    out={a:{"status":"UNVERIFIED","bad":[]} for a in addrs}
    if not addrs:return out

    calls=[]
    for a in addrs:
        calls.append(("getAccountInfo",[a,{"encoding":"jsonParsed","commitment":"confirmed"}]))
    for a in addrs:
        calls.append(("getTokenLargestAccounts",[a,{"commitment":"confirmed"}]))
    data=rpc_batch(calls)
    lg={a:(data.get(len(addrs)+i+1) or {}).get("result",{}).get("value") or [] for i,a in enumerate(addrs)}
    rh=real_holders(lg)

    for i,addr in enumerate(addrs):
        mint=(data.get(i+1) or {}).get("result",{}).get("value")
        largest=(data.get(len(addrs)+i+1) or {}).get("result",{}).get("value") or []
        try:
            info=((mint or {}).get("data") or {}).get("parsed",{}).get("info",{})
            if not info:
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
            if not supply or not largest or addr not in rh:continue  # dati holder mancanti: resta UNVERIFIED
            top10=sum(sorted(rh[addr],reverse=True)[:10])
            if top10/supply>0.60:bad.append("holder_concentration")

            out[addr]={"status":"RISK" if bad else "PASS","bad":bad}
            print(f"[ONCHAIN] {addr} => {out[addr]['status']} {bad}")
        except Exception as e:
            print(f"[ONCHAIN] {addr} PARSE ERROR: {type(e).__name__}: {e}")
    return out

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
        "",f"🎯 MAX PUNTATA TEORICA: {sol:.2f} SOL",
        f"   USD stimati: {usd:.0f} | price impact teorico <=2%","","🛡️ SECURITY: PASS",f"🔗 {p.get('url','')}"])

def send(text,pair):
    if not BOT or not CHAT:return False
    st=load(TGSTATE,{})
    if n(st.get(pair)) and time.time()-n(st[pair])<3600:return False
    try:
        r=S.post(f"https://api.telegram.org/bot{BOT}/sendMessage",json={"chat_id":CHAT,"text":text,"disable_web_page_preview":False},timeout=TIMEOUT)
        if r.status_code!=200:print("[Telegram]",r.status_code,r.text[:200]);return False
        if not r.json().get("ok"):return False
        st[pair]=time.time();save(TGSTATE,st);print("[Telegram] Alert inviato.");return True
    except Exception as e:print("[Telegram]",type(e).__name__);return False

def log_alert(p,level):
    m=p["_m"];new=not os.path.exists(LOG)
    try:
        with open(LOG,"a",newline="",encoding="utf-8") as f:
            w=csv.writer(f)
            if new:w.writerow(COLS)
            w.writerow([int(time.time()),p.get("pairAddress"),(p.get("baseToken") or {}).get("symbol","?"),level,p["_score"],round(m["burst"],2),round(m["accel"],2),round(m["bp"],3),n(p.get("priceUsd")),"","",""])
    except Exception as e:print("[LOG]",type(e).__name__)

def update_log():
    # compila r1h/r6h/r24h (% vs prezzo dell'alert); "NA" se la finestra e' scaduta o il prezzo non c'e'
    try:
        with open(LOG,newline="",encoding="utf-8") as f:rows=list(csv.DictReader(f))
        now=time.time();todo=[]
        for r in rows:
            el=(now-n(r["ts"]))/3600
            for k,h in (("r1h",1),("r6h",6),("r24h",24)):
                if not r[k] and el>=h:todo.append((r,k,h,el))
        if not todo:return
        pairs=sorted({r["pair"] for r,_,_,_ in todo});px={}
        for i in range(0,len(pairs),30):
            d=get(f"{BASE}/latest/dex/pairs/{CHAIN}/{','.join(pairs[i:i+30])}")
            for x in d.get("pairs") or []:px[x.get("pairAddress")]=n(x.get("priceUsd"))
        for r,k,h,el in todo:
            p0=n(r["price"]);p=px.get(r["pair"])
            r[k]="NA" if el>h*1.5 or not p0 or not p else round((p/p0-1)*100,1)
        tmp=LOG+".tmp"
        with open(tmp,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=COLS);w.writeheader();w.writerows(rows)
        os.replace(tmp,LOG)
    except FileNotFoundError:pass
    except Exception as e:print("[LOG]",type(e).__name__)

def main():
    print("="*80);print(" MEMECOIN SCANNER — SOLANA");print(" MARKET DATA + SECURITY + MAX POSITION");print("="*80)
    update_log()
    rows=discover()
    if not rows:print("Nessun candidato.");return
    alerts=[]
    secmap=security_batch(rows)
    for i,p in enumerate(rows,1):
        addr=(p.get("baseToken") or {}).get("address")
        sec=secmap.get(addr,{"status":"UNVERIFIED","bad":[]});m=p["_m"];sym=(p.get("baseToken") or {}).get("symbol","?")
        p1=n((p.get("priceChange") or {}).get("h1"));p24=n((p.get("priceChange") or {}).get("h24"));b=m["bp"]*100
        print("#{:<2} {:<10} score={:>3} burst={:>4.1f}x buy={:>5.1f}% 1h={:+6.1f}% 24h={:+7.1f}% security={}".format(i,sym,p["_score"],m["burst"],b,p1,p24,sec["status"]))
        if sec["status"]!="PASS" or p1>60 or p24>150 or p1<=-30 or p24<=-50:continue
        if p["_score"]>=75 and (m["burst"]>=2 or m["has_accel"]):level="🔥 STRONG"
        elif p["_score"]>=60 and (m["burst"]>=1.5 or b>=55):level="🟢 INTERESTING"
        else:continue
        alerts.append((p,level))
    alerts.sort(key=lambda x:(x[0]["_score"],x[0]["_m"]["burst"]),reverse=True)
    if not alerts:print("\nNessun alert Telegram.");return
    for p,level in alerts:
        text=alert(p,level);print("\n"+"-"*80);print(text);print("-"*80)
        if send(text,p.get("pairAddress")):log_alert(p,level)

if __name__=="__main__":
    try:main()
    except Exception:traceback.print_exc();raise
