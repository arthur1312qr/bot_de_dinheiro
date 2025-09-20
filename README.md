## Bot de Trading Bitget + TradingView

### Deploy no Render
1. Conecte este repositório no Render → New → Web Service.
2. Start command:
   gunicorn webhook_bot:app --bind 0.0.0.0:$PORT
3. Crie variáveis de ambiente no Render:
   - BITGET_APIKEY
   - BITGET_SECRET
   - BITGET_PASSPHRASE
   - INVEST_PCT (ex: 0.95)

### TradingView
- Webhook URL:
  https://SEUAPP.onrender.com/webhook
- Mensagem BUY:
  {"action":"BUY","symbol":"WLF/USDT","price":{{close}}}
- Mensagem SELL:
  {"action":"SELL","symbol":"WLF/USDT","price":{{close}}}
