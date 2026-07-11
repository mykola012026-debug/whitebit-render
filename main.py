import time
import ccxt
from datetime import datetime

# ==============================================================================
# --- НАЛАШТУВАННЯ ТА БЕЗПЕКА НА ОСНОВІ АУДИТУ РИНКУ ---
# ==============================================================================
# Залишено 5 топ-монет із найкращим співвідношенням імпульсу до шуму
SYMBOLS = [
    'BTC/USDT:USDT', 
    'ETH/USDT:USDT', 
    'SOL/USDT:USDT', 
    'SUI/USDT:USDT',  # Лідер за ефективністю (60.7%)
    'XRP/USDT:USDT'   # Найменший рівень шуму (0.30%)
]

TIMEFRAME_TRADE, TIMEFRAME_TREND = '15m', '1h'
BASE_POSITION_VOLUME = 10  # Об'єм першої позиції в USDT
LEVERAGE = 10               # Плече
VOLUME_MULTIPLIER = 1.1     # Тригер сплеску об'єму
TIMEOUT_SECONDS = 3600      # 1 година на скасування лімітки без відкату
BREAKEVEN_TRIGGER_PCT = 0.7 # Перенесення в БУ при проходженні 70% до TP
MAX_CONCURRENT_TRADES = 2   # МАКСИМУМ ОДНОЧАСНИХ УГОД (Захист від проливів)

# Індивідуальні профілі активів: [Множник_Оффсету, TP_PERCENT, SL_PERCENT]
ASSET_PROFILES = {
    'SUI/USDT:USDT': [0.9, 0.005, 0.006],   # Даємо забрати більший тейк
    'XRP/USDT:USDT': [0.8, 0.003, 0.004],   # Короткий стоп і швидкий вхід завдяки мікро-шуму
    'SOL/USDT:USDT': [1.0, 0.004, 0.006],   # Стандартний профіль
    'BTC/USDT:USDT': [1.0, 0.0025, 0.006],   # Стандартний профіль
    'ETH/USDT:USDT': [1.0, 0.0035, 0.006]    # Стандартний профіль
}
DEFAULT_PROFILE = [1.0, 0.008, 0.006]

# API підключення
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
# --- ДОПОМІЖНІ ТА АНАЛІТИЧНІ ФУНКЦІЇ ---
# ==============================================================================
def safe_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def calculate_ema(prices, period):
    if len(prices) < period: return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]: ema = price * k + ema * (1 - k)
    return ema

def get_ohlcv_data(symbol, timeframe, limit=100):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [b[4] for b in bars], [b[5] for b in bars]
    except: return None, None

def check_global_trend(symbol):
    closes, _ = get_ohlcv_data(symbol, TIMEFRAME_TREND)
    if not closes or len(closes) < 50: return "FLAT", 0
    ema_50 = calculate_ema(closes, 50)
    return ("LONG_ONLY" if closes[-1] > ema_50 else "SHORT_ONLY"), closes[-1]

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
    except Exception as e: print(f"⚠️ Помилка отримання реальних позицій: {e}")
    return real_positions

def get_position_protection_levels(symbol):
    tp, sl = "НЕМАЄ", "НЕМАЄ"
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        for order in open_orders:
            stop_price = safe_float(order.get('stopPrice'))
            if stop_price > 0:
                if tp == "НЕМАЄ": tp = f"{stop_price:.4f}"
                else: sl = f"{stop_price:.4f}"
    except: pass
    return tp, sl

