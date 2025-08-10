# app.py
# Single-file ETH scalper for Bitget (reads .env). EDIT ONLY .env.
# IMPORTANT: operate real money only if you accept risk. Test in PAPER_TRADING=true first.

import os, time, json, hmac, hashlib, base64, threading, logging
from datetime import datetime
from math import floor
from flask import Flask, render_template_string, request, jsonify
import requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ---- load env ----
load_dotenv()

# ---- CONFIG (from .env) ----
BITGET_API_KEY = os.getenv("BITGET_API_KEY","")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET","")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE","")

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY","")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY","")
# CoinGecko base endpoints work without key
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY","")

SYMBOL = os.getenv("SYMBOL","ethusdt_UMCBL")
PAPER_TRADING = os.getenv("PAPER_TRADING","true").lower() in ("1","true","yes")

MIN_LEVERAGE = int(os.getenv("MIN_LEVERAGE","9"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE","60"))
MIN_MARGIN_USAGE_PERCENT = float(os.getenv("MIN_MARGIN_USAGE_PERCENT","80.0"))

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL","1.0"))
DRAWDOWN_CLOSE_PCT = float(os.getenv("DRAWDOWN_CLOSE_PCT","0.03"))
LIQ_DIST_THRESHOLD = float(os.getenv("LIQ_DIST_THRESHOLD","0.03"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES","5"))

STATE_FILE = os.getenv("STATE_FILE","bot_state.json")
LOG_FILE = os.getenv("LOG_FILE","bot.log")

# fee approx (taker). adjust if needed
BITGET_FEE_RATE = float(os.getenv("BITGET_FEE_RATE","0.0006"))

# ---- logging ----
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)])
logger = logging.getLogger("eth_scalper")

# ---- flask UI ----
app = Flask(__name__)
INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>ETH Scalper</title></head>
<body style="font-family:Arial,Helvetica,sans-serif">
  <h2>ETH Scalper (Bitget)</h2>
  <p>Paper trading: <b>{{paper}}</b></p>
  <form method="post" action="/start"><button type="submit">Start</button></form>
  <form method="post" action="/stop"><button type="submit">Stop</button></form>
  <form method="get" action="/status"><button type="submit">Status</button></form>
  <hr>
  <pre id="state">{{state}}</pre>
<script>
setInterval(()=>fetch('/status_json').then(r=>r.json()).then(d=>document.getElementById('state').textContent=JSON.stringify(d,null,2)),1500);
</script>
</body></html>
"""

# ---- state persistence ----
state_lock = threading.Lock()
def default_state():
    return {"balance":1000.0, "profit":0.0, "positions":{"LONG":0,"SHORT":0}, "last_action":None, "trades":[], "consecutive_losses":0}
def load_state():
    try:
        with open(STATE_FILE,"r") as f:
            return json.load(f)
    except:
        s = default_state()
        save_state(s)
        return s
def save_state(s):
    with state_lock:
        with open(STATE_FILE,"w") as f:
            json.dump(s,f,indent=2,default=str)

state = load_state()

# ---- Bitget helpers ----
BASE_URL = "https://api.bitget.com"

def ts_ms():
    return str(int(time.time()*1000))

def sign_message(prehash, secret):
    mac = hmac.new((secret or "").encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def build_headers(method, request_path, body=''):
    timestamp = ts_ms()
    prehash = timestamp + method.upper() + request_path + (body or '')
    signature = sign_message(prehash, BITGET_API_SECRET)
    return {
        'ACCESS-KEY': BITGET_API_KEY,
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
        'Content-Type': 'application/json'
    }

# safe wrappers
def place_market_order_api(side, size, holdSide, leverage):
    body = {
        "symbol": SYMBOL,
        "price": "0",
        "size": str(int(size)),
        "side": side,
        "type": "market",
        "openType": "OPEN",
        "positionId": 0,
        "leverage": str(leverage),
        "externalOid": str(int(time.time()*1000)),
        "stopLossPrice": "0",
        "takeProfitPrice": "0",
        "reduceOnly": False,
        "visibleSize": "0",
        "holdSide": holdSide
    }
    if PAPER_TRADING:
        logger.info("[PAPER] Simulated order: %s size=%s lev=%s hold=%s", side, size, leverage, holdSide)
        return {"code":"00000","data":{"sim":"ok"}}
    try:
        path = "/api/mix/v1/order/placeOrder"
        body_s = json.dumps(body)
        r = requests.post(BASE_URL+path, headers=build_headers('POST', path, body_s), data=body_s, timeout=15)
        return r.json()
    except Exception as e:
        logger.exception("place_market_order_api error")
        return {"code":"err","msg":str(e)}

def close_position_api(holdSide):
    pos = get_positions().get(holdSide)
    if not pos or float(pos.get('size',0)) <= 0:
        return {"code":"no_pos"}
    size = int(float(pos.get('size')))
    side = 'SELL' if holdSide=='LONG' else 'BUY'
    body = {
        "symbol": SYMBOL, "price": "0", "size": str(size),
        "side": side, "type": "market", "openType": "CLOSE",
        "positionId": int(pos.get('positionId') or 0),
        "leverage": str(pos.get('leverage') or MIN_LEVERAGE),
        "externalOid": str(int(time.time()*1000)),
        "stopLossPrice":"0","takeProfitPrice":"0","reduceOnly": True,
        "visibleSize":"0","holdSide": holdSide
    }
    if PAPER_TRADING:
        logger.info("[PAPER] Simulated close: %s", holdSide)
        with state_lock:
            state['positions'][holdSide] = 0
            save_state(state)
        return {"code":"00000","data":{"sim":"ok"}}
    try:
        path = "/api/mix/v1/order/placeOrder"
        body_s = json.dumps(body)
        r = requests.post(BASE_URL+path, headers=build_headers('POST', path, body_s), data=body_s, timeout=15)
        return r.json()
    except Exception as e:
        logger.exception("close_position_api err")
        return {"code":"err","msg":str(e)}

# ---- market data ----
def fetch_candles(limit=200):
    try:
        path = f"/api/mix/v1/market/candles?symbol={SYMBOL}&period=1min&limit={limit}"
        r = requests.get(BASE_URL+path, timeout=8).json()
        if isinstance(r, dict) and r.get('code')=='00000':
            return r['data']
        if isinstance(r, list):
            return r
    except Exception as e:
        logger.debug("fetch_candles err: %s", e)
    return []

def fetch_orderbook(size=50):
    try:
        path = f"/api/mix/v1/market/depth?symbol={SYMBOL}&size={size}"
        r = requests.get(BASE_URL+path, timeout=6).json()
        if isinstance(r, dict) and r.get('code')=='00000':
            return r['data']
    except Exception as e:
        logger.debug("fetch_orderbook err: %s", e)
    return None

def coingecko_price():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", timeout=6).json()
        return float(r.get("ethereum",{}).get("usd"))
    except Exception as e:
        logger.debug("coingecko err: %s", e)
        return None

# ---- news + onchain ----
def newsapi_fetch(q="ethereum OR eth", page_size=6):
    if not NEWSAPI_KEY: return []
    try:
        url="https://newsapi.org/v2/everything"
        params={"q":q,"pageSize":page_size,"language":"en","apiKey":NEWSAPI_KEY,"sortBy":"publishedAt"}
        r = requests.get(url, params=params, timeout=6).json()
        return r.get("articles",[])
    except Exception as e:
        logger.debug("newsapi_fetch err: %s", e)
        return []

def assess_news(articles):
    # quick credibility + sentiment
    titles=[a.get('title','') for a in articles[:8]]
    sets=[set(t.lower().split()[:8]) for t in titles]
    matches=0
    for i in range(len(sets)):
        for j in range(i+1,len(sets)):
            if len(sets[i].intersection(sets[j]))>=3: matches+=1
    cross=min(1.0, matches/max(1,len(sets)))
    official_hosts=["ethereum.org","cointelegraph.com","coindesk.com","reuters.com","coinbase.com","binance.com"]
    official_count=sum(1 for a in articles if any(h in (a.get('url') or '').lower() for h in official_hosts))
    official=min(1.0, official_count/3.0)
    cred=0.4*cross+0.6*official
    pos_words=["upgrade","bull","gain","surge","positive","adopt","launch","success"]
    neg_words=["drop","bear","sell","scam","hack","downgrade","regulation","fail","attack"]
    sents=[]
    for a in articles[:6]:
        t=(a.get('title','')+" "+(a.get('description') or "")).lower()
        score=sum(1 for w in pos_words if w in t)-sum(1 for w in neg_words if w in t)
        sents.append(max(-1,min(1,score/3)))
    avg_sent=float(np.mean(sents)) if sents else 0.0
    return cred, avg_sent

def etherscan_whale_watch(min_eth=200):
    if not ETHERSCAN_API_KEY: return []
    try:
        url=f"https://api.etherscan.io/api?module=account&action=txlistinternal&startblock=0&endblock=99999999&page=1&offset=50&sort=desc&apikey={ETHERSCAN_API_KEY}"
        r=requests.get(url,timeout=6).json()
        out=[]
        if r.get("status")=="1":
            for t in r.get("result",[]):
                val=float(t.get("value",0))/1e18
                if val>=min_eth:
                    out.append({"hash":t.get("hash"),"value":val,"to":t.get("to")})
        return out
    except Exception as e:
        logger.debug("etherscan err: %s", e)
        return []

# ---- indicators ----
def orderbook_imbalance():
    ob=fetch_orderbook(50)
    if not ob: return 0.0
    bids=ob.get('bids',[])[:20]; asks=ob.get('asks',[])[:20]
    sum_b=sum(float(b[1]) for b in bids) if bids else 0
    sum_a=sum(float(a[1]) for a in asks) if asks else 0
    if sum_a+sum_b==0: return 0.0
    return (sum_b-sum_a)/(sum_b+sum_a)

def ema_signals(closes):
    s=pd.Series(closes)
    return float(s.ewm(span=5).mean().iloc[-1]), float(s.ewm(span=30).mean().iloc[-1])

# ---- ensemble and online learning ----
weights = np.array([0.6,0.3,0.1])  # model, news, book initial
lr = 0.03

def ensemble_score(rel, news_sent, obi):
    model_signal=np.tanh(rel*200)
    news_signal=np.tanh(news_sent*2)
    book_signal=np.tanh(obi*2)
    feats=np.array([model_signal, news_signal, book_signal])
    score=np.dot(weights,feats)
    confidence=float(np.clip(abs(score),0.0,1.0))
    direction=1 if score>0 else -1 if score<0 else 0
    return direction,confidence,feats

def online_update(feats,outcome):
    global weights
    # outcome +1 profit, -1 loss
    pred=np.dot(weights,feats)
    err= (1 if outcome>0 else -1) - pred
    weights += lr * err * feats
    weights = np.clip(weights,0.01,10.0)
    weights = weights / weights.sum()

# ---- sizing + risk ----
def map_conf_to_leverage(conf):
    lev = int(round(MIN_LEVERAGE + (MAX_LEVERAGE-MIN_LEVERAGE)*(conf**1.5)))
    return max(MIN_LEVERAGE, min(MAX_LEVERAGE, lev))

def compute_qty(balance, price, leverage):
    margin_pct = max(0.01, MIN_MARGIN_USAGE_PERCENT/100.0)
    notional = balance * margin_pct * leverage
    qty = int(notional // max(1.0, price))
    return max(0, qty)

def get_balance():
    if PAPER_TRADING:
        with state_lock:
            return float(state.get("balance",1000.0))
    try:
        path="/api/mix/v1/account/assets"
        r=requests.get(BASE_URL+path, headers=build_headers('GET', path, ''), timeout=8).json()
        if r.get('code')=='00000':
            for a in r.get('data',[]):
                if a.get('marginCoin')=='USDT': return float(a.get('availableBalance',0.0))
    except Exception as e:
        logger.debug("get_balance err: %s", e)
    return 0.0

def get_positions():
    try:
        path=f"/api/mix/v1/position/singlePosition?symbol={SYMBOL}&marginCoin=USDT"
        r=requests.get(BASE_URL+path, headers=build_headers('GET', path, ''), timeout=8).json()
        if r.get('code')=='00000':
            out={'LONG':None,'SHORT':None}
            for p in r.get('data',[]): out[p['holdSide']] = p
            return out
    except Exception as e:
        logger.debug("get_positions err: %s", e)
    return {'LONG':None,'SHORT':None}

def get_last_price():
    c = fetch_candles(3)
    if c:
        try:
            closes=[float(x[4]) for x in c]; return closes[-1]
        except:
            pass
    return coingecko_price()

# ---- bot loop (core) ----
running=False
bot_thread=None

def bot_loop():
    global running
    logger.info("Bot started. PAPER=%s SYMBOL=%s",PAPER_TRADING,SYMBOL)
    while running:
        try:
            candles=fetch_candles(120)
            if not candles or len(candles)<30:
                time.sleep(POLL_INTERVAL); continue
            # parse closes
            closes=[]
            for c in candles:
                try: closes.append(float(c[4]))
                except:
                    try: closes.append(float(c.get('close')))
                    except: pass
            if len(closes)<30: time.sleep(POLL_INTERVAL); continue
            price=closes[-1]
            ema_fast, ema_slow = ema_signals(closes)
            rel = (ema_fast - ema_slow)/price
            articles = newsapi_fetch("ethereum OR eth", page_size=5) if NEWSAPI_KEY else []
            cred, news_sent = assess_news(articles) if articles else (0.5,0.0)
            whales = etherscan_whale_watch(200) if ETHERSCAN_API_KEY else []
            if whales:
                news_sent = news_sent + 0.2 if news_sent>0 else news_sent - 0.2
            obi = orderbook_imbalance()
            direction, confidence, feats = ensemble_score(rel, news_sent, obi)
            leverage = map_conf_to_leverage(confidence)
            balance = get_balance()
            qty = compute_qty(balance, price, leverage)
            # dynamic scalp target based on recent volatility
            vol = float(np.std(pd.Series(closes).pct_change().dropna()) if len(closes)>10 else 0.002)
            scalp_target = max(0.0004, min(0.002, vol*2))  # 0.04% - 0.2% typical
            # safety: if many consecutive losses, reduce margin usage and leverage
            with state_lock:
                cons_losses = int(state.get("consecutive_losses",0))
            if cons_losses >= 3:
                # emergency defensive mode
                scalp_target *= 0.8
                leverage = max(MIN_LEVERAGE, int(leverage * 0.6))
            action="HOLD"
            if direction>0 and abs(rel) > 0.0003:
                action="BUY"
            elif direction<0 and abs(rel) > 0.0003:
                action="SELL"
            logger.info("P=%.2f rel=%.6f dir=%s conf=%.3f lev=%d qty=%d scalp=%.5f news=%.3f obi=%.3f whales=%d", price, rel, direction, confidence, leverage, qty, scalp_target, news_sent, obi, len(whales))
            if qty>0 and action in ("BUY","SELL"):
                holdSide = "LONG" if action=="BUY" else "SHORT"
                side = "BUY" if action=="BUY" else "SELL"
                # close opposite
                close_position_api("SHORT" if holdSide=="LONG" else "LONG")
                res = place_market_order_api(side, qty, holdSide, leverage)
                entry_price = price
                t0 = datetime.utcnow().isoformat()
                trade = {"ts":t0,"action":action,"entry":entry_price,"qty":qty,"lev":leverage,"res":str(res)[:200]}
                with state_lock:
                    state["trades"].append(trade); state["last_action"]=action; save_state(state)
                # aggressive scalping window
                scalp_wait = max(0.2, POLL_INTERVAL/2)
                checks = int(max(3, min(30, 2.0/scalp_wait)))  # up to a few seconds
                pnl = 0.0; outcome= -1
                for i in range(checks):
                    time.sleep(scalp_wait)
                    last = get_last_price()
                    if not last: continue
                    if action=="BUY":
                        if (last - entry_price)/entry_price >= scalp_target - BITGET_FEE_RATE:
                            close_position_api("LONG"); pnl = (last-entry_price)*qty; outcome=1; break
                        if (entry_price - last)/entry_price >= DRAWDOWN_CLOSE_PCT:
                            close_position_api("LONG"); pnl=(last-entry_price)*qty; outcome=-1; break
                    else:
                        if (entry_price - last)/entry_price >= scalp_target - BITGET_FEE_RATE:
                            close_position_api("SHORT"); pnl = (entry_price-last)*qty; outcome=1; break
                        if (last - entry_price)/entry_price >= DRAWDOWN_CLOSE_PCT:
                            close_position_api("SHORT"); pnl=(entry_price-last)*qty; outcome=-1; break
                if outcome==-1 and pnl==0.0:
                    # force close to limit exposure
                    close_position_api("LONG" if action=="BUY" else "SHORT")
                    last = get_last_price()
                    if last:
                        pnl = (last-entry_price)*qty if action=="BUY" else (entry_price-last)*qty
                        outcome = 1 if pnl>0 else -1
                    else:
                        outcome = -1
                with state_lock:
                    state["profit"] = float(state.get("profit",0.0) + pnl)
                    state["trades"][-1].update({"exit_pnl":pnl,"outcome":outcome})
                    if outcome>0:
                        state["consecutive_losses"]=0
                    else:
                        state["consecutive_losses"]=state.get("consecutive_losses",0)+1
                    # emergency stop if too many consecutive losses
                    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                        logger.warning("Consecutive losses >= %d -> stopping bot for safety", MAX_CONSECUTIVE_LOSSES)
                        running=False
                    save_state(state)
                # online learning
                try:
                    online_update(feats, 1 if outcome>0 else -1)
                except Exception as e:
                    logger.debug("online update err: %s", e)
            # end action
        except Exception as e:
            logger.exception("bot loop exception")
        time.sleep(POLL_INTERVAL)
    logger.info("Bot stopped")

# ---- flask routes ----
@app.route("/")
def index_route():
    with state_lock:
        s = load_state()
    return render_template_string(INDEX_HTML, paper=str(PAPER_TRADING), state=json.dumps(s,indent=2))

@app.route("/start", methods=["POST"])
def start_route():
    global running, bot_thread
    if running: return "already running",200
    running=True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("User started bot")
    return "started",200

@app.route("/stop", methods=["POST"])
def stop_route():
    global running
    running=False
    logger.info("User stopped bot")
    return "stopped",200

@app.route("/status")
def status_page(): 
    with state_lock:
        s = load_state()
    return jsonify(s)

@app.route("/status_json")
def status_json(): 
    with state_lock:
        s = load_state()
    return jsonify(s)

@app.route("/health")
def health_route():
    ok=True; msgs=[]
    if not BITGET_API_KEY or not BITGET_API_SECRET or not BITGET_PASSPHRASE:
        ok=False; msgs.append("Missing BITGET keys")
    return jsonify({"ok":ok,"messages":msgs})

# ---- entrypoint ----
if __name__=="__main__":
    logger.info("Starting scalper. PAPER=%s SYMBOL=%s",PAPER_TRADING,SYMBOL)
    save_state(state)
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
