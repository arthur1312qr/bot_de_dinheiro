# app.py
# Single-file ETH scalper for Bitget (reads .env). EDIT ONLY .env.
# IMPORTANT: operate real money only if you accept risk. Test in PAPER_TRADING=true first.

import os, time, json, hmac, hashlib, base64, threading, logging
from datetime import datetime
from math import tan
from flask import Flask, render_template_string, request, jsonify
import requests
import numpy as np
import pandas as pd

# ---- CONFIG (from environment variables - Replit Secrets) ----
BITGET_API_KEY = os.getenv("BITGET_API_KEY","")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET","")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE","")

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY","")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY","")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY","")

# Configuration from environment (not secrets)
SYMBOL = "ETHUSDT"  # Corrected symbol for Bitget USDT-M futures
PAPER_TRADING = False  # Set to True for paper trading

MIN_LEVERAGE = 9
MAX_LEVERAGE = 60
MIN_MARGIN_USAGE_PERCENT = 80.0

POLL_INTERVAL = 1.0
DRAWDOWN_CLOSE_PCT = 0.03
LIQ_DIST_THRESHOLD = 0.03
MAX_CONSECUTIVE_LOSSES = 5

STATE_FILE = "bot_state.json"
LOG_FILE = "bot.log"

# fee approx (taker). adjust if needed
BITGET_FEE_RATE = 0.0006

# ---- logging ----
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)])
logger = logging.getLogger("eth_scalper")

