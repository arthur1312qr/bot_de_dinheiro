<<<<<<< HEAD
# app.py
# Bot único (Flask UI + motor de trading Bitget + sinais NewsAPI/CoinGecko/Etherscan)
# Uso: python app.py
# Recomendo rodar em servidor (Render.com) em produção. Teste localmente com PAPER_TRADING=true.

import os, time, json, math, hmac, hashlib, base64, threading
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
import requests
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

# -------------------------
# CONFIG (leitura via ENV)
# -------------------------
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")            # newsapi.org
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")# etherscan.io
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")# optional (CoinGecko basic doesn't need key)

SYMBOL = os.getenv("SYMBOL", "ethusdt_UMCBL")         # Bitget symbol
MIN_LEVERAGE = int(os.getenv("MIN_LEVERAGE", "9"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "60"))
MIN_MARGIN_USAGE_PERCENT = float(os.getenv("MIN_MARGIN_USAGE_PERCENT", "80.0"))  # percent
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1","true","yes")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))   # seconds between cycles
DRAWDOWN_CLOSE_PCT = float(os.getenv("DRAWDOWN_CLOSE_PCT", "0.03"))  # 3% drawdown -> close
LIQ_DIST_THRESHOLD = float(os.getenv("LIQ_DIST_THRESHOLD", "0.03"))  # 3% distance to liquidation -> close

STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

# Fees (approx) — adjust if needed; used to ensure minimal profitable moves
BITGET_FEE_RATE = float(os.getenv("BITGET_FEE_RATE", "0.0006"))  # 0.06% example taker fee

# ------------------------------
# Minimal positive/negative words (fast sentiment)
# ------------------------------
POS_WORDS = {"upgrade","bull","bullish","gain","surge","improve","positive","adopt","partnership","launch","success"}
NEG_WORDS = {"drop","bear","bearish","sell","scam","hack","downgrade","negative","regulation","fail","attack"}