# ==============================================================================
# --- МОДУЛЬ УПРАВЛІННЯ ОРДЕРАМИ ТА ЗАХИСТУ ---
# ==============================================================================
def execute_trade_audit(symbol, last_known_entry_price, vol_coeff, open_time):
    try:
        set_exchange_context()
        trades = exchange.fetch_my_trades(symbol, limit=5)
        if not trades: return
        trades = sorted(trades, key=lambda x: x['timestamp'], reverse=True)
        exit_trade = trades[0]
        exit_price = safe_float(exit_trade.get('price'))
        entry_price = last_known_entry_price if last_known_entry_price > 0 else safe_float(exit_trade.get('fee', {}).get('rate', 0))
        if exit_price == 0 or entry_price == 0: return
        
        is_long = exit_trade.get('side') == 'sell'
        price_change_pct = ((exit_price - entry_price) / entry_price) * 100 if is_long else ((entry_price - exit_price) / entry_price) * 100
        pnl_usd = (price_change_pct / 100) * BASE_POSITION_VOLUME * LEVERAGE
        pnl_capital_pct = price_change_pct * LEVERAGE
        duration_str = str(datetime.now() - open_time).split('.')[0] if open_time else "Невідомо"
        
        profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
        sl_pct = profile[2]
        
        if pnl_usd >= 0: reason = "🎯 TAKE_PROFIT (Ціль досягнута)"
        else:
            reason = "🛑 STOP_LOSS"
            slippage = (abs(price_change_pct) / 100) - sl_pct
            if slippage > 0.0005: reason += f" (🚨 ПРОСЛОВЗУВАННЯ / СЛІПЕЙДЖ: +{slippage * 100:.3f}%)"
            else: reason += " (Технічне закриття)"
            
        print(f"\n==================================================================")
        print(f"📊 [АУДИТ УГОДИ] #{symbol.split('/')[0]}-{'LONG' if is_long else 'SHORT'}")
        print(f"    • Вхід: {entry_price:.4f} | Вихід: {exit_price:.4f} | Результат: {pnl_usd:+.2f} USDT ({pnl_capital_pct:+.2f}%)")
        print(f"    • Тривалість угоди: {duration_str} | Причина: {reason}")
        print(f"    • Параметри входу: Сплєск об'єму був {vol_coeff:.2f}x від середнього")
        print(f"==================================================================\n")
    except Exception as e: print(f"⚠️ Не вдалося згенерувати аудит угоди для {symbol}: {e}")

def clean_orphan_orders(symbol, real_positions):
    try:
        set_exchange_context()
        if symbol in position_history_cache and symbol not in real_positions:
            cached = position_history_cache[symbol]
            execute_trade_audit(symbol, cached['entry_price'], cached['vol_ratio'], cached['open_time'])
            del position_history_cache[symbol]
            
        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders:
            for order in open_orders:
                if not order.get('stopPrice'):
                    closes_15m, _ = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    if closes_15m and len(closes_15m) >= 26:
                        ema_25 = calculate_ema(closes_15m, 25)
                        global_trend, _ = check_global_trend(symbol)
                        reason = ""
                        if global_trend == "LONG_ONLY" and closes_15m[-1] <= ema_25: reason = f"Ціна впала нижче EMA-25 ({closes_15m[-1]:.4f})"
                        elif global_trend == "SHORT_ONLY" and closes_15m[-1] >= ema_25: reason = f"Ціна піднялась вище EMA-25 ({closes_15m[-1]:.4f})"
                        if reason:
                            print(f"🧹 [CLEAN] {symbol.split('/')[0]} -> Пастку знято ({reason})")
                            exchange.cancel_order(order['id'], symbol)
                            if symbol in active_traps: del active_traps[symbol]
                elif order.get('stopPrice') and symbol not in real_positions:
                    print(f"🧹 [CLEAN] {symbol.split('/')[0]} -> Зачищено залишений стоп захисту")
                    exchange.cancel_order(order['id'], symbol)
    except Exception: pass

def control_and_protect_positions(real_positions):
    for symbol, pos in real_positions.items():
        try:
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            is_long = p_size > 0
            entry_price = safe_float(pos.get('entryPrice') or pos.get('info', {}).get('entryPrice'))
            unrealized_pnl = safe_float(pos.get('unrealizedPnl') or pos.get('info', {}).get('pnl'))
            
            profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
            tp_pct, sl_pct = profile[1], profile[2]
            
            if symbol not in position_history_cache:
                vol_ratio = active_traps.get(symbol, {}).get('vol_ratio', 1.4)
                position_history_cache[symbol] = {'entry_price': entry_price, 'vol_ratio': vol_ratio, 'open_time': datetime.now()}
                
            open_orders = exchange.fetch_open_orders(symbol)
            has_sl, has_tp, has_breakeven_sl = False, False, False
            old_sl_id = None
            
            for order in open_orders:
                o_price = safe_float(order.get('stopPrice') or order.get('info', {}).get('stopPrice'))
                if o_price > 0:
                    if abs(o_price - entry_price) / entry_price < 0.001: has_breakeven_sl, has_sl = True, True
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
                print(f"🛡️ [PROTECT] {symbol.split('/')[0]} | Адаптивний захист -> SL: {precise_sl} ({sl_pct*100}%) | TP: {precise_tp} ({tp_pct*100}%)")
                if symbol in active_traps: del active_traps[symbol]
                continue
                
            target_pnl_to_activate = BASE_POSITION_VOLUME * LEVERAGE * tp_pct * BREAKEVEN_TRIGGER_PCT
            if unrealized_pnl >= target_pnl_to_activate and not has_breakeven_sl:
                if old_sl_id:
                    try: exchange.cancel_order(old_sl_id, symbol)
                    except: pass
                precise_entry = float(exchange.price_to_precision(symbol, entry_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))
                exchange.create_order(symbol, 'market', 'sell' if is_long else 'buy', precise_amount, params={'stopPrice': precise_entry})
                print(f"🔥 [BREAKEVEN] {symbol.split('/')[0]} | Ризик знято! Стоп перенесено в нуль: {precise_entry}")
        except Exception as e: print(f"❌ Помилка модуля захисту {symbol.split('/')[0]}: {e}")

