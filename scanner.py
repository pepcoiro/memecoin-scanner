import os,json,time,math,hashlib,traceback,requests

BASE="https://api.dexscreener.com"; TIMEOUT=20
DIR=os.path.dirname(os.path.abspath(__file__))
STATE=os.path.join(DIR,"scanner_state.json"); TGSTATE=os.path.join(DIR,"telegram_alert_state.json")
CHAIN="solana"; MIN_LIQ=20000; MIN_MC=20000; MAX_MC=10000000; MIN_VOL24=20000; TOP_N=15
BOT=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()
SOLANA_RPC=os.getenv("SOLANA_RPC_URL","https://api.mainnet.solana.com").strip()

def rpc_batch(calls):
    try:
        r=S.post(SOLANA_RPC,json=[
            {"jsonrpc":"2.0","id":i+1,"method":m,"params":p}
            for i,(m,p) in enumerate(calls)
        ],headers={"Content-Type":"application/json"},timeout=TIMEOUT)
        r.raise_for_status()
        data=r.json()
        return {x.get("id"):x for x in data if isinstance(x,dict)}
    except Exception as e:
        print(f"[ONCHAIN] RPC ERROR: {type(e).__name__}: {e}")
        return {}

def security_batch(rows):
    addrs=[]
    for p in rows:
        addr=(p.get("baseToken") or {}).get("address")
        if addr and addr not in addrs:addrs.append(addr)
    out={a:{"status":"UNVERIFIED","bad":[]} for a in addrs}
    if not addrs:return out

    calls=[]
    for a in addrs:
        calls.append(("getAccountInfo",[a,{"encoding":"jsonParsed","commitment":"finalized"}]))
    for a in addrs:
        calls.append(("getTokenLargestAccounts",[a,{"commitment":"finalized"}]))
    data=rpc_batch(calls)

    for i,addr in enumerate(addrs):
        mint_resp=data.get(i+1,{})
        largest_resp=data.get(len(addrs)+i+1,{})
        bad=[]

        try:
            value=(mint_resp.get("result") or {}).get("value")
            info=((value or {}).get("data") or {}).get("parsed",{}).get("info",{})
            program=(value or {}).get("owner","")
            if not info:
                out[addr]={"status":"UNVERIFIED","bad":[]}
                continue

            mint_auth=info.get("mintAuthority")
            freeze_auth=info.get("freezeAuthority")
            if mint_auth:bad.append("mint_authority")
            if freeze_auth:bad.append("freeze_authority")

            if program=="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPx":
                exts=info.get("extensions") or []
                for ext in exts:
                    et=str(ext.get("extension","")).lower()
                    if et=="transferhook":bad.append("transfer_hook")
                    elif et=="permanentdelegate":bad.append("permanent_delegate")
                    elif et=="pausable":bad.append("pausable")
                    elif et=="defaultaccountstate":
                        state=str((ext.get("state") or {}).get("state","")).lower()
                        if "frozen" in state:bad.append("default_frozen")
                    elif et=="nontransferable":bad.append("non_transferable")
                    elif et=="mintcloseauthority":bad.append("mint_close_authority")
                    elif et=="transferfeeconfig":
                        cfg=ext.get("transferFeeConfig") or ext.get("transfer_fee_config") or {}
                        bps=n(cfg.get("newerTransferFee",{}).get("transferFeeBasisPoints") or cfg.get("transfer_fee_basis_points"))
                        if bps>1000:bad.append("transfer_fee")

            largest=((largest_resp.get("result") or {}).get("value") or [])
            supply=n(info.get("supply"))
            if supply>0 and largest:
                amounts=sorted([n(x.get("amount")) for x in largest],reverse=True)
                top_ex_lp=sum(amounts[1:10])
                concentration=top_ex_lp/supply*100
                if concentration>60:bad.append("holder_concentration")
        except Exception as e:
            print(f"[ONCHAIN] {addr} PARSE ERROR: {type(e).__name__}: {e}")
            out[addr]={"status":"UNVERIFIED","bad":[]}
            continue

        out[addr]={"status":"RISK" if bad else "PASS","bad":bad}
        print(f"[ONCHAIN] {addr} => {out[addr]['status']} {bad}")

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
        "","🎯 MAX PUNTATA TEORICA: {}".format(sol if sol>=1 else sol),
        "   USD stimati: {:.0f} | price impact teorico <=2%".format(usd),"","🛡️ SECURITY: PASS",f"🔗 {p.get('url','')}"])

def send(text,pair):
    if not BOT or not CHAT:return False
    st=load(TGSTATE,{})
    if n(st.get(pair)) and time.time()-n(st[pair])<3600:return False
    try:
        r=S.post(f"https://api.telegram.org/bot{BOT}/sendMessage",json={"chat_id":CHAT,"text":text,"disable_web_page_preview":False},timeout=TIMEOUT)
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
        sec=secmap.get(addr,{"status":"UNVERIFIED","bad":[]});m=p["_m"];sym=(p.get("baseToken") or {}).get("symbol","?")
        p1=n((p.get("priceChange") or {}).get("h1"));p24=n((p.get("priceChange") or {}).get("h24"));b=m["bp"]*100
        print("#{:<2} {:<10} score={:>3} burst={:>4.1f}x buy={:>5.1f}% 1h={:+6.1f}% 24h={:+7.1f}% security={} {}".format(i,sym,p["_score"],m["burst"],b,p1,p24,sec["status"],",".join(sec["bad"])))
        if sec["status"]!="PASS" or p1>60 or p24>150 or p1<=-30 or p24<=-50:continue
        if p["_score"]>=75 and (m["burst"]>=2 or m["has_accel"]):level="🔥 STRONG"
        elif p["_score"]>=60 and (m["burst"]>=1.5 or b>=55):level="🟢 INTERESTING"
        else:continue
        alerts.append((p,level))
    alerts.sort(key=lambda x:(x[0]["_score"],x[0]["_m"]["burst"]),reverse=True)
    if not alerts:print("\nNessun alert Telegram.");return
    for p,level in alerts:
        text=alert(p,level);print("\n"+"-"*80);print(text);print("-"*80);send(text,p.get("pairAddress"))

if __name__=="__main__":
    try:main()
    except Exception:traceback.print_exc();raise
