import time
import ccxt
from datetime import datetime

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
BASE_POSITION_VOLUME = 6   # Твоя нова початкова ставка в USDT
LEVERAGE = 10               # Базове плече
TIMEOUT_SECONDS = 1800      # 30 хвилин для 15м свічок
BREAKEVEN_TRIGGER_PCT = 0.6 # Перенесення в БУ при проходженні 60% до TP
MAX_CONCURRENT_TRADES = 3   # Максимум одночасних угод

ASSET_PROFILES = {
    'BTC/USDT:USDT':  ['CONTR_TREND', 0.0025, 0.0012, 20], # Мікро-стоп, плече 20x
    'ETH/USDT:USDT':  ['BREAKOUT_LONG', 0.0045, 0.0015, 10],# Тільки ЛОНГ на пробій
    'SOL/USDT:USDT':  ['CONTR_TREND', 0.0042, 0.0015, 10], 
    'ADA/USDT:USDT':  ['CONTR_TREND', 0.0030, 0.0022, 10], 
    'DOT/USDT:USDT':  ['CONTR_TREND', 0.0035, 0.0020, 10], 
    'DOGE/USDT:USDT': ['CONTR_TREND', 0.0022, 0.0015, 10], 
    'XRP/USDT:USDT':  ['CONTR_TREND', 0.0028, 0.0018, 10]  
}
DEFAULT_PROFILE = ['CONTR_TREND', 0.003, 0.002, 10]

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
# --- ЧИСТА МАТЕМАТИКА (БЕЗ PANDAS) ---
# ==============================================================================
def safe_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def calculate_ema_list(prices, period):
    if len(prices) < period: return [0] * len(prices)
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [0] * (period - 1) + ema

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
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_macd_hist(prices):
    if len(prices) < 35: return [0.0] * len(prices)
    ema12 = calculate_ema_list(prices, 12)
    ema26 = calculate_ema_list(prices, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    
    # Сигнальна лінія — це EMA(9) від лінії MACD
    k = 2 / (9 + 1)
    signal_line = [0.0] * 25
    signal_line.append(sum(macd_line[25:34]) / 9)
    for m in macd_line[34:]:
        signal_line.append(m * k + signal_line[-1] * (1 - k))
        
    return [m - s for m, s in zip(macd_line, signal_line)]

def get_market_data(symbol, timeframe, limit=60):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 35: return None
        closes = [safe_float(b[4]) for b in bars]
        volumes = [safe_float(b[5]) for b in bars]
        return closes, volumes
    except: return None

# ==============================================================================
# --- МОДУЛЬ УПРАВЛІННЯ ТА ЗАХИСТУ ---
# ==============================================================================
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
    except: pass
    return real_positions

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

            if not has_sl or not has_tp:
                sl_side = 'sell' if is_long else 'buy'
                sl_price = entry_price * (1 - sl_pct) if is_long else entry_price * (1 + sl_pct)
                tp_price = entry_price * (1 + tp_pct) if is_long else entry_price * (1 - tp_pct)

                precise_sl = float(exchange.price_to_precision(symbol, sl_price))
                precise_tp = float(exchange.price_to_precision(symbol, tp_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))

                if not has_sl: exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_sl})
                if not has_tp: exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_tp})
                print(f"🛡️ [ЗАХИСТ МАТРИЦІ] {symbol.split('/')[0]} | SL: {precise_sl} | TP: {precise_tp}")
                continue

            target_pnl_to_activate = BASE_POSITION_VOLUME * current_leverage * tp_pct * BREAKEVEN_TRIGGER_PCT
            if unrealized_pnl >= target_pnl_to_activate and not has_breakeven_sl:
                if old_sl_id:
                    try: exchange.cancel_order(old_sl_id, symbol)
                    except: pass
                precise_entry = float(exchange.price_to_precision(symbol, entry_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))
                exchange.create_order(symbol, 'market', 'sell' if is_long else 'buy', precise_amount, params={'stopPrice': precise_entry})
                print(f"🔥 [БЕЗУБИТОК] {symbol.split('/')[0]} | Стоп у нуль: {precise_entry}")
        except Exception as e: print(f"❌ Помилка модуля захисту {symbol}: {e}")

