import time
from datetime import datetime
import ccxt

# --- НАЛАШТУВАННЯ ---
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ONDO/USDT:USDT', 'LINK/USDT:USDT',
    'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'FET/USDT:USDT', 'SOL/USDT:USDT',
    'SUI/USDT:USDT', 'BNB/USDT:USDT', 'XRP/USDT:USDT', 'ADA/USDT:USDT',
    'DOGE/USDT:USDT', 'AVAX/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'TRX/USDT:USDT', 'AAVE/USDT:USDT'
]
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

BASE_POSITION_VOLUME = 10  # Об'єм входу в USDT
LEVERAGE = 10
VOLUME_MULTIPLIER = 1.1     # Знижена планка, фільтрація йде за рахунок глибини відступу

# --- ОПТИМІЗАЦІЯ РИЗИКІВ ---
TP_PERCENT = 0.008          # +0.8% Take Profit
SL_PERCENT = 0.006          # -0.6% Stop Loss
TIMEOUT_SECONDS = 3600      # 1 година життя лімітки в засаді
BREAKEVEN_TRIGGER_PCT = 0.7  

exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# Локальна база даних бота для моніторингу та аудиту
active_traps = {}
position_history_cache = {}
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
        print(f"⚠️ Помилка отримання реальних позицій: {e}")
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

# --- МОДУЛЬ АВТОМАТИЧНОГО АУДИТУ УГОД ---
def execute_trade_audit(symbol, last_known_entry_price, vol_coeff, open_time):
    """Викликається Двірником відразу після закриття позиції для виведення сухого аналізу ризиків."""
    try:
        set_exchange_context()
        # Шукаємо останню закриту угоду в історії біржі по цій монеті
        trades = exchange.fetch_my_trades(symbol, limit=5)
        if not trades:
            return

        # Фільтруємо саме закриваючий ордер (той, що відбувся щойно)
        trades = sorted(trades, key=lambda x: x['timestamp'], reverse=True)
        exit_trade = trades[0]
        
        exit_price = safe_float(exit_trade.get('price'))
        entry_price = last_known_entry_price if last_known_entry_price > 0 else safe_float(exit_trade.get('fee', {}).get('rate', 0)) 
        
        if exit_price == 0 or entry_price == 0:
            return

        # Рахуємо чистий PnL руху ціни
        is_long = exit_trade.get('side') == 'sell'  # Якщо закрили продажем, то це був лонг
        if is_long:
            price_change_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            price_change_pct = ((entry_price - exit_price) / entry_price) * 100

        pnl_usd = (price_change_pct / 100) * BASE_POSITION_VOLUME * LEVERAGE
        pnl_capital_pct = price_change_pct * LEVERAGE

        # Розрахунок часу утримання позиції
        duration_str = "Невідомо"
        if open_time:
            duration = datetime.now() - open_time
            duration_str = str(duration).split('.')[0]

        # Аналіз характеру закриття та сліпейджу
        if pnl_usd >= 0:
            reason = "TAKE_PROFIT (Ціль досягнута успішно)"
        else:
            reason = "STOP_LOSS"
            actual_loss_move = abs(price_change_pct) / 100
            slippage = actual_loss_move - SL_PERCENT
            if slippage > 0.0005:
                reason += f" (Ринковий пролив / Сліпейдж: +{slippage * 100:.3f}%)"
            else:
                reason += " (Технічний стоп без просковзування)"

        # Вивід структурованого блоку аудиту
        side_str = "LONG" if is_long else "SHORT"
        audit_log = (
            f"\n==================================================================\n"
            f"📊 [АУДИТ УГОДИ] #{symbol.split('/')[0]}-{side_str}\n"
            f"    • Вхід: {entry_price:.4f} | Вихід: {exit_price:.4f} | Результат: {pnl_usd:+.2f} USDT ({pnl_capital_pct:+.2f}%)\n"
            f"    • Тривалість угоди: {duration_str}\n"
            f"    • Причина закриття: {reason}\n"
            f"    • Параметри входу: Сплєск об'єму був {vol_coeff:.2f}x від середнього\n"
            f"=================================================================="
        )
        print(audit_log)

    except Exception as e:
        print(f"⚠️ Не вдалося згенерувати аудит угоди для {symbol}: {e}")