# ------------------------------
# Flask app (UI)
# ------------------------------
app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<title>ETH Bot Control</title>
<h2>Bot ETHUSDT (Bitget) - Painel</h2>
<p>Paper trading: <b>{{paper}}</b></p>
<form method="post" action="/start"><button type="submit">Iniciar Bot</button></form>
<form method="post" action="/stop"><button type="submit">Parar Bot</button></form>
<form method="get" action="/status"><button type="submit">Status</button></form>
<hr>
<p>Último status (JSON):</p>
<pre id="state">{{state}}</pre>
<script>
setInterval(function(){
  fetch('/status_json').then(r=>r.json()).then(d=>{
    document.getElementById('state').textContent=JSON.stringify(d, null, 2);
  });
}, 4000);
</script>
"""

# ------------------------------
# State persistence
# ------------------------------
state_lock = threading.Lock()
def load_state():
    try:
        with open(STATE_FILE,"r") as f:
            return json.load(f)
    except:
        s = {"balance":1000.0, "positions": {"LONG":0,"SHORT":0}, "last_action":None, "profit":0.0, "last_update":None}
        save_state(s)
        return s

def save_state(s):
    with state_lock:
        with open(STATE_FILE,"w") as f:
            json.dump(s, f, indent=2)

state = load_state()

# ------------------------------
# Bitget REST helpers
# ------------------------------
BASE_URL = "https://api.bitget.com"

def ts_ms():
    return str(int(time.time()*1000))

def sign_message(prehash, secret):
    mac = hmac.new(secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def build_headers(method, request_path, body=''):
    timestamp = ts_ms()
    prehash = timestamp + method.upper() + request_path + (body or '')
    signature = sign_message(prehash, BITGET_API_SECRET or "")
    return {
        'ACCESS-KEY': BITGET_API_KEY or "",
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE or "",
        'Content-Type': 'application/json'
    }

# public: fetch candles
def fetch_candles(limit=200):
    try:
        path = f"/api/mix/v1/market/candles?symbol={SYMBOL}&period=1min&limit={limit}"
        r = requests.get(BASE_URL + path, timeout=8).json()
        if r.get('code') == '00000':
            return r['data']
    except Exception:
        pass
    return []

# public: orderbook
def fetch_orderbook(size=50):
    try:
        path = f"/api/mix/v1/market/depth?symbol={SYMBOL}&size={size}"
        r = requests.get(BASE_URL + path, timeout=6).json()
        if r.get('code') == '00000':
            return r['data']
    except Exception:
        pass
    return None

# account balance (USDT)
def get_balance():
    if PAPER_TRADING:
        with state_lock:
            return float(state.get("balance",1000.0))
    try:
        path = "/api/mix/v1/account/assets"
        r = requests.get(BASE_URL + path, headers=build_headers('GET',path,''), timeout=8).json()
        if r.get('code') == '00000':
            for a in r['data']:
                if a.get('marginCoin') == 'USDT':
                    return float(a.get('availableBalance',0.0))
    except Exception:
        pass
    return 0.0

# positions
def get_positions():
    try:
        path = f"/api/mix/v1/position/singlePosition?symbol={SYMBOL}&marginCoin=USDT"
        r = requests.get(BASE_URL + path, headers=build_headers('GET',path,''), timeout=8).json()
        if r.get('code') == '00000':
            pos = {'LONG': None, 'SHORT': None}
            for p in r['data']:
                pos[p['holdSide']] = p
            return pos
    except Exception:
        pass
    return {'LONG': None, 'SHORT': None}

# place market order
def place_market_order(side, size, holdSide, leverage):
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
        # simulate: store minimal info
        with state_lock:
            state['last_action'] = f"SIM_{side}_{holdSide}_{size}"
            state['last_update'] = datetime.utcnow().isoformat()
            # update balance ~ crude: don't deduct margin to keep simple
            save_state(state)
        return {"code":"00000","data":{"sim":"ok"}}
    try:
        path = "/api/mix/v1/order/placeOrder"
        body_s = json.dumps(body)
        r = requests.post(BASE_URL + path, headers=build_headers('POST',path,body_s), data=body_s, timeout=10).json()
        return r
    except Exception as e:
        return {"code":"err","msg":str(e)}

# close position
def close_position(holdSide):
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
        with state_lock:
            state['positions'][holdSide] = 0
            state['last_action'] = f"SIM_CLOSE_{holdSide}"
            state['last_update'] = datetime.utcnow().isoformat()
            save_state(state)
        return {"code":"00000","data":{"sim":"ok"}}
    try:
        path = "/api/mix/v1/order/placeOrder"
        body_s = json.dumps(body)
        r = requests.post(BASE_URL + path, headers=build_headers('POST',path,body_s), data=body_s, timeout=10).json()
        return r
    except Exception as e:
        return {"code":"err","msg":str(e)}

# ------------------------------
# Market signals & news & heuristics
# ------------------------------
def coingecko_price():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", timeout=6).json()
        p = r.get("ethereum",{}).get("usd")
        return float(p) if p else None
    except:
        return None

def newsapi_fetch(q="ethereum OR eth", page_size=10):
    if not NEWSAPI_KEY:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {"q": q, "pageSize": page_size, "language":"en", "apiKey": NEWSAPI_KEY}
    try:
        r = requests.get(url, params=params, timeout=6).json()
        return r.get("articles", [])
    except:
        return []

def simple_sentiment(text):
    t = (text or "").lower()
    sc = 0.0
    for w in POS_WORDS:
        if w in t: sc += 1
    for w in NEG_WORDS:
        if w in t: sc -= 1
    if sc > 0: return min(1.0, sc/5.0)
    if sc < 0: return max(-1.0, sc/5.0)
    return 0.0

def assess_news(articles):
    # credibility by cross-source + official domains
    titles = [a.get('title','') for a in articles[:10]]
    sets = [set(t.lower().split()[:8]) for t in titles]
    matches = 0
    for i in range(len(sets)):
        for j in range(i+1,len(sets)):
            if len(sets[i].intersection(sets[j])) >= 3: matches += 1
    cross = min(1.0, matches / max(1, len(sets)))
    official_hosts = ["ethereum.org","cointelegraph.com","coindesk.com","reuters.com","coinbase.com","binance.com"]
    official_count = sum(1 for a in articles if any(h in (a.get('url') or '').lower() for h in official_hosts))
    official = min(1.0, official_count / 3.0)
    cred = 0.4*cross + 0.6*official
    sents = [simple_sentiment((a.get('title','')+" "+(a.get('description') or ""))) for a in articles[:8]]
    avg_sent = float(np.mean(sents)) if sents else 0.0
    return cred, avg_sent

def orderbook_imbalance():
    ob = fetch_orderbook(50)
    if not ob: return 0.0
    bids = ob.get('bids',[])[:15]
    asks = ob.get('asks',[])[:15]
    sum_b = sum(float(b[1]) for b in bids) if bids else 0
    sum_a = sum(float(a[1]) for a in asks) if asks else 0
    if sum_a + sum_b == 0: return 0.0
    return (sum_b - sum_a) / (sum_b + sum_a)

def ema_signal(closes):
    s = pd.Series(closes)
    ema5 = s.ewm(span=5).mean().iloc[-1]
    ema30 = s.ewm(span=30).mean().iloc[-1]
    return ema5, ema30

# ------------------------------
# Decision logic + sizing
# ------------------------------
def determine_action():
    candles = fetch_candles(200)
    if not candles or len(candles) < 60:
        return "HOLD", 0.0, None
    closes = [float(c[4]) for c in candles]
    price = closes[-1]
    ema5, ema30 = ema_signal(closes)
    pred_short = price + (ema5 - ema30)
    rel = (pred_short - price) / price if price else 0.0
    # news
    arts = newsapi_fetch("ethereum OR eth") if NEWSAPI_KEY else []
    cred, news_sent = assess_news(arts) if arts else (0.5, 0.0)
    obi = orderbook_imbalance()
    # confidence
    conf_model = min(1.0, max(0.0, abs(rel)*200))
    conf_news = min(1.0, max(0.0, abs(news_sent)*2)) * cred
    conf_book = min(1.0, abs(obi))
    confidence = 0.6*conf_model + 0.3*conf_news + 0.1*conf_book
    # action
    threshold = 0.0025  # 0.25%
    action = "HOLD"
    if rel > threshold:
        action = "BUY"
    elif rel < -threshold:
        action = "SELL"
    # news override if very credible and strong
    if cred > 0.6 and abs(news_sent) > 0.15:
        action = "BUY" if news_sent > 0 else "SELL"
    return action, float(min(1.0,confidence)), price

def map_conf_to_leverage(conf):
    lev = int(round(MIN_LEVERAGE + (MAX_LEVERAGE - MIN_LEVERAGE) * (conf ** 1.5)))
    return max(MIN_LEVERAGE, min(MAX_LEVERAGE, lev))

def compute_qty(balance, price, leverage):
    margin_pct = max(0.01, MIN_MARGIN_USAGE_PERCENT/100.0)
    notional = balance * margin_pct * leverage
    qty = int(notional // max(1.0, price))
    return max(0, qty)

# ------------------------------
# Risk checks (liquidation/drawdown)
# ------------------------------
def risk_checks(holdSide, entry_price):
    # check current position via API if available
    pos = get_positions().get(holdSide)
    if not pos: return False
    try:
        current_price = float(get_last_price() or entry_price)
        liq = float(pos.get('liquidationPrice') or 0)
        if liq > 0:
            dist = abs(current_price - liq) / liq
            if dist < LIQ_DIST_THRESHOLD:
                close_position(holdSide)
                return True
        # drawdown
        if holdSide == 'LONG':
            draw = (entry_price - current_price) / entry_price
        else:
            draw = (current_price - entry_price) / entry_price
        if draw > DRAWDOWN_CLOSE_PCT:
            close_position(holdSide)
            return True
    except:
        pass
    return False

def get_last_price():
    candles = fetch_candles(2)
    if candles:
        try:
            return float(candles[0][4])
        except:
            try:
                return float(candles[-1][4])
            except:
                pass
    p = coingecko_price()
    return p

# ------------------------------
# Bot main loop
# ------------------------------
running = False
bot_thread = None

def bot_loop():
    global running
    print("Bot iniciado (loop). Paper:", PAPER_TRADING)
    while running:
        try:
            action, confidence, price = determine_action()
            balance = get_balance()
            if not price:
                time.sleep(POLL_INTERVAL)
                continue
            leverage = map_conf_to_leverage(confidence)
            qty = compute_qty(balance, price, leverage)
            print(f"[{datetime.utcnow().isoformat()}] action={action} conf={confidence:.3f} lev={leverage} qty={qty} price={price:.2f}")
            # execute
            if action == "BUY" and qty > 0:
                # close short if exists
                close_position("SHORT")
                res = place_market_order("BUY", qty, "LONG", leverage)
                print("order:", res)
            elif action == "SELL" and qty > 0:
                close_position("LONG")
                res = place_market_order("SELL", qty, "SHORT", leverage)
                print("order:", res)
            # risk management
            # (basic: if position exists check drawdown/liquidation)
            pos = get_positions()
            # naive tracking: we won't compute P&L here; state updated minimal
            with state_lock:
                state["balance"] = get_balance()
                state["last_action"] = f"{action}:{confidence:.3f}"
                state["last_update"] = datetime.utcnow().isoformat()
                save_state(state)
        except Exception as e:
            print("Erro bot loop:", e)
        time.sleep(POLL_INTERVAL)
    print("Bot parado")

# ------------------------------
# Flask routes (UI control)
# ------------------------------
@app.route("/")
def index():
    with state_lock:
        s = load_state()
    return render_template_string(INDEX_HTML, paper=str(PAPER_TRADING), state=json.dumps(s, indent=2))

@app.route("/start", methods=["POST"])
def start():
    global running, bot_thread
    if running:
        return "Bot já em execução", 200
    running = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return "Bot iniciado", 200

@app.route("/stop", methods=["POST"])
def stop():
    global running
    if not running:
        return "Bot não estava rodando", 200
    running = False
    return "Bot parado", 200

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

# ------------------------------
# Entrypoint
# ------------------------------
if __name__ == "__main__":
    # ensure state file exists
    save_state(state)
    # Run Flask dev server (local). For production (Render) use gunicorn:
    # gunicorn app:app --bind 0.0.0.0:$PORT
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
=======
# app.py
# Bot único (Flask UI + motor de trading Bitget + sinais NewsAPI/CoinGecko/Etherscan)
# Uso: python app.py
# Recomendo rodar em servidor (Render.com) em produção. Teste localmente com PAPER_TRADING=true.

import os, time, json, math, hmac, hashlib, base64, threading
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
import requests
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

# -------------------------
# CONFIG (leitura via ENV)
# -------------------------
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")            # newsapi.org
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")# etherscan.io
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")# optional (CoinGecko basic doesn't need key)

SYMBOL = os.getenv("SYMBOL", "ethusdt_UMCBL")         # Bitget symbol
MIN_LEVERAGE = int(os.getenv("MIN_LEVERAGE", "9"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "60"))
MIN_MARGIN_USAGE_PERCENT = float(os.getenv("MIN_MARGIN_USAGE_PERCENT", "80.0"))  # percent
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1","true","yes")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))   # seconds between cycles
DRAWDOWN_CLOSE_PCT = float(os.getenv("DRAWDOWN_CLOSE_PCT", "0.03"))  # 3% drawdown -> close
LIQ_DIST_THRESHOLD = float(os.getenv("LIQ_DIST_THRESHOLD", "0.03"))  # 3% distance to liquidation -> close

STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

# Fees (approx) — adjust if needed; used to ensure minimal profitable moves
BITGET_FEE_RATE = float(os.getenv("BITGET_FEE_RATE", "0.0006"))  # 0.06% example taker fee

# ------------------------------
# Minimal positive/negative words (fast sentiment)
# ------------------------------
POS_WORDS = {"upgrade","bull","bullish","gain","surge","improve","positive","adopt","partnership","launch","success"}
NEG_WORDS = {"drop","bear","bearish","sell","scam","hack","downgrade","negative","regulation","fail","attack"}

# ------------------------------
# Flask app (UI)
# ------------------------------
app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<title>ETH Bot Control</title>
<h2>Bot ETHUSDT (Bitget) - Painel</h2>
<p>Paper trading: <b>{{paper}}</b></p>
<form method="post" action="/start"><button type="submit">Iniciar Bot</button></form>
<form method="post" action="/stop"><button type="submit">Parar Bot</button></form>
<form method="get" action="/status"><button type="submit">Status</button></form>
<hr>
<p>Último status (JSON):</p>
<pre id="state">{{state}}</pre>
<script>
setInterval(function(){
  fetch('/status_json').then(r=>r.json()).then(d=>{
    document.getElementById('state').textContent=JSON.stringify(d, null, 2);
  });
}, 4000);
</script>
"""