def place_trap_order(symbol, side, price):
    try:
        profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
        current_leverage = profile[3]
        
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
        print(f"❌ Помилка створення лімітки {symbol}: {e}")
        return None, 0, 0

def clean_orphan_orders(symbol, real_positions):
    try:
        set_exchange_context()
        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders and symbol not in real_positions:
            for order in open_orders:
                if order.get('stopPrice'):
                    exchange.cancel_order(order['id'], symbol)
    except: pass

def handle_traps_and_timeouts(symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)
        if order['status'] in ['closed', 'filled']:
            del active_traps[symbol]
        elif order['status'] == 'canceled': del active_traps[symbol]
        elif time.time() - trap['placed_time'] >= TIMEOUT_SECONDS:
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
    print(f"🚀 Розумний бот Lyra V4.1 [БЕЗ PANDAS - LIGHTWEIGHT] запущений.")
    exchange.load_markets()

    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()

            if real_positions: 
                control_and_protect_positions(real_positions)

            if current_time.hour != last_heartbeat_hour:
                print(f"\n📊 [{current_time.strftime('%H:%M')}] === МОНІТОРИНГ СВЯТОГО ГРААЛЯ ===")
                print(f"Активних угод: {len(real_positions)} / Макс: {MAX_CONCURRENT_TRADES} | Базова ставка: {BASE_POSITION_VOLUME} USDT")
                last_heartbeat_hour = current_time.hour

            for symbol in SYMBOLS:
                clean_orphan_orders(symbol, real_positions)
                handle_traps_and_timeouts(symbol)

            if len(real_positions) >= MAX_CONCURRENT_TRADES:
                time.sleep(15)
                continue

            for symbol in SYMBOLS:
                if symbol in real_positions or symbol in active_traps: continue

                data = get_market_data(symbol, TIMEFRAME_TRADE)
                if not data: continue
                closes, volumes = data

                # Розрахунок чистих індикаторів
                rsi = calculate_rsi(closes)
                macd_hist = calculate_macd_hist(closes)
                
                avg_vol_20 = sum(volumes[-21:-1]) / 20
                vol_ratio = volumes[-2] / avg_vol_20 if avg_vol_20 > 0 else 0.0
                current_price = closes[-1]

                hist_curr = macd_hist[-2]
                hist_prev = macd_hist[-3]

                profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
                strat_type = profile[0]

                # СТРАТЕГІЯ 1: Контр-тренд
                if strat_type == 'CONTR_TREND':
                    if rsi < 35 and hist_curr > hist_prev and vol_ratio > 1.0:
                        target_entry = current_price * 0.9985 
                        print(f"🟢 [LONG] {symbol.split('/')[0]} | RSI: {rsi:.1f} | Vol: {vol_ratio:.1f}x -> Вхід: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', target_entry)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time()}

                    elif rsi > 65 and hist_curr < hist_prev and vol_ratio > 1.0:
                        target_entry = current_price * 1.0015
                        print(f"🔴 [SHORT] {symbol.split('/')[0]} | RSI: {rsi:.1f} | Vol: {vol_ratio:.1f}x -> Вхід: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', target_entry)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time()}

                # СТРАТЕГІЯ 2: Пробойний Лонг (ETH)
                elif strat_type == 'BREAKOUT_LONG':
                    if rsi > 65 and hist_curr > hist_prev and vol_ratio > 1.3:
                        print(f"🔥 [🚀 ПРОБІЙ ETH] RSI: {rsi:.1f} | Потужний імпульс! Заходимо.")
                        oid, size, prc = place_trap_order(symbol, 'buy', current_price)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time()}

            time.sleep(15)
        except Exception as e:
            print(f"🚨 Помилка головного циклу: {e}")
            time.sleep(10)

if __name__ == "__main__": 
    main_cycle()
