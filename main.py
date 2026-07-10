import time
from datetime import datetime
import ccxt

# --- НАЛАШТУВАННЯ ---
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ONDO/USDT:USDT', 'LINK/USDT:USDT',
    'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'FET/USDT:USDT', 'SOL/USDT:USDT',
    'SUI/USDT:USDT', 'BNB/USDT:USDT', 'XRP/USDT:USDT', 'ADA/USDT:USDT',
    'DOGE/USDT:USDT', 'AVAX/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'TRX/USDT:USDT', 'AAVE/USDT:USDT', 'WLD/USDT:USDT'
]
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

BASE_POSITION_VOLUME = 5.5  # Об'єм входу в USDT
LEVERAGE = 10
VOLUME_MULTIPLIER = 1.1     # Знижуємо планку до 1.1, бо тепер слабкі об'єми фільтруються глибшим відступом

# --- ОПТИМІЗАЦІЯ РИЗИКІВ ---
TP_PERCENT = 0.008          # +0.8% Take Profit
SL_PERCENT = 0.006          # -0.6% Stop Loss
TIMEOUT_SECONDS = 3600      # 1 година життя лімітки у засідці
BREAKEVEN_TRIGGER_PCT = 0.7  

exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

active_traps = {}
last_heartbeat_hour = -1

# --- МАТЕМАТИКА ТА ДОПОМІЖНІ ФУНКЦІЇ ---
def safe_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def calculate_ema(prices, period):
    if len(prices) < period: return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_ohlcv_data(symbol, timeframe, limit=100):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [b[4] for b in bars], [b[5] for b in bars]  # closes, volumes
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
            if symbol and abs(p_size) > 0.000001:
                real_positions[symbol] = pos
    except Exception as e:
        print(f"⚠️ Помилка реальних позицій: {e}")
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

# --- ІНТЕЛЕКТУАЛЬНИЙ ДВІРНИК ---
def clean_orphan_orders(symbol):
    """Видаляє лімітки, ТІЛЬКИ якщо ціна повністю зламала тренд EMA-12"""
    try:
        set_exchange_context()
        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders:
            for order in open_orders:
                if not order.get('stopPrice'):
                    closes_15m, _ = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    if closes_15m and len(closes_15m) >= 21:
                        ema_12 = calculate_ema(closes_15m, 12)
                        global_trend, _ = check_global_trend(symbol)

                        reason = ""
                        if global_trend == "LONG_ONLY" and closes_15m[-1] <= ema_12:
                            reason = f"Ціна випала нижче EMA-12 ({closes_15m[-1]:.4f})"
                        elif global_trend == "SHORT_ONLY" and closes_15m[-1] >= ema_12:
                            reason = f"Ціна піднялась вище EMA-12 ({closes_15m[-1]:.4f})"

                        if reason:
                            print(f"🧹 [🧹 CLEAN] {symbol} (ID: {order['id']}) -> Скасовано. Причина: {reason}")
                            exchange.cancel_order(order['id'], symbol)
                            if symbol in active_traps: del active_traps[symbol]

                elif order.get('stopPrice'):
                    real_positions = get_active_positions()
                    if symbol not in real_positions:
                        print(f"🧹 [🧹 CLEAN] {symbol} -> Зачищено залишений стоп-ордер (ID: {order['id']})")
                        exchange.cancel_order(order['id'], symbol)
    except Exception:
        pass