# ------------------------------
# State persistence
# ------------------------------
state_lock = threading.Lock()
def load_state():
    try:
        with open(STATE_FILE,"r") as f:
            return json.load(f)
    except:
        s = {"balance":1000.0, "positions": {"LONG":0,"SHORT":0}, "last_action":None, "profit":0.0, "last_update":None}
        save_state(s)
        return s

def save_state(s):
    with state_lock:
        with open(STATE_FILE,"w") as f:
            json.dump(s, f, indent=2)

state = load_state()

# ------------------------------
# Bitget REST helpers
# ------------------------------
BASE_URL = "https://api.bitget.com"

def ts_ms():
    return str(int(time.time()*1000))

def sign_message(prehash, secret):
    mac = hmac.new(secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def build_headers(method, request_path, body=''):
    timestamp = ts_ms()
    prehash = timestamp + method.upper() + request_path + (body or '')
    signature = sign_message(prehash, BITGET_API_SECRET or "")
    return {
        'ACCESS-KEY': BITGET_API_KEY or "",
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE or "",
        'Content-Type': 'application/json'
    }

# public: fetch candles
def fetch_candles(limit=200):
    try:
        path = f"/api/mix/v1/market/candles?symbol={SYMBOL}&period=1min&limit={limit}"
        r = requests.get(BASE_URL + path, timeout=8).json()
        if r.get('code') == '00000':
            return r['data']
    except Exception:
        pass
    return []

# public: orderbook
def fetch_orderbook(size=50):
    try:
        path = f"/api/mix/v1/market/depth?symbol={SYMBOL}&size={size}"
        r = requests.get(BASE_URL + path, timeout=6).json()
        if r.get('code') == '00000':
            return r['data']
    except Exception:
        pass
    return None

# account balance (USDT)
def get_balance():
    if PAPER_TRADING:
        with state_lock:
            return float(state.get("balance",1000.0))
    try:
        path = "/api/mix/v1/account/assets"
        r = requests.get(BASE_URL + path, headers=build_headers('GET',path,''), timeout=8).json()
        if r.get('code') == '00000':
            for a in r['data']:
                if a.get('marginCoin') == 'USDT':
                    return float(a.get('availableBalance',0.0))
    except Exception:
        pass
    return 0.0

# positions
def get_positions():
    try:
        path = f"/api/mix/v1/position/singlePosition?symbol={SYMBOL}&marginCoin=USDT"
        r = requests.get(BASE_URL + path, headers=build_headers('GET',path,''), timeout=8).json()
        if r.get('code') == '00000':
            pos = {'LONG': None, 'SHORT': None}
            for p in r['data']:
                pos[p['holdSide']] = p
            return pos
    except Exception:
        pass
    return {'LONG': None, 'SHORT': None}

# place market order
def place_market_order(side, size, holdSide, leverage):
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
        # simulate: store minimal info
        with state_lock:
            state['last_action'] = f"SIM_{side}_{holdSide}_{size}"
            state['last_update'] = datetime.utcnow().isoformat()
            # update balance ~ crude: don't deduct margin to keep simple
            save_state(state)
        return {"code":"00000","data":{"sim":"ok"}}
    try:
        path = "/api/mix/v1/order/placeOrder"
        body_s = json.dumps(body)
        r = requests.post(BASE_URL + path, headers=build_headers('POST',path,body_s), data=body_s, timeout=10).json()
        return r
    except Exception as e:
        return {"code":"err","msg":str(e)}

# close position
def close_position(holdSide):
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
        with state_lock:
            state['positions'][holdSide] = 0
            state['last_action'] = f"SIM_CLOSE_{holdSide}"
            state['last_update'] = datetime.utcnow().isoformat()
            save_state(state)
        return {"code":"00000","data":{"sim":"ok"}}
    try:
        path = "/api/mix/v1/order/placeOrder"
        body_s = json.dumps(body)
        r = requests.post(BASE_URL + path, headers=build_headers('POST',path,body_s), data=body_s, timeout=10).json()
        return r
    except Exception as e:
        return {"code":"err","msg":str(e)}

# ------------------------------
# Market signals & news & heuristics
# ------------------------------
def coingecko_price():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", timeout=6).json()
        p = r.get("ethereum",{}).get("usd")
        return float(p) if p else None
    except:
        return None

def newsapi_fetch(q="ethereum OR eth", page_size=10):
    if not NEWSAPI_KEY:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {"q": q, "pageSize": page_size, "language":"en", "apiKey": NEWSAPI_KEY}
    try:
        r = requests.get(url, params=params, timeout=6).json()
        return r.get("articles", [])
    except:
        return []

def simple_sentiment(text):
    t = (text or "").lower()
    sc = 0.0
    for w in POS_WORDS:
        if w in t: sc += 1
    for w in NEG_WORDS:
        if w in t: sc -= 1
    if sc > 0: return min(1.0, sc/5.0)
    if sc < 0: return max(-1.0, sc/5.0)
    return 0.0

def assess_news(articles):
    # credibility by cross-source + official domains
    titles = [a.get('title','') for a in articles[:10]]
    sets = [set(t.lower().split()[:8]) for t in titles]
    matches = 0
    for i in range(len(sets)):
        for j in range(i+1,len(sets)):
            if len(sets[i].intersection(sets[j])) >= 3: matches += 1
    cross = min(1.0, matches / max(1, len(sets)))
    official_hosts = ["ethereum.org","cointelegraph.com","coindesk.com","reuters.com","coinbase.com","binance.com"]
    official_count = sum(1 for a in articles if any(h in (a.get('url') or '').lower() for h in official_hosts))
    official = min(1.0, official_count / 3.0)
    cred = 0.4*cross + 0.6*official
    sents = [simple_sentiment((a.get('title','')+" "+(a.get('description') or ""))) for a in articles[:8]]
    avg_sent = float(np.mean(sents)) if sents else 0.0
    return cred, avg_sent

def orderbook_imbalance():
    ob = fetch_orderbook(50)
    if not ob: return 0.0
    bids = ob.get('bids',[])[:15]
    asks = ob.get('asks',[])[:15]
    sum_b = sum(float(b[1]) for b in bids) if bids else 0
    sum_a = sum(float(a[1]) for a in asks) if asks else 0
    if sum_a + sum_b == 0: return 0.0
    return (sum_b - sum_a) / (sum_b + sum_a)

def ema_signal(closes):
    s = pd.Series(closes)
    ema5 = s.ewm(span=5).mean().iloc[-1]
    ema30 = s.ewm(span=30).mean().iloc[-1]
    return ema5, ema30

# ------------------------------
# Decision logic + sizing
# ------------------------------
def determine_action():
    candles = fetch_candles(200)
    if not candles or len(candles) < 60:
        return "HOLD", 0.0, None
    closes = [float(c[4]) for c in candles]
    price = closes[-1]
    ema5, ema30 = ema_signal(closes)
    pred_short = price + (ema5 - ema30)
    rel = (pred_short - price) / price if price else 0.0
    # news
    arts = newsapi_fetch("ethereum OR eth") if NEWSAPI_KEY else []
    cred, news_sent = assess_news(arts) if arts else (0.5, 0.0)
    obi = orderbook_imbalance()
    # confidence
    conf_model = min(1.0, max(0.0, abs(rel)*200))
    conf_news = min(1.0, max(0.0, abs(news_sent)*2)) * cred
    conf_book = min(1.0, abs(obi))
    confidence = 0.6*conf_model + 0.3*conf_news + 0.1*conf_book
    # action
    threshold = 0.0025  # 0.25%
    action = "HOLD"
    if rel > threshold:
        action = "BUY"
    elif rel < -threshold:
        action = "SELL"
    # news override if very credible and strong
    if cred > 0.6 and abs(news_sent) > 0.15:
        action = "BUY" if news_sent > 0 else "SELL"
    return action, float(min(1.0,confidence)), price

def map_conf_to_leverage(conf):
    lev = int(round(MIN_LEVERAGE + (MAX_LEVERAGE - MIN_LEVERAGE) * (conf ** 1.5)))
    return max(MIN_LEVERAGE, min(MAX_LEVERAGE, lev))

def compute_qty(balance, price, leverage):
    margin_pct = max(0.01, MIN_MARGIN_USAGE_PERCENT/100.0)
    notional = balance * margin_pct * leverage
    qty = int(notional // max(1.0, price))
    return max(0, qty)

# ------------------------------
# Risk checks (liquidation/drawdown)
# ------------------------------
def risk_checks(holdSide, entry_price):
    # check current position via API if available
    pos = get_positions().get(holdSide)
    if not pos: return False
    try:
        current_price = float(get_last_price() or entry_price)
        liq = float(pos.get('liquidationPrice') or 0)
        if liq > 0:
            dist = abs(current_price - liq) / liq
            if dist < LIQ_DIST_THRESHOLD:
                close_position(holdSide)
                return True
        # drawdown
        if holdSide == 'LONG':
            draw = (entry_price - current_price) / entry_price
        else:
            draw = (current_price - entry_price) / entry_price
        if draw > DRAWDOWN_CLOSE_PCT:
            close_position(holdSide)
            return True
    except:
        pass
    return False

def get_last_price():
    candles = fetch_candles(2)
    if candles:
        try:
            return float(candles[0][4])
        except:
            try:
                return float(candles[-1][4])
            except:
                pass
    p = coingecko_price()
    return p

# ------------------------------
# Bot main loop
# ------------------------------
running = False
bot_thread = None

def bot_loop():
    global running
    print("Bot iniciado (loop). Paper:", PAPER_TRADING)
    while running:
        try:
            action, confidence, price = determine_action()
            balance = get_balance()
            if not price:
                time.sleep(POLL_INTERVAL)
                continue
            leverage = map_conf_to_leverage(confidence)
            qty = compute_qty(balance, price, leverage)
            print(f"[{datetime.utcnow().isoformat()}] action={action} conf={confidence:.3f} lev={leverage} qty={qty} price={price:.2f}")
            # execute
            if action == "BUY" and qty > 0:
                # close short if exists
                close_position("SHORT")
                res = place_market_order("BUY", qty, "LONG", leverage)
                print("order:", res)
            elif action == "SELL" and qty > 0:
                close_position("LONG")
                res = place_market_order("SELL", qty, "SHORT", leverage)
                print("order:", res)
            # risk management
            # (basic: if position exists check drawdown/liquidation)
            pos = get_positions()
            # naive tracking: we won't compute P&L here; state updated minimal
            with state_lock:
                state["balance"] = get_balance()
                state["last_action"] = f"{action}:{confidence:.3f}"
                state["last_update"] = datetime.utcnow().isoformat()
                save_state(state)
        except Exception as e:
            print("Erro bot loop:", e)
        time.sleep(POLL_INTERVAL)
    print("Bot parado")

# ------------------------------
# Flask routes (UI control)
# ------------------------------
@app.route("/")
def index():
    with state_lock:
        s = load_state()
    return render_template_string(INDEX_HTML, paper=str(PAPER_TRADING), state=json.dumps(s, indent=2))

@app.route("/start", methods=["POST"])
def start():
    global running, bot_thread
    if running:
        return "Bot já em execução", 200
    running = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return "Bot iniciado", 200

@app.route("/stop", methods=["POST"])
def stop():
    global running
    if not running:
        return "Bot não estava rodando", 200
    running = False
    return "Bot parado", 200

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

# ------------------------------
# Entrypoint
# ------------------------------
if __name__ == "__main__":
    # ensure state file exists
    save_state(state)
    # Run Flask dev server (local). For production (Render) use gunicorn:
    # gunicorn app:app --bind 0.0.0.0:$PORT
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
>>>>>>> 1bb16bdc4e5d3ba9341b3a8f55204ba5645deb87