def place_trap_order(symbol, side, price):
    try:
        amount_contracts = (BASE_POSITION_VOLUME * LEVERAGE) / price
        min_qty = safe_float(exchange.market(symbol)['limits']['amount']['min'], 0.001)
        if amount_contracts < min_qty: amount_contracts = min_qty
        precise_amount = float(exchange.amount_to_precision(symbol, amount_contracts))
        precise_price = float(exchange.price_to_precision(symbol, price))
        set_exchange_context()
        order = exchange.create_order(symbol, 'limit', side, precise_amount, precise_price)
        return order['id'], precise_amount, precise_price
    except Exception as e:
        print(f"❌ Помилка створення пастки {symbol.split('/')[0]}: {e}")
        return None, 0, 0

def handle_traps_and_timeouts(symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)
        if order['status'] in ['closed', 'filled']:
            print(f"🕸️ [TRAP FILLED] {symbol.split('/')[0]} | Пастка спрацювала. Позицію відкрито.")
            del active_traps[symbol]
        elif order['status'] == 'canceled': del active_traps[symbol]
        elif time.time() - trap['placed_time'] >= TIMEOUT_SECONDS:
            print(f"⏰ [TIMEOUT] {symbol.split('/')[0]} | Видаляємо лімітку за таймаутом.")
            try: exchange.cancel_order(trap['order_id'], symbol)
            except: pass
            del active_traps[symbol]
    except Exception as e:
        if "NOT_FOUND" in str(e).upper() and symbol in active_traps: del active_traps[symbol]

def sync_existing_traps_on_startup():
    global active_traps
    set_exchange_context()
    real_positions = get_active_positions()
    for symbol in SYMBOLS:
        if symbol in real_positions: continue
        try:
            open_orders = exchange.fetch_open_orders(symbol)
            for order in open_orders:
                if order.get('status') == 'open' and not order.get('stopPrice'):
                    active_traps[symbol] = {'order_id': str(order['id']), 'placed_time': time.time(), 'side': order['side'].lower(), 'price': safe_float(order.get('price')), 'amount': safe_float(order.get('amount')), 'vol_ratio': 1.4}
                    print(f"🔗 [SYNC] Синхронізовано активну лімітку по {symbol.split('/')[0]}")
        except Exception: pass