# --- АВТО-ЗАХИСТ ТА БЕЗЗБИТОК ---
def control_and_protect_positions(real_positions):
    for symbol, pos in real_positions.items():
        try:
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            is_long = p_size > 0
            entry_price = safe_float(pos.get('entryPrice') or pos.get('info', {}).get('entryPrice'))
            unrealized_pnl = safe_float(pos.get('unrealizedPnl') or pos.get('info', {}).get('pnl'))

            open_orders = exchange.fetch_open_orders(symbol)
            has_sl, has_tp, has_breakeven_sl = False, False, False
            old_sl_id = None

            for order in open_orders:
                o_price = safe_float(order.get('stopPrice') or order.get('info', {}).get('stopPrice'))
                if o_price > 0:
                    if abs(o_price - entry_price) / entry_price < 0.001:
                        has_breakeven_sl, has_sl = True, True
                    elif (is_long and o_price < entry_price) or (not is_long and o_price > entry_price):
                        has_sl = True
                        old_sl_id = order['id']
                    else:
                        has_tp = True

            if not has_sl or not has_tp:
                sl_side = 'sell' if is_long else 'buy'
                sl_price = entry_price * (1 - SL_PERCENT) if is_long else entry_price * (1 + SL_PERCENT)
                tp_price = entry_price * (1 + TP_PERCENT) if is_long else entry_price * (1 - TP_PERCENT)

                precise_sl = float(exchange.price_to_precision(symbol, sl_price))
                precise_tp = float(exchange.price_to_precision(symbol, tp_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))

                if not has_sl:
                    exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_sl})
                    print(f"🛑 [PROTECT] {symbol} | Первинний SL виставлено: {precise_sl}")
                if not has_tp:
                    exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_tp})
                    print(f"🟢 [PROTECT] {symbol} | Первинний TP виставлено: {precise_tp}")

                if symbol in active_traps: del active_traps[symbol]
                continue

            target_pnl_to_activate = BASE_POSITION_VOLUME * LEVERAGE * TP_PERCENT * BREAKEVEN_TRIGGER_PCT
            if unrealized_pnl >= target_pnl_to_activate and not has_breakeven_sl:
                if old_sl_id:
                    try: exchange.cancel_order(old_sl_id, symbol)
                    except: pass
                precise_entry = float(exchange.price_to_precision(symbol, entry_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))
                sl_side = 'sell' if is_long else 'buy'
                exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_entry})
                print(f"🔥 [BREAKEVEN] {symbol} | Профіт >= 70% від цілі. SL перенесено в 0: {precise_entry}")

        except Exception as e:
            print(f"❌ Помилка модуля захисту {symbol}: {e}")

# --- ВИСТАВЛЕННЯ ОРДЕРІВ ---
def place_trap_order(symbol, side, price):
    try:
        amount_usdt = BASE_POSITION_VOLUME * LEVERAGE
        amount_contracts = amount_usdt / price
        market_info = exchange.market(symbol)
        min_qty = safe_float(market_info['limits']['amount']['min'], 0.001)
        if amount_contracts < min_qty: amount_contracts = min_qty

        precise_amount = float(exchange.amount_to_precision(symbol, amount_contracts))
        precise_price = float(exchange.price_to_precision(symbol, price))

        set_exchange_context()
        order = exchange.create_order(symbol, 'limit', side, precise_amount, precise_price)
        return order['id'], precise_amount, precise_price
    except Exception as e:
        print(f"❌ Помилка створення лімітки {symbol}: {e}")
        return None, 0, 0

def handle_traps_and_timeouts(symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)

        if order['status'] in ['closed', 'filled']:
            print(f"🕸️ [TRAP FILLED] {symbol} | Пастка спрацювала! Передано авто-захисту.")
            del active_traps[symbol]
        elif order['status'] == 'canceled':
            del active_traps[symbol]
        elif time.time() - trap['placed_time'] >= TIMEOUT_SECONDS:
            print(f"⏰ [TIMEOUT] {symbol} | Лімітка висіла 1 годину без відкату. Видаляємо.")
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
                    active_traps[symbol] = {
                        'order_id': str(order['id']), 'placed_time': time.time(),
                        'side': order['side'].lower(), 'price': safe_float(order.get('price')),
                        'amount': safe_float(order.get('amount'))
                    }
                    print(f"🔗 [SYNC] Взято під контроль відкритий ордер {order['side'].upper()} по {symbol}")
        except Exception: pass

