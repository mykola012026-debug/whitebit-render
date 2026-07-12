import time
import ccxt
from datetime import datetime
import pandas as pd
import numpy as np
import pandas_ta as ta
import sys
import subprocess

# Перевірка бібліотеки pandas_ta перед стартом
try:
    import pandas_ta as ta
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas_ta"])
    import pandas_ta as ta

# ==============================================================================
# --- НАЛАШТУВАННЯ ТА БЕЗПЕКА НА ОСНОВІ НАШОЇ МАТРИЦІ ---
# ==============================================================================
SYMBOLS = [
    'BTC/USDT:USDT', 
    'ETH/USDT:USDT', 
    'SOL/USDT:USDT', 
    'ADA/USDT:USDT',
    'DOT/USDT:USDT',
    'DOGE/USDT:USDT',
    'XRP/USDT:USDT'
]

TIMEFRAME_TRADE = '15m'
BASE_POSITION_VOLUME = 10  # Об'єм першої позиції в USDT
LEVERAGE = 10               # Базове плече
TIMEOUT_SECONDS = 1800      # 30 хвилин для 15м свічок (щоб пастка не висіла довго)
BREAKEVEN_TRIGGER_PCT = 0.6 # Перенесення в БУ при проходженні 60% до TP
MAX_CONCURRENT_TRADES = 3   # Розширили до 3, бо маємо топ-10 кошик

# Індивідуальні профілі: [Напрямок_Стратегії, TP_PERCENT, SL_PERCENT, Особливе_Плече]
# На основі аналізу: Рух закриття, Макс. вихід вгору/вниз
ASSET_PROFILES = {
    'BTC/USDT:USDT':  ['CONTR_TREND', 0.0025, 0.0012, 20], # Мікро-стоп, плече 20x
    'ETH/USDT:USDT':  ['BREAKOUT_LONG', 0.0045, 0.0015, 10],# Тільки ЛОНГ на пробій
    'SOL/USDT:USDT':  ['CONTR_TREND', 0.0042, 0.0015, 10], # Ідеальний контр-шорт
    'ADA/USDT:USDT':  ['CONTR_TREND', 0.0030, 0.0022, 10], # Стабільний контр-лонг
    'DOT/USDT:USDT':  ['CONTR_TREND', 0.0035, 0.0020, 10], # Глибокі відскоки
    'DOGE/USDT:USDT': ['CONTR_TREND', 0.0022, 0.0015, 10], # Імпульсний контр-шорт
    'XRP/USDT:USDT':  ['CONTR_TREND', 0.0028, 0.0018, 10]  # Шортова дамп-монета
}
DEFAULT_PROFILE = ['CONTR_TREND', 0.003, 0.002, 10]

# API підключення до WhiteBIT
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1', 
    'secret': '4ff8480b5bb8914e4dacf7ac40401762', 
    'enableRateLimit': True, 
    'options': {'defaultType': 'swap'}
})

active_traps = {}
position_history_cache = {}
last_heartbeat_hour = -1

# ==============================================================================
# --- МАТЕМАТИЧНИЙ ТА СТАТИСТИЧНИЙ МОДУЛЬ (PANDAS_TA) ---
# ==============================================================================
def safe_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def get_market_indicators(symbol, timeframe, limit=50):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 30: return None
        
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
            
        # Розрахунок RSI та MACD через pandas_ta
        df['RSI'] = ta.rsi(df['close'], length=14)
        macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
        if macd_df is not None:
            df['MACD_hist'] = macd_df.iloc[:, 2]
            
        df['rolling_vol_avg'] = df['volume'].rolling(window=20).mean()
        return df
    except Exception as e:
        print(f"⚠️ Помилка прорахунку індикаторів для {symbol}: {e}")
        return None

def set_exchange_context():
    exchange.options['accountsByType'] = {'swap': 'collateral'}

def get_active_positions():
    set_exchange_context()
    real_positions = {}
    try:
        positions = exchange.fetch_positions(SYMBOLS)
        for pos in positions:
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            symbol = pos.get('symbol')
            if symbol and abs(p_size) > 0.000001: real_positions[symbol] = pos
    except Exception as e: print(f"⚠️ Помилка отримання позицій: {e}")
    return real_positions

