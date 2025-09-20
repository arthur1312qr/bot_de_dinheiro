import os, ccxt
from flask import Flask, request, jsonify

app = Flask(__name__)

exchange = ccxt.bitget({
    'apiKey': os.environ['BITGET_APIKEY'],
    'secret': os.environ['BITGET_SECRET'],
    'password': os.environ['BITGET_PASSPHRASE'],
    'enableRateLimit': True,
})

@app.route('/ping')
def ping():
    return 'ok', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    action = data.get('action','').upper()
    symbol = data.get('symbol','WLF/USDT').upper()
    price = float(data.get('price', 0) or 0)

    exchange.load_markets()
    try:
        exchange.set_leverage(10, symbol)
    except Exception:
        pass

    bal = exchange.fetch_free_balance()
    quote = symbol.split('/')[-1]
    base = symbol.split('/')[0]
    usdt = bal.get(quote, 0)

    try:
        if action == 'BUY' and usdt > 0:
            invest_pct = float(os.environ.get('INVEST_PCT', '0.95'))
            use_amount = usdt * invest_pct
            if price <= 0:
                price = exchange.fetch_ticker(symbol)['last']
            amount_base = use_amount / price
            order = exchange.create_market_buy_order(symbol, amount_base, params={'leverage':10, 'marginMode':'isolated'})
        elif action == 'SELL':
            base_amt = bal.get(base, 0)
            order = exchange.create_market_sell_order(symbol, base_amt, params={'marginMode':'isolated'})
        else:
            return jsonify({'error':'invalid_action_or_balance'}), 400

        return jsonify({'status':'ok','order':order}), 200

    except Exception as e:
        return jsonify({'error':str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