# ==============================================================================
# --- ГОЛОВНИЙ ЦИКЛ ПРОГРАМИ ---
# ==============================================================================
def main_cycle():
    global last_heartbeat_hour
    print(f"🤖 Lyra V3.2 [NIGHT SAFE] запущена успішно.")
    exchange.load_markets()
    sync_existing_traps_on_startup()
    
    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()
            
            # Супроводжуємо вже відкриті угоди в будь-якому випадку
            if real_positions: 
                control_and_protect_positions(real_positions)
            
            # Логування панелі стану (раз на годину)
            if current_time.hour != last_heartbeat_hour:
                print(f"\n📊 [{current_time.strftime('%H:%M')}] === СТАН ПАНЕЛІ КЕРУВАННЯ ===")
                for symbol in SYMBOLS:
                    trend, last_p = check_global_trend(symbol)
                    closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    vol_str, dev_str = "0.00x", "0.00%"
                    if closes_15m and len(volumes_15m) >= 26:
                        ema_25 = calculate_ema(closes_15m, 25)
                        avg_vol = sum(volumes_15m[-21:-1]) / 20
                        vol_ratio = volumes_15m[-2] / avg_vol if avg_vol > 0 else 0
                        vol_str, dev_str = f"{vol_ratio:.2f}x", f"{(((closes_15m[-1] - ema_25)/ema_25)*100):+.2f}%" if ema_25 > 0 else "0.00%"
                    status = "Вільна"
                    if symbol in real_positions:
                        p = real_positions[symbol]
                        pnl = safe_float(p.get('unrealizedPnl') or p.get('info', {}).get('pnl'))
                        tp_val, sl_val = get_position_protection_levels(symbol)
                        status = f"ПОЗИЦІЯ | PnL: {pnl:.2f} USDT | Захист (TP: {tp_val} / SL: {sl_val})"
                    elif symbol in active_traps:
                        rem = max(0.0, (TIMEOUT_SECONDS - (time.time() - active_traps[symbol]['placed_time'])) / 60)
                        status = f"ПАСТКА ({active_traps[symbol]['side'].upper()}) по {active_traps[symbol]['price']} | Таймаут: {rem:.1f} хв"
                    print(f"  • {symbol.split('/')[0]:<7} | Цiна: {last_p:<9} | Trend: {trend:<10} | Vol: {vol_str:<6} | EMA-25: {dev_str:<7} | {status}")
                print(f"🔒 Навантаження: {len(real_positions)}/{MAX_CONCURRENT_TRADES} активних угод.")
                print("=========================================\n")
                last_heartbeat_hour = current_time.hour
            
            # Очищення та обробка таймаутів
            for symbol in SYMBOLS:
                clean_orphan_orders(symbol, real_positions)
                handle_traps_and_timeouts(symbol)
            
            # --- ЗАХИСНИЙ БЛОК: ОБМЕЖЕННЯ НА КІЛЬКІСТЬ УГОД ---
            if len(real_positions) >= MAX_CONCURRENT_TRADES:
                time.sleep(15)
                continue  # Повністю блокуємо пошук та виставлення нових пасток, якщо ліміт вичерпано
                
            # Пошук нових точок входу
            for symbol in SYMBOLS:
                if symbol in real_positions or symbol in active_traps: continue
                
                closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                if not closes_15m or len(volumes_15m) < 26: continue
                
                ema_25 = calculate_ema(closes_15m, 25)
                avg_vol_20 = sum(volumes_15m[-21:-1]) / 20
                vol_ratio = volumes_15m[-2] / avg_vol_20 if avg_vol_20 > 0 else 0
                global_trend, _ = check_global_trend(symbol)
                
                if vol_ratio >= VOLUME_MULTIPLIER:
                    current_price = closes_15m[-1]
                    
                    # Визначаємо індивідуальний профіль та оффсет монети
                    profile = ASSET_PROFILES.get(symbol, DEFAULT_PROFILE)
                    offset_mult = profile[0]
                    
                    base_off = 0.003 if vol_ratio >= 1.8 else 0.004 if vol_ratio >= 1.4 else 0.005
                    off = base_off * offset_mult  # Розрахунок адаптивного оффсету
                    
                    label = "КИТ" if vol_ratio >= 1.8 else "СЕРЕДНІЙ" if vol_ratio >= 1.4 else "ШУМ"
                    
                    if global_trend == "LONG_ONLY" and current_price > ema_25:
                        target_entry = current_price * (1 - off)
                        print(f"🕸️ [SIGNAL LONG] {symbol.split('/')[0]} | Vol: {vol_ratio:.2f}x ({label}) | Адаптивний Оффсет: {off*100:.2f}% -> Target: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', target_entry)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'buy', 'price': prc, 'amount': size, 'vol_ratio': vol_ratio}
                        
                    elif global_trend == "SHORT_ONLY" and current_price < ema_25:
                        target_entry = current_price * (1 + off)
                        print(f"🕸️ [SIGNAL SHORT] {symbol.split('/')[0]} | Vol: {vol_ratio:.2f}x ({label}) | Адаптивний Оффсет: {off*100:.2f}% -> Target: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', target_entry)
                        if oid: active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'sell', 'price': prc, 'amount': size, 'vol_ratio': vol_ratio}
                        
            time.sleep(15)
        except Exception as e:
            print(f"🚨 Помилка головного циклу: {e}")
            time.sleep(10)

if __name__ == "__main__": 
    main_cycle()
