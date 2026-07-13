import time
import ccxt
from datetime import datetime

# ==============================================================================
# --- НАЛАШТУВАННЯ ТА БЕЗПЕКА (ЧИСТИЙ ФУНДАМЕНТАЛЬНИЙ СПОТ) ---
# ==============================================================================
# Залишено тільки активи першого ешелону (Tier-1) для довгострокового утримання
SYMBOLS = [
    'BTC/USDT', 
    'ETH/USDT', 
    'SOL/USDT'
]

TIMEFRAME_TRADE = '1h'          # Рекомендую 1h або 4h для зрізання шуму
BASE_BUY_VOLUME_USDT = 5       # Сума однієї покупки (кроку) в USDT
MAX_DCA_STEPS = 3               # Максимум доборів (усереднень) на одну монету
DCA_DROP_PERCENT = 0.05         # Закуповувати наступний крок при падінні на 5%

# Налаштування RSI для підбору на локальній паніці
ASSET_PROFILES = {
    'BTC/USDT':  [30], # BTC купуємо тільки на глибоких проливах (RSI <= 30)
    'ETH/USDT':  [32], 
    'SOL/USDT':  [30]  
}
DEFAULT_RSI_TRIGGER = 30

exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1', 
    'secret': '4ff8480b5bb8914e4dacf7ac40401762', 
    'enableRateLimit': True, 
    'options': {'defaultType': 'spot'}
})

spot_portfolio_tracker = {}
last_heartbeat_hour = -1

def safe_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def get_market_data(symbol, timeframe, limit=60):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 20: return None
        closes = [safe_float(b[4]) for b in bars]
        return closes
    except: return None

def execute_spot_buy(symbol, price, reason="Перший вхід"):
    try:
        amount_to_buy = BASE_BUY_VOLUME_USDT / price
        precise_amount = float(exchange.amount_to_precision(symbol, amount_to_buy))
        order = exchange.create_order(symbol, 'market', 'buy', precise_amount)
        print(f"🛒 [СПОТ ПОКУПКА] {symbol} за ціною {price} USDT | Причина: {reason}")
        return True
    except Exception as e:
        print(f"❌ Помилка ордера по {symbol}: {e}")
        return False

def main_cycle():
    global last_heartbeat_hour
    print(f"🚀 Розумний Спотовий DCA-Накопичувач запущений. Режим: Накопичення Tier-1 активів.")
    exchange.load_markets()

    while True:
        try:
            current_time = datetime.now()

            if current_time.hour != last_heartbeat_hour:
                btc_data = get_market_data('BTC/USDT', TIMEFRAME_TRADE)
                if btc_data:
                    b_rsi = calculate_rsi(btc_data)
                    print(f"\n📊 [{current_time.strftime('%H:%M')}] === МОНІТОРИНГ СПОТУ ===")
                    print(f"Ціна BTC: {btc_data[-1]} USDT | RSI ({TIMEFRAME_TRADE}): {b_rsi:.1f}")
                    print(f"Монет на доборі: {len(spot_portfolio_tracker)}")
                    print(f"=========================================\n")
                last_heartbeat_hour = current_time.hour

            for symbol in SYMBOLS:
                closes = get_market_data(symbol, TIMEFRAME_TRADE)
                if not closes: continue
                
                current_price = closes[-1]
                rsi = calculate_rsi(closes)
                
                profile = ASSET_PROFILES.get(symbol, [DEFAULT_RSI_TRIGGER])
                rsi_trigger = profile[0]

                if symbol not in spot_portfolio_tracker:
                    if rsi <= rsi_trigger:
                        success = execute_spot_buy(symbol, current_price, f"RSI перепроданість ({rsi:.1f} <= {rsi_trigger})")
                        if success:
                            spot_portfolio_tracker[symbol] = {'steps_done': 1, 'last_buy_price': current_price}
                else:
                    tracker = spot_portfolio_tracker[symbol]
                    last_price = tracker['last_buy_price']
                    steps = tracker['steps_done']
                    drop_pct = (last_price - current_price) / last_price
                    
                    if drop_pct >= DCA_DROP_PERCENT and steps < MAX_DCA_STEPS:
                        reason = f"DCA Крок {steps + 1} (Впало на {drop_pct*100:.1f}%)"
                        success = execute_spot_buy(symbol, current_price, reason)
                        if success:
                            spot_portfolio_tracker[symbol]['steps_done'] += 1
                            spot_portfolio_tracker[symbol]['last_buy_price'] = current_price
                            
                    elif current_price >= last_price * 1.10: 
                        print(f"✨ [ПРОФІТНИЙ ЦИКЛ] {symbol} виросла на 10%+ від останньої закупки. Скидаємо DCA-пам'ять.")
                        del spot_portfolio_tracker[symbol]

            time.sleep(30)
        except Exception as e:
            time.sleep(10)

if __name__ == "__main__": 
    main_cycle()