# ==============================================================================
# --- МОДУЛЬ УПРАВЛІННЯ ОРДЕРАМИ ТА АДАПТИВНОГО ЗАХИСТУ ---
# ==============================================================================
def control_and_protect_positions(real_positions):
    for symbol, pos in real_positions.items():
        try:
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            is_long = p_size > 0
            entry_price = safe_float(pos.get('entryPrice') or pos.get('info', {}).get('entryPrice'))
            unrealized_pnl = safe_float(pos.get('unrealizedPnl') or pos.get('info', {}).get('pnl'))

            profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
            tp_pct, sl_pct, current_leverage = profile[1], profile[2], profile[3]

            if symbol not in position_history_cache:
                position_history_cache[symbol] = {'entry_price': entry_price, 'open_time': datetime.now()}

            open_orders = exchange.fetch_open_orders(symbol)
            has_sl, has_tp, has_breakeven_sl = False, False, False
            old_sl_id = None

            for order in open_orders:
                o_price = safe_float(order.get('stopPrice') or order.get('info', {}).get('stopPrice'))
                if o_price > 0:
                    if abs(o_price - entry_price) / entry_price < 0.0005: has_breakeven_sl, has_sl = True, True
                    elif (is_long and o_price < entry_price) or (not is_long and o_price > entry_price): has_sl, old_sl_id = True, order['id']
                    else: has_tp = True

            # Виставлення індивідуальних TP/SL на основі нашої матриці
            if not has_sl or not has_tp:
                sl_side = 'sell' if is_long else 'buy'
                sl_price = entry_price * (1 - sl_pct) if is_long else entry_price * (1 + sl_pct)
                tp_price = entry_price * (1 + tp_pct) if is_long else entry_price * (1 - tp_pct)

                precise_sl = float(exchange.price_to_precision(symbol, sl_price))
                precise_tp = float(exchange.price_to_precision(symbol, tp_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))

                if not has_sl: exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_sl})
                if not has_tp: exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_tp})
                print(f"🛡️ [ЗАХИСТ МАТРИЦІ] {symbol.split('/')[0]} | SL: {precise_sl} ({sl_pct*100:.2f}%) | TP: {precise_tp} ({tp_pct*100:.2f}%)")
                continue

            # Розрахунок безубитку з урахуванням кастомного плеча монети
            target_pnl_to_activate = BASE_POSITION_VOLUME * current_leverage * tp_pct * BREAKEVEN_TRIGGER_PCT
            if unrealized_pnl >= target_pnl_to_activate and not has_breakeven_sl:
                if old_sl_id:
                    try: exchange.cancel_order(old_sl_id, symbol)
                    except: pass
                precise_entry = float(exchange.price_to_precision(symbol, entry_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))
                exchange.create_order(symbol, 'market', 'sell' if is_long else 'buy', precise_amount, params={'stopPrice': precise_entry})
                print(f"🔥 [БЕЗУБИТОК] {symbol.split('/')[0]} | Ризик знято. Стоп у нуль: {precise_entry}")
        except Exception as e: print(f"❌ Помилка модуля захисту {symbol}: {e}")

def place_trap_order(symbol, side, price):
    try:
        profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
        current_leverage = profile[3]
        
        # Встановлюємо плече для конкретного активу
        try: exchange.set_leverage(current_leverage, symbol)
        except: pass
        
        amount_contracts = (BASE_POSITION_VOLUME * current_leverage) / price
        min_qty = safe_float(exchange.market(symbol)['limits']['amount']['min'], 0.001)
        if amount_contracts < min_qty: amount_contracts = min_qty
        
        precise_amount = float(exchange.amount_to_precision(symbol, amount_contracts))
        precise_price = float(exchange.price_to_precision(symbol, price))
        
        set_exchange_context()
        order = exchange.create_order(symbol, 'limit', side, precise_amount, precise_price)
        return order['id'], precise_amount, precise_price
    except Exception as e:
        print(f"❌ Помилка створення лімітки для {symbol}: {e}")
        return None, 0, 0

def clean_orphan_orders(symbol, real_positions):
    try:
        set_exchange_context()
        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders and symbol not in real_positions:
            for order in open_orders:
                if order.get('stopPrice'):
                    print(f"🧹 [CLEAN] Видалено залишений стоп захисту для {symbol.split('/')[0]}")
                    exchange.cancel_order(order['id'], symbol)
    except: pass