# --- ГОЛОВНИЙ ЦИКЛ ---
def main_cycle():
    global last_heartbeat_hour
    print(f"🤖 Lyra V3 [DYNAMIC OFFSET] запущена успішно.")
    exchange.load_markets()
    sync_existing_traps_on_startup()

    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()

            if real_positions:
                control_and_protect_positions(real_positions)

            # --- СУХИЙ ЩОГОДИННИЙ ЛОГ ---
            if current_time.hour != last_heartbeat_hour:
                print(f"\n📊 [{current_time.strftime('%H:%M')}] === СТАН ПАНЕЛІ КЕРУВАННЯ ===")
                for symbol in SYMBOLS:
                    trend, last_p = check_global_trend(symbol)
                    closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    vol_str, dev_str = "0.00x", "0.00%"

                    if closes_15m and len(volumes_15m) >= 21:
                        ema_12 = calculate_ema(closes_15m, 12)
                        avg_vol = sum(volumes_15m[-21:-1]) / 20
                        vol_ratio = volumes_15m[-2] / avg_vol if avg_vol > 0 else 0
                        vol_str = f"{vol_ratio:.2f}x"
                        dev_str = f"{(((closes_15m[-1] - ema_12)/ema_12)*100):+.2f}%" if ema_12 > 0 else "0.00%"

                    status = "Вільна"
                    if symbol in real_positions:
                        p = real_positions[symbol]
                        pnl = safe_float(p.get('unrealizedPnl') or p.get('info', {}).get('pnl'))
                        tp_val, sl_val = get_position_protection_levels(symbol)
                        status = f"ПОЗИЦІЯ | PnL: {pnl:.2f} USDT | Захист (TP: {tp_val} / SL: {sl_val})"
                    elif symbol in active_traps:
                        rem = max(0.0, (TIMEOUT_SECONDS - (time.time() - active_traps[symbol]['placed_time'])) / 60)
                        status = f"ПАСТКА ({active_traps[symbol]['side'].upper()}) по {active_traps[symbol]['price']} | Таймаут: {rem:.1f} хв"

                    print(f"  • {symbol.split('/')[0]:<7} | Цiна: {last_p:<9} | Trend: {trend:<10} | Vol: {vol_str:<6} | EMA-12: {dev_str:<7} | {status}")
                print("=========================================\n")
                last_heartbeat_hour = current_time.hour

            # --- МЕХАНІКА АНАЛІЗУ ТА ТОРГІВЛІ ---
            for symbol in SYMBOLS:
                clean_orphan_orders(symbol)
                if symbol in real_positions: continue
                handle_traps_and_timeouts(symbol)
                if symbol in active_traps: continue

                closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                if not closes_15m or len(volumes_15m) < 21: continue

                ema_12 = calculate_ema(closes_15m, 12)
                avg_vol_20 = sum(volumes_15m[-21:-1]) / 20  
                vol_ratio = volumes_15m[-2] / avg_vol_20 if avg_vol_20 > 0 else 0
                global_trend, _ = check_global_trend(symbol)

                if vol_ratio >= VOLUME_MULTIPLIER:
                    current_price = closes_15m[-1]

                    # 🧮 РОЗРАХУНОК ДИНАМІЧНОГО ВІДСТУПУ НА ОСНОВІ ДАНИХ КОЛАБУ
                    if vol_ratio >= 1.8:
                        dynamic_offset = 0.003   # 0.3% (Сильний кит -> короткий відкат)
                        label = "КИТ"
                    elif vol_ratio >= 1.4:
                        dynamic_offset = 0.004   # 0.4% (Середній імпульс -> стандартний відкат)
                        label = "СЕРЕДНІЙ"
                    else:
                        dynamic_offset = 0.005   # 0.5% (Слабкий сплесок -> глибокий захисний відступ)
                        label = "ШУМ"

                    # Виставлення LONG пастки від ціни закриття
                    if global_trend == "LONG_ONLY" and current_price > ema_12:
                        target_entry = current_price * (1 - dynamic_offset)
                        print(f"🕸️ [SIGNAL LONG] {symbol.split('/')[0]} | Vol: {vol_ratio:.2f}x ({label}) | Dynamic Offset: {dynamic_offset*100}% | Close: {current_price} -> Target: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', target_entry)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'buy', 'price': prc, 'amount': size}

                    # Виставлення SHORT пастки від ціни закриття
                    elif global_trend == "SHORT_ONLY" and current_price < ema_12:
                        target_entry = current_price * (1 + dynamic_offset)
                        print(f"🕸️ [SIGNAL SHORT] {symbol.split('/')[0]} | Vol: {vol_ratio:.2f}x ({label}) | Dynamic Offset: {dynamic_offset*100}% | Close: {current_price} -> Target: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', target_entry)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'sell', 'price': prc, 'amount': size}

            time.sleep(15)
        except Exception as e:
            print(f"🚨 Помилка головного циклу: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()