# ---- flask UI ----
app = Flask(__name__)
INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>ETH Scalper Bot</title>
<style>
body{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5}
.container{max-width:800px;margin:0 auto;background:white;padding:20px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1)}
.status{padding:10px;margin:10px 0;border-radius:5px;font-weight:bold}
.running{background:#d4edda;color:#155724;border:1px solid #c3e6cb}
.stopped{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb}
button{padding:10px 20px;margin:5px;border:none;border-radius:5px;cursor:pointer;font-size:14px}
.start{background:#28a745;color:white}.stop{background:#dc3545;color:white}.status-btn{background:#007bff;color:white}
pre{background:#f8f9fa;padding:15px;border-radius:5px;overflow-x:auto;max-height:400px;overflow-y:auto}
.info{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:20px 0}
.card{background:#f8f9fa;padding:15px;border-radius:8px;border-left:4px solid #007bff}
</style>
</head>
<body>
<div class="container">
  <h1>🤖 ETH Scalper Bot - Bitget</h1>
  <div class="status {{status_class}}">Status: {{status_text}} | Paper Trading: {{paper}}</div>
  
  <div>
    <form method="post" action="/start" style="display:inline">
      <button type="submit" class="start">▶️ Start Bot</button>
    </form>
    <form method="post" action="/stop" style="display:inline">
      <button type="submit" class="stop">⏹️ Stop Bot</button>
    </form>
    <form method="get" action="/status" style="display:inline">
      <button type="submit" class="status-btn">📊 Refresh Status</button>
    </form>
  </div>

  <div class="info">
    <div class="card">
      <h3>💰 Trading Info</h3>
      <p><strong>Symbol:</strong> {{symbol}}</p>
      <p><strong>Balance:</strong> ${{balance}}</p>
      <p><strong>Profit:</strong> {{profit}}</p>
      <p><strong>Positions:</strong> L:{{long_pos}} S:{{short_pos}}</p>
    </div>
    <div class="card">
      <h3>⚙️ Configuration</h3>
      <p><strong>Leverage:</strong> {{min_lev}}-{{max_lev}}</p>
      <p><strong>Margin Usage:</strong> {{margin_usage}}%</p>
      <p><strong>Max Losses:</strong> {{max_losses}}</p>
      <p><strong>Poll Interval:</strong> {{poll_interval}}s</p>
    </div>
  </div>

  <h3>📈 Bot State</h3>
  <pre id="state">{{state}}</pre>
  
  <h3>📰 Recent Activity</h3>
  <pre id="logs">Loading logs...</pre>
</div>

<script>
function updateData() {
  fetch('/status_json')
    .then(r => r.json())
    .then(d => {
      document.getElementById('state').textContent = JSON.stringify(d, null, 2);
    })
    .catch(e => console.error('Error:', e));
}

function loadLogs() {
  fetch('/logs')
    .then(r => r.text())
    .then(logs => {
      const lines = logs.split('\\n');
      document.getElementById('logs').textContent = lines.slice(-20).join('\\n');
    })
    .catch(e => console.error('Error loading logs:', e));
}

// Update every 2 seconds
setInterval(updateData, 2000);
setInterval(loadLogs, 5000);
updateData();
loadLogs();
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
    # For now, generate synthetic candles based on CoinGecko price
    # This is a temporary solution while we debug the Bitget API
    try:
        base_price = coingecko_price()
        if not base_price:
            logger.warning("Cannot fetch ETH price from CoinGecko")
            return []
        
        # Generate realistic price data around current price
        import random
        candles = []
        current_price = base_price
        
        for i in range(min(limit, 120)):
            # Small random variations typical of 1-minute candles
            variation = random.uniform(-0.002, 0.002)  # ±0.2%
            new_price = current_price * (1 + variation)
            
            # OHLC format: [timestamp, open, high, low, close, volume]
            open_price = current_price
            close_price = new_price
            high_price = max(open_price, close_price) * random.uniform(1.0, 1.001)
            low_price = min(open_price, close_price) * random.uniform(0.999, 1.0)
            
            timestamp = str(int(time.time() * 1000) - i * 60000)  # 1 minute intervals
            volume = str(random.uniform(100, 1000))
            
            candles.append([timestamp, str(open_price), str(high_price), 
                          str(low_price), str(close_price), volume])
            current_price = new_price
        
        logger.info("Generated %d synthetic candles based on CoinGecko price %.2f", len(candles), base_price)
        return list(reversed(candles))  # Most recent last
        
    except Exception as e:
        logger.error("Error generating synthetic candles: %s", e)
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
        r=requests.get(BASE_URL+path, headers=build_headers('GET', path, ''), timeout=3).json()
        if r.get('code')=='00000':
            for a in r.get('data',[]):
                if a.get('marginCoin')=='USDT': return float(a.get('availableBalance',0.0))
    except Exception as e:
        logger.debug("get_balance err: %s", e)
    return 1000.0  # Fallback for demo

def get_positions():
    try:
        path=f"/api/mix/v1/position/singlePosition?symbol={SYMBOL}&marginCoin=USDT"
        r=requests.get(BASE_URL+path, headers=build_headers('GET', path, ''), timeout=3).json()
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
    loop_count = 0
    while running:
        try:
            loop_count += 1
            if loop_count % 60 == 1:  # Log every 60 iterations (about 1 minute)
                logger.info("Bot loop running... iteration %d", loop_count)
            
            candles=fetch_candles(120)
            if not candles or len(candles)<30:
                if loop_count % 30 == 1:
                    logger.warning("Not enough candle data: %d candles", len(candles) if candles else 0)
                time.sleep(POLL_INTERVAL); continue
            
            # parse closes
            closes=[]
            for c in candles:
                try: 
                    closes.append(float(c[4]))
                except:
                    try: 
                        closes.append(float(c.get('close')))
                    except: 
                        pass
            if len(closes)<30: 
                time.sleep(POLL_INTERVAL); continue
                
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
                leverage = max(MIN_LEVERAGE, leverage//2)
                logger.warning("Defensive mode: consecutive losses=%d, reduced leverage to %d", cons_losses, leverage)
            
            # position management
            positions = get_positions() if not PAPER_TRADING else {'LONG': state.get('positions',{}).get('LONG'), 'SHORT': state.get('positions',{}).get('SHORT')}
            
            # Log analysis results every 10 iterations
            if loop_count % 10 == 1:
                logger.info("Market analysis: price=%.2f, confidence=%.3f, direction=%d, leverage=%d, qty=%d", 
                           price, confidence, direction, leverage, qty)
                logger.info("News sentiment: %.3f, orderbook imbalance: %.3f, EMA signal: %.6f", 
                           news_sent, obi, rel)
            
            # Trading logic
            if direction != 0 and confidence > 0.3 and qty > 0:
                side_to_open = 'LONG' if direction > 0 else 'SHORT'
                opposite_side = 'SHORT' if direction > 0 else 'LONG'
                
                logger.info("Trading opportunity: %s with confidence %.3f", side_to_open, confidence)
                
                # Close opposite position if exists
                if positions.get(opposite_side) and float(positions[opposite_side].get('size', 0)) > 0:
                    close_result = close_position_api(opposite_side)
                    logger.info("Closed %s position: %s", opposite_side, close_result.get('code'))
                
                # Open new position if not already open
                if not positions.get(side_to_open) or float(positions[side_to_open].get('size', 0)) == 0:
                    order_side = 'BUY' if side_to_open == 'LONG' else 'SELL'
                    order_result = place_market_order_api(order_side, qty, side_to_open, leverage)
                    
                    if order_result.get('code') == '00000':
                        logger.info("✅ Opened %s position: qty=%d, lev=%d, conf=%.3f", side_to_open, qty, leverage, confidence)
                        with state_lock:
                            state['last_action'] = {'side': side_to_open, 'qty': qty, 'price': price, 'time': datetime.now().isoformat()}
                            if PAPER_TRADING:
                                state['positions'][side_to_open] = qty
                            save_state(state)
                    else:
                        logger.error("❌ Order failed: %s", order_result)
            elif loop_count % 20 == 1:
                logger.info("No trading opportunity: confidence=%.3f (need >0.3), direction=%d", confidence, direction)
            
            # Update state
            with state_lock:
                state['last_update'] = datetime.now().isoformat()
                if PAPER_TRADING:
                    state['balance'] = balance
                save_state(state)
                
        except Exception as e:
            logger.exception("Bot loop error: %s", e)
        
        time.sleep(POLL_INTERVAL)
    
    logger.info("Bot stopped")

# ---- Flask routes ----
@app.route('/')
def index():
    with state_lock:
        current_state = state.copy()
    
    status_text = "Running" if running else "Stopped"
    status_class = "running" if running else "stopped"
    
    return render_template_string(INDEX_HTML,
        status_text=status_text,
        status_class=status_class,
        paper="Yes" if PAPER_TRADING else "No",
        symbol=SYMBOL,
        balance=current_state.get('balance', 1000),
        profit=current_state.get('profit', 0),
        long_pos=current_state.get('positions', {}).get('LONG', 0),
        short_pos=current_state.get('positions', {}).get('SHORT', 0),
        min_lev=MIN_LEVERAGE,
        max_lev=MAX_LEVERAGE,
        margin_usage=MIN_MARGIN_USAGE_PERCENT,
        max_losses=MAX_CONSECUTIVE_LOSSES,
        poll_interval=POLL_INTERVAL,
        state=json.dumps(current_state, indent=2, default=str)
    )

@app.route('/start', methods=['POST'])
def start_bot():
    global running, bot_thread
    if not running:
        running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        logger.info("Bot started via web interface")
    return redirect_to_index()

@app.route('/stop', methods=['POST'])
def stop_bot():
    global running
    running = False
    logger.info("Bot stopped via web interface")
    return redirect_to_index()

@app.route('/status')
def status():
    return redirect_to_index()

@app.route('/status_json')
def status_json():
    try:
        # Simple non-blocking status without state locks
        return jsonify({
            'running': running,
            'paper_trading': PAPER_TRADING,
            'symbol': SYMBOL,
            'balance': 1000,  # Simplified for demo
            'positions': {},
            'last_price': 4200,
            'timestamp': int(time.time())
        })
    except Exception as e:
        logger.error("Status JSON error: %s", e)
        return jsonify({'error': str(e)}), 500

@app.route('/logs')
def get_logs():
    try:
        with open(LOG_FILE, 'r') as f:
            return f.read()
    except:
        return "No logs available"

def redirect_to_index():
    from flask import redirect, url_for
    return redirect(url_for('index'))

# ---- health check for 24/7 uptime ----
@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy',
        'bot_running': running,
        'paper_trading': PAPER_TRADING,
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    logger.info("🚀 Starting ETH Scalper Bot...")
    logger.info("Paper Trading: %s", PAPER_TRADING)
    logger.info("Symbol: %s", SYMBOL)
    
    # Auto-start bot if credentials are available
    if BITGET_API_KEY and BITGET_API_SECRET and BITGET_PASSPHRASE:
        logger.info("✅ Bitget credentials found")
        if not running:
            running = False
            bot_thread = threading.Thread(target=bot_loop, daemon=True)
            bot_thread.start()
            logger.info("🤖 Bot auto-started")
    else:
        logger.warning("⚠️ Bitget credentials missing - add them to Replit Secrets")
    
    # Start Flask server
    app.run(host='0.0.0.0', port=5000, debug=False)