# --- РОЗУМНИЙ ДВІРНИК ---
def clean_orphan_orders(symbol, real_positions):
    """Зачищає лімітки за трендом EMA-12 та проводить аудит, якщо позиція закрилася."""
    try:
        set_exchange_context()
        
        # Перевірка на закриття позиції, яка була у нас на контролі
        if symbol in position_history_cache and symbol not in real_positions:
            cached = position_history_cache[symbol]
            # Запускаємо аудит угоди
            execute_trade_audit(symbol, cached['entry_price'], cached['vol_ratio'], cached['open_time'])
            # Видаляємо з кешу
            del position_history_cache[symbol]

        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders:
            for order in open_orders:
                # Якщо це лімітна пастка, а не стоп-ордер захисту
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
                            print(f"🧹 [CLEAN] {symbol.split('/')[0]} (ID: {order['id']}) -> Пастку знято. Причина: {reason}")
                            exchange.cancel_order(order['id'], symbol)
                            if symbol in active_traps: del active_traps[symbol]

                # Якщо залишився сиротою стоп-ордер захисту без реальної позиції
                elif order.get('stopPrice'):
                    if symbol not in real_positions:
                        print(f"🧹 [CLEAN] {symbol.split('/')[0]} -> Зачищено залишений стоп-ордер захисту (ID: {order['id']})")
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

            # Логуємо факт відкриття позиції в наш кеш для майбутнього аудиту
            if symbol not in position_history_cache:
                # Спробуємо підтягнути коефіцієнт об'єму з активної пастки, якщо вона була
                vol_ratio = active_traps.get(symbol, {}).get('vol_ratio', 1.4)
                position_history_cache[symbol] = {
                    'entry_price': entry_price,
                    'vol_ratio': vol_ratio,
                    'open_time': datetime.now()
                }

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

            # Виставлення первинної броні
            if not has_sl or not has_tp:
                sl_side = 'sell' if is_long else 'buy'
                sl_price = entry_price * (1 - SL_PERCENT) if is_long else entry_price * (1 + SL_PERCENT)
                tp_price = entry_price * (1 + TP_PERCENT) if is_long else entry_price * (1 - TP_PERCENT)

                precise_sl = float(exchange.price_to_precision(symbol, sl_price))
                precise_tp = float(exchange.price_to_precision(symbol, tp_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))

                if not has_sl:
                    exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_sl})
                    print(f"🛑 [PROTECT] {symbol.split('/')[0]} | Первинний SL виставлено: {precise_sl}")
                if not has_tp:
                    exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_tp})
                    print(f"🟢 [PROTECT] {symbol.split('/')[0]} | Первинний TP виставлено: {precise_tp}")

                if symbol in active_traps: del active_traps[symbol]
                continue

            # Логіка переносу в безубиток (Breakeven)
            target_pnl_to_activate = BASE_POSITION_VOLUME * LEVERAGE * TP_PERCENT * BREAKEVEN_TRIGGER_PCT
            if unrealized_pnl >= target_pnl_to_activate and not has_breakeven_sl:
                if old_sl_id:
                    try: exchange.cancel_order(old_sl_id, symbol)
                    except: pass
                precise_entry = float(exchange.price_to_precision(symbol, entry_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))
                sl_side = 'sell' if is_long else 'buy'
                exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_entry})
                print(f"🔥 [BREAKEVEN] {symbol.split('/')[0]} | Профіт >= 70% від цілі. Стоп перенесено в нуль: {precise_entry}")

        except Exception as e:
            print(f"❌ Помилка модуля захисту {symbol.split('/')[0]}: {e}")

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
        print(f"❌ Помилка створення лімітки {symbol.split('/')[0]}: {e}")
        return None, 0, 0

def handle_traps_and_timeouts(symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)

        if order['status'] in ['closed', 'filled']:
            print(f"🕸️ [TRAP FILLED] {symbol.split('/')[0]} | Пастка спрацювала! Позицію відкрито.")
            del active_traps[symbol]
        elif order['status'] == 'canceled':
            del active_traps[symbol]
        elif time.time() - trap['placed_time'] >= TIMEOUT_SECONDS:
            print(f"⏰ [TIMEOUT] {symbol.split('/')[0]} | Лімітка висіла 1 годину без відкату. Видаляємо.")
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
                        'amount': safe_float(order.get('amount')), 'vol_ratio': 1.4
                    }
                    print(f"🔗 [SYNC] Взято під контроль відкритий ордер {order['side'].upper()} по {symbol.split('/')[0]}")
        except Exception: pass

# --- ГОЛОВНИЙ ЦИКЛ ---
def main_cycle():
    global last_heartbeat_hour
    print(f"🤖 Lyra V3.1 [DYNAMIC + AUDIT MODULE] запущена успішно.")
    exchange.load_markets()
    sync_existing_traps_on_startup()

    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()

            if real_positions:
                control_and_protect_positions(real_positions)

            # --- СУХИЙ ДАШБОРД (ЩОГОДИННИЙ) ---
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

            # --- ТОРГОВЕЛЬНИЙ АЛГОРИТМ ---
            for symbol in SYMBOLS:
                clean_orphan_orders(symbol, real_positions)
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

                    # РОЗРАХУНОК ДИНАМІЧНОГО ВІДСТУПУ НА ОСНОВІ СТАТИСТИКИ З КОЛАБУ
                    if vol_ratio >= 1.8:
                        dynamic_offset = 0.003   # 0.3% (Сильний кит -> беремо раніше)
                        label = "КИТ"
                    elif vol_ratio >= 1.4:
                        dynamic_offset = 0.004   # 0.4% (Середня зона)
                        label = "СЕРЕДНІЙ"
                    else:
                        dynamic_offset = 0.005   # 0.5% (Слабкий об'єм -> глибокий захисний відступ)
                        label = "ШУМ"

                    # Пастка для LONG
                    if global_trend == "LONG_ONLY" and current_price > ema_12:
                        target_entry = current_price * (1 - dynamic_offset)
                        print(f"🕸️ [SIGNAL LONG] {symbol.split('/')[0]} | Vol: {vol_ratio:.2f}x ({label}) | Offset: {dynamic_offset*100}% | Close: {current_price} -> Target: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', target_entry)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'buy', 'price': prc, 'amount': size, 'vol_ratio': vol_ratio}

                    # Пастка для SHORT
                    elif global_trend == "SHORT_ONLY" and current_price < ema_12:
                        target_entry = current_price * (1 + dynamic_offset)
                        print(f"🕸️ [SIGNAL SHORT] {symbol.split('/')[0]} | Vol: {vol_ratio:.2f}x ({label}) | Offset: {dynamic_offset*100}% | Close: {current_price} -> Target: {target_entry:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', target_entry)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'sell', 'price': prc, 'amount': size, 'vol_ratio': vol_ratio}

            time.sleep(15)
        except Exception as e:
            print(f"🚨 Помилка головного циклу: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()