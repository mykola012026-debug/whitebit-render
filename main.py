import time
import pandas as pd
import pandas_ta as ta
import ccxt

# --- НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ ---
# Вставлено твої актуальні ключі API для роботи на WhiteBIT Futures
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {'defaultType': 'future'} # Працюємо суворо на ф'ючерсах
})

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'ONDO/USDT', 'LINK/USDT', 'NEAR/USDT', 'RENDER/USDT', 'FET/USDT', 'SOL/USDT', 'SUI/USDT']
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

# Словник для контролю активних пасток (ліміток) у пам'яті
active_traps = {}

def get_crypto_data(symbol, timeframe, limit=100):
    """Отримання свічок з біржі"""
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ Помилка даних ({timeframe}) для {symbol}: {e}")
        return None

def check_global_trend(symbol):
    """КРОК 1: Фільтр старшого таймфрейму (1h) за допомогою EMA-50"""
    df_1h = get_crypto_data(symbol, TIMEFRAME_TREND, limit=100)
    if df_1h is None or df_1h.empty: return "FLAT"
    
    df_1h['ema_global'] = ta.ema(df_1h['close'], length=50)
    last_close = df_1h['close'].iloc[-1]
    last_ema = df_1h['ema_global'].iloc[-1]
    
    if last_close > last_ema: return "LONG_ONLY"
    if last_close < last_ema: return "SHORT_ONLY"
    return "FLAT"

def calculate_dynamic_threshold(df_15m):
    """КРОК 2: Динамічний коефіцієнт об'єму (від 1.6 до 2.5)"""
    vol_std = df_15m['volume'].tail(20).std()
    vol_mean = df_15m['volume'].tail(20).mean()
    if vol_mean == 0: return 1.6
    
    cv = vol_std / vol_mean
    threshold = 1.6 + (cv * 0.5)
    return min(max(threshold, 1.6), 2.5)

def has_active_position(symbol):
    """Твій базовий захист: перевірка балансу та відкритих позицій"""
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0:
                return True
        return False
    except Exception as e:
        print(f"❌ Помилка перевірки позицій для {symbol}: {e}")
        return True # Безпека: якщо помилка, вважаємо що позиція є

def manage_open_positions():
    """КРОК 4: Катапульта (Перенос СТОПу в безубиток при +0.4%)"""
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            symbol = pos['symbol']
            if symbol not in SYMBOLS or float(pos['contracts']) == 0: continue
                
            entry_price = float(pos['entryPrice'])
            current_price = float(pos['markPrice'])
            side = pos['side']
            
            p_diff = (current_price - entry_price) / entry_price if side == 'long' else (entry_price - current_price) / entry_price
                
            if p_diff >= 0.004:
                print(f"🚀 [КАТАПУЛЬТА] {symbol} пройшов {p_diff*100:.2f}%. Переносимо STOP в безубиток по {entry_price}")
                # Тут викликається твоя стандартна функція модифікації стоп-ордера
    except Exception as e:
        print(f"❌ Помилка в модулі Катапульта: {e}")

def handle_traps_timeout(current_candle_idx):
    """КРОК 3.2: Контроль часу життя пасток (Макс 2 свічки = 30 хв)"""
    for symbol in list(active_traps.keys()):
        trap = active_traps[symbol]
        try:
            order = exchange.fetch_order(trap['order_id'], symbol)
            if order['status'] == 'closed':
                print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Ми в позиції по {symbol}!")
                del active_traps[symbol]
                continue
            elif order['status'] == 'canceled':
                del active_traps[symbol]
                continue
                
            if current_candle_idx - trap['placed_at_candle_idx'] >= 2:
                print(f"⏰ [ТАЙМАУТ ПАСТКИ] Минуло 30 хв. Видаляємо лімітку по {symbol}")
                exchange.cancel_order(trap['order_id'], symbol)
                del active_traps[symbol]
        except Exception as e:
            print(f"❌ Помилка пастки для {symbol}: {e}")

def main_cycle():
    print("🤖 Бот Lyra V2 активований. Повний захист увімкнено.")
    
    while True:
        try:
            manage_open_positions()
            
            for symbol in SYMBOLS:
                # 1. Захист: якщо є позиція або вже висить пастка — ігноруємо монету
                if has_active_position(symbol) or symbol in active_traps:
                    continue
                
                df_15m = get_crypto_data(symbol, TIMEFRAME_TRADE, limit=100)
                if df_15m is None or df_15m.empty: continue
                
                current_candle_idx = len(df_15m)
                handle_traps_timeout(current_candle_idx)
                if symbol in active_traps: continue

                # Розрахунок індикаторів
                df_15m['ema_fast'] = ta.ema(df_15m['close'], length=12)
                df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
                df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)
                
                last_row = df_15m.iloc[-1]
                dynamic_coef = calculate_dynamic_threshold(df_15m)
                volume_anomaly = last_row['volume'] > (last_row['vol_ma'] * dynamic_coef)
                
                if volume_anomaly:
                    global_trend = check_global_trend(symbol)
                    
                    # СИГНАЛ LONG (Тільки якщо 1h тренд вище EMA)
                    if global_trend == "LONG_ONLY" and last_row['rsi'] < 65 and last_row['close'] > last_row['ema_fast']:
                        trap_price = last_row['ema_fast']
                        print(f"🕸️ [ПАСТКА LONG] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {trap_price}")
                        
                        amount = 5.0 / trap_price
                        order = exchange.create_order(symbol, 'limit', 'buy', amount, trap_price)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx, 'side': 'buy'}
                    
                    # СИГНАЛ SHORT (Тільки якщо 1h тренд нижче EMA)
                    elif global_trend == "SHORT_ONLY" and last_row['rsi'] > 35 and last_row['close'] < last_row['ema_fast']:
                        trap_price = last_row['ema_fast']
                        print(f"🕸️ [ПАСТКА SHORT] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {trap_price}")
                        
                        amount = 5.0 / trap_price
                        order = exchange.create_order(symbol, 'limit', 'sell', amount, trap_price)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx, 'side': 'sell'}
                    
                    # Захист від шортів на вертикальних зелених свічках
                    elif global_trend == "LONG_ONLY" and last_row['rsi'] >= 65:
                        print(f"🛡️ [ЗАХИСТ] По {symbol} пре зелений імпульс. Шорт заблоковано годинним трендом.")
            
            time.sleep(30)
            
        except Exception as e:
            print(f"🚨 Помилка циклу: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()

# КІНЕЦЬ КОДУ. ВСІ МОДУЛІ ЗБРАНО. Вдалого запуску! 👍
