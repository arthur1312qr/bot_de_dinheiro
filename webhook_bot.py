import os, ccxt
from flask import Flask, request, jsonify

app = Flask(__name__)

exchange = ccxt.bitget({
    'apiKey': os.environ['BITGET_APIKEY'],
    'secret': os.environ['BITGET_SECRET'],
    'password': os.environ['BITGET_PASSPHRASE'],
    'enableRateLimit': True,
})

# memória simples p/ tendência confirmada pelo 1H
trend_state = {"trend": "NONE"}

@app.route('/ping')
def ping():
    return 'ok', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    global trend_state
    data = request.get_json(force=True)

    # --- Atualização de tendência (1H) ---
    if "trend" in data:
        trend = data["trend"].upper()
        if trend in ["UP","DOWN","NONE"]:
            trend_state["trend"] = trend
            return jsonify({"status":"trend_updated","trend":trend}), 200
        return jsonify({"error":"invalid_trend"}), 400

    # --- Sinais de gatilho (5m) ---
    action = data.get("action","").upper()
    symbol = data.get("symbol","WLF/USDT").upper()
    price = float(data.get("price",0) or 0)

    # Checa se a tendência permite essa ação
    if trend_state["trend"] == "UP" and action != "BUY":
        return jsonify({"ignored":"trend_UP_allows_only_BUY"}), 200
    if trend_state["trend"] == "DOWN" and action != "SELL":
        return jsonify({"ignored":"trend_DOWN_allows_only_SELL"}), 200
    if trend_state["trend"] == "NONE":
        return jsonify({"ignored":"no_trend_confirmation"}), 200

    try:
        exchange.load_markets()
    except Exception as e:
        return jsonify({"error":"load_markets_failed","msg":str(e)}), 500

    # tenta setar alavancagem
    try:
        exchange.set_leverage(10, symbol)
    except Exception:
        pass

    try:
        bal = exchange.fetch_free_balance()
    except Exception as e:
        return jsonify({"error":"fetch_balance_failed","msg":str(e)}), 500

    quote = symbol.split('/')[-1]
    base = symbol.split('/')[0]
    usdt = bal.get(quote, 0)

    try:
        if action == "BUY" and usdt > 0:
            invest_pct = float(os.environ.get("INVEST_PCT", "0.95"))
            use_amount = usdt * invest_pct
            if price <= 0:
                price = exchange.fetch_ticker(symbol)["last"]
            amount_base = use_amount / price
            order = exchange.create_market_buy_order(symbol, amount_base, params={"leverage":10,"marginMode":"isolated"})
        elif action == "SELL":
            base_amt = bal.get(base, 0)
            if base_amt <= 0:
                return jsonify({"error":"no_base_balance"}), 400
            order = exchange.create_market_sell_order(symbol, base_amt, params={"marginMode":"isolated"})
        else:
            return jsonify({"error":"invalid_action_or_balance"}), 400

        return jsonify({"status":"ok","order":order,"trend":trend_state["trend"]}), 200

    except Exception as e:
        return jsonify({"error":str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
