import time
import ccxt

# --- НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ ---
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'ONDO/USDT', 'LINK/USDT', 'NEAR/USDT', 'RENDER/USDT', 'FET/USDT', 'SOL/USDT', 'SUI/USDT']
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

active_traps = {}

# --- МАТЕМАТИЧНІ ФУНКЦІЇ НА ЧИСТОМУ PYTHON ---
def calculate_ema(prices, period):
    """Розрахунок EMA без pandas"""
    if len(prices) < period: return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Початкове SMA
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_rsi(prices, period=14):
    """Розрахунок RSI без pandas"""
    if len(prices) < period + 1: return 50
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0: return 100
    
    # Згладжування за методом Уеллса Вайлдера
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_dynamic_threshold(volumes):
    """Динамічний коефіцієнт об'єму на чистому Python"""
    if len(volumes) < 20: return 1.6
    last_20 = volumes[-20:]
    mean_vol = sum(last_20) / 20
    if mean_vol == 0: return 1.6
    
    # Стандартне відхилення вручну
    variance = sum((x - mean_vol) ** 2 for x in last_20) / 20
    std_vol = variance ** 0.5
    
    cv = std_vol / mean_vol
    threshold = 1.6 + (cv * 0.5)
    return min(max(threshold, 1.6), 2.5)

# --- ЛОГІКА БОТА ---
def get_crypto_close_and_volume(symbol, timeframe):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]
        return closes, volumes
    except Exception as e:
        print(f"❌ Помилка даних для {symbol}: {e}")
        return None, None

def check_global_trend(symbol):
    closes, _ = get_crypto_close_and_volume(symbol, TIMEFRAME_TREND)
    if not closes: return "FLAT"
    ema_50 = calculate_ema(closes, 50)
    if closes[-1] > ema_50: return "LONG_ONLY"
    if closes[-1] < ema_50: return "SHORT_ONLY"
    return "FLAT"

def has_active_position(symbol):
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0: return True
        return False
    except:
        return True

def handle_traps_timeout(current_candle_idx, symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        order = exchange.fetch_order(trap['order_id'], symbol)
        if order['status'] in ['closed', 'canceled']:
            del active_traps[symbol]
            return
        if current_candle_idx - trap['placed_at_candle_idx'] >= 2:
            print(f"⏰ [ТАЙМАУТ ПАСТКИ] Видаляємо лімітку по {symbol}")
            exchange.cancel_order(trap['order_id'], symbol)
            del active_traps[symbol]
    except Exception as e:
        print(f"❌ Помилка пастки для {symbol}: {e}")

def main_cycle():
    print("🤖 Бот Lyra V2 (Без панд) активований.")
    while True:
        try:
            for symbol in SYMBOLS:
                if has_active_position(symbol): continue
                
                closes, volumes = get_crypto_close_and_volume(symbol, TIMEFRAME_TRADE)
                if not closes: continue
                
                current_candle_idx = len(closes)
                handle_traps_timeout(current_candle_idx, symbol)
                if symbol in active_traps: continue

                # Рахуємо індикатори через наші чисті функції
                ema_12 = calculate_ema(closes, 12)
                rsi = calculate_rsi(closes, 14)
                
                last_20_vols = volumes[-20:]
                avg_vol_20 = sum(last_20_vols) / 20
                dynamic_coef = calculate_dynamic_threshold(volumes)
                
                if volumes[-1] > (avg_vol_20 * dynamic_coef):
                    global_trend = check_global_trend(symbol)
                    
                    if global_trend == "LONG_ONLY" and rsi < 65 and closes[-1] > ema_12:
                        print(f"🕸️ [ПАСТКА LONG] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на {ema_12}")
                        amount = 5.0 / ema_12
                        order = exchange.create_order(symbol, 'limit', 'buy', amount, ema_12)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx}
                        
                    elif global_trend == "SHORT_ONLY" and rsi > 35 and closes[-1] < ema_12:
                        print(f"🕸️ [ПАСТКА SHORT] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на {ema_12}")
                        amount = 5.0 / ema_12
                        order = exchange.create_order(symbol, 'limit', 'sell', amount, ema_12)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx}
                        
                    elif global_trend == "LONG_ONLY" and rsi >= 65:
                        print(f"🛡️ [ЗАХИСТ] По {symbol} пре зелений імпульс. Шорт заблоковано.")
            time.sleep(30)
        except Exception as e:
            print(f"🚨 Помилка: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()

# КІНЕЦЬ КОДУ. БЕЗ ПАНДАС, БЕЗ КОНФЛІКТІВ ВЕРСІЙ! 👍