def handle_traps_and_timeouts(symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)
        if order['status'] in ['closed', 'filled']:
            print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] {symbol.split('/')[0]} | Вхід у позицію підтверджено.")
            del active_traps[symbol]
        elif order['status'] == 'canceled': del active_traps[symbol]
        elif time.time() - trap['placed_time'] >= TIMEOUT_SECONDS:
            print(f"⏰ [ТАЙМАУТ] {symbol.split('/')[0]} | Скасування ордера за часом.")
            try: exchange.cancel_order(trap['order_id'], symbol)
            except: pass
            del active_traps[symbol]
    except Exception as e:
        if "NOT_FOUND" in str(e).upper(): del active_traps[symbol]

# ==============================================================================
# --- ГОЛОВНИЙ ЦИКЛ БОТА ---
# ==============================================================================
def main_cycle():
    global last_heartbeat_hour
    print(f"🚀 Розумний бот Lyra V4.0 [КОМБО МАТРИЦЯ] запущений.")
    exchange.load_markets()

    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()

            if real_positions: 
                control_and_protect_positions(real_positions)

            # Панель керування раз на годину
            if current_time.hour != last_heartbeat_hour:
                print(f"\n📊 [{current_time.strftime('%H:%M')}] === МОНІТОРИНГ СВЯТОГО ГРААЛЯ ===")
                print(f"Активних угод: {len(real_positions)} / Макс: {MAX_CONCURRENT_TRADES}")
                last_heartbeat_hour = current_time.hour

            for symbol in SYMBOLS:
                clean_orphan_orders(symbol, real_positions)
                handle_traps_and_timeouts(symbol)

            # Перевірка ліміту паралельних угод
            if len(real_positions) >= MAX_CONCURRENT_TRADES:
                time.sleep(15)
                continue

            # Аналіз нових точок входу
            for symbol in SYMBOLS:
                if symbol in real_positions or symbol in active_traps: continue

                df = get_market_indicators(symbol, TIMEFRAME_TRADE)
                if df is None or len(df) < 3: continue

                # Останній закритий рядок (свічка N)
                i = len(df) - 2 
                rsi = df.loc[i, 'RSI']
                hist_curr = df.loc[i, 'MACD_hist']
                hist_prev = df.loc[i-1, 'MACD_hist']
                vol_ratio = df.loc[i, 'volume'] / df.loc[i, 'rolling_vol_avg']
                current_price = df.loc[i+1, 'close']

                profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
                strat_type = profile[0]

                # -----------------------------------------------------------------
                # СТРАТЕГІЯ 1: Контр-тренд (Для BTC, SOL, ADA, DOT, DOGE, XRP)
                # -----------------------------------------------------------------
                if strat_type == 'CONTR_TREND':
                    # Зв'язуємо Combo LONG: RSI низький + розворот гістограми вгору + об'єм
                    if rsi < 35 and hist_curr > hist_prev and vol_ratio > 1.0:
                        # Заходимо ліміткою трохи нижче ринку для ідеального зняття відкату
                        target_entry = current_price * 0.9985 
                        print(f"🟢 [СИГНАЛ LONG] {symbol.split('/')[0]} | RSI: {rsi:.1f} | Vol: {vol_ratio:.1f}x -> Вхід: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', target_entry)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time()}

                    # Зв'язуємо Combo SHORT: RSI високий + розворот гістограми вниз + об'єм
                    elif rsi > 65 and hist_curr < hist_prev and vol_ratio > 1.0:
                        target_entry = current_price * 1.0015
                        print(f"🔴 [СИГНАЛ SHORT] {symbol.split('/')[0]} | RSI: {rsi:.1f} | Vol: {vol_ratio:.1f}x -> Вхід: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', target_entry)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time()}

                # -----------------------------------------------------------------
                # СТРАТЕГІЯ 2: Пробойний Лонг (Виключно для Ефіру)
                # -----------------------------------------------------------------
                elif strat_type == 'BREAKOUT_LONG':
                    # Якщо Ефір розганяється в перекупленість на великих об'ємах — стрибаємо в потяг
                    if rsi > 65 and hist_curr > hist_prev and vol_ratio > 1.3:
                        print(f"🔥 [🚀 ПРОБІЙ ETH ЛОНГ] RSI: {rsi:.1f} | Потужний імпульс! Вхід по ринку.")
                        # Для пробою заходимо ліміткою прямо в поточну ціну (майже маркет), щоб забрати миттєво
                        oid, size, prc = place_trap_order(symbol, 'buy', current_price)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time()}

            time.sleep(15)
        except Exception as e:
            print(f"🚨 Помилка головного циклу: {e}")
            time.sleep(10)

if __name__ == "__main__": 
    main_cycle()
