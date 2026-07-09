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
VOLUME_MULTIPLIER = 1.4     # Аномальний об'єм (> ніж середній * 1.2)
TP_PERCENT = 0.006          # +1.6% (при 10х = +16% до маржі)
SL_PERCENT = 0.015          # -0.9% (при 10х = -9% до маржі)
TIMEOUT_SECONDS = 3600      # 1 година у секундах для таймауту ліміток

# Коефіцієнт активації безубитку (0.44 = ~44% від цілі Take Profit, тобто ~0.7% руху ціни)
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

# --- РОБОТА З БІРЖЕЮ ТА МОНІТОРИНГ ---
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
        print(f"⚠️ Помилка запиту реальних позицій: {e}")
    return real_positions

def get_position_protection_levels(symbol):
    """Пошук виставлених TP/SL ордерів на біржі для відображення в логах"""
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

def clean_orphan_orders(symbol):
    """ІНТЕЛЕКТУАЛЬНИЙ ДВІРНИК: перевіряє доцільність утримання лімітки та зачищає хвости"""
    try:
        set_exchange_context()
        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders:
            for order in open_orders:
                # 1. Робота зі звичайними лімітками на вхід (у них немає stopPrice)
                if not order.get('stopPrice'):
                    
                    # Витягуємо свіжу математику ринку, щоб перевірити актуальність сигналу
                    closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    if closes_15m and len(volumes_15m) >= 21:
                        ema_12 = calculate_ema(closes_15m, 12)
                        avg_vol_20 = sum(volumes_15m[-21:-1]) / 20
                        current_vol = volumes_15m[-2]
                        global_trend, _ = check_global_trend(symbol)
                        
                        vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 0
                        
                        # Шукаємо причину, чому лімітку треба прибрати
                        reason = ""
                        if vol_ratio < VOLUME_MULTIPLIER:
                            reason = f"аномальний об'єм зник, поточний: {vol_ratio:.2f}x (треба > {VOLUME_MULTIPLIER}x)"
                        elif global_trend == "LONG_ONLY" and closes_15m[-1] <= ema_12:
                            reason = f"ціна випала з лонгового тренду нижче EMA-12 ({closes_15m[-1]:.4f})"
                        elif global_trend == "SHORT_ONLY" and closes_15m[-1] >= ema_12:
                            reason = f"ціна випала з шортового тренду вище EMA-12 ({closes_15m[-1]:.4f})"
                        
                        # Якщо причина знайдена — скасовуємо лімітку з поясненням у лог
                        if reason:
                            print(f"🧹 [ДВІРНИК] Скасування лімітки по {symbol} (ID: {order['id']}):")
                            print(f"   ℹ️ Причина: {reason}.")
                            exchange.cancel_order(order['id'], symbol)
                            print(f"   ✅ Лімітку успішно прибрано з ринку.")
                            if symbol in active_traps: 
                                del active_traps[symbol]
                
                # 2. Якщо це стоп-ордер (є stopPrice), але позиції по монеті вже НЕМАЄ
                elif order.get('stopPrice'):
                    real_positions = get_active_positions()
                    if symbol not in real_positions:
                        print(f"🧹 [ДВІРНИК] Знайдено залишений стоп-ордер (ID: {order['id']}) по {symbol} без активної позиції! Видаляємо...")
                        exchange.cancel_order(order['id'], symbol)
                        print(f"   ✅ Стоп-хвіст успішно зачищено.")
    except Exception:
        pass

# --- АВТО-ЗАХИСТ ТА БЕЗЗБИТОК ПОЗИЦІЙ ---
def control_and_protect_positions(real_positions):
    """Сканує активні позиції, ставить стопи для ліміток, що спрацювали, та переносить у безубиток"""
    for symbol, pos in real_positions.items():
        try:
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            is_long = p_size > 0
            entry_price = safe_float(pos.get('entryPrice') or pos.get('info', {}).get('entryPrice'))
            unrealized_pnl = safe_float(pos.get('unrealizedPnl') or pos.get('info', {}).get('pnl'))

            open_orders = exchange.fetch_open_orders(symbol)
            has_sl = False
            has_tp = False
            has_breakeven_sl = False
            old_sl_id = None

            for order in open_orders:
                o_price = safe_float(order.get('stopPrice') or order.get('info', {}).get('stopPrice'))
                if o_price > 0:
                    if abs(o_price - entry_price) / entry_price < 0.001:
                        has_breakeven_sl = True
                        has_sl = True
                    elif (is_long and o_price < entry_price) or (not is_long and o_price > entry_price):
                        has_sl = True
                        old_sl_id = order['id']
                    else:
                        has_tp = True

            if not has_sl or not has_tp:
                print(f"⚠️ [КОНТРОЛЬ] Виявлено незахищену позицію по {symbol}! Ставимо параметри захисту...")
                sl_side = 'sell' if is_long else 'buy'

                if is_long:
                    sl_price = entry_price * (1 - SL_PERCENT)
                    tp_price = entry_price * (1 + TP_PERCENT)
                else:
                    sl_price = entry_price * (1 + SL_PERCENT)
                    tp_price = entry_price * (1 - TP_PERCENT)

                precise_sl = float(exchange.price_to_precision(symbol, sl_price))
                precise_tp = float(exchange.price_to_precision(symbol, tp_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))

                if not has_sl:
                    exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_sl})
                    print(f"   🛑 Первинний Stop Loss (-{SL_PERCENT*100}%) виставлено: {precise_sl}")
                if not has_tp:
                    exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_tp})
                    print(f"   🟢 Первинний Take Profit (+{TP_PERCENT*100}%) виставлено: {precise_tp}")

                if symbol in active_traps: del active_traps[symbol]
                continue

            target_pnl_to_activate = BASE_POSITION_VOLUME * LEVERAGE * TP_PERCENT * BREAKEVEN_TRIGGER_PCT
            if unrealized_pnl >= target_pnl_to_activate:
                if has_breakeven_sl:
                    continue  

                print(f"🚀 [БЕЗЗБИТОК] Позиція {symbol} дала гарний профіт ({unrealized_pnl:.2f} USDT). Переносимо SL у нуль...")

                if old_sl_id:
                    try:
                        exchange.cancel_order(old_sl_id, symbol)
                        print(f"   ✅ Старий збитковий Stop Loss (ID: {old_sl_id}) скасовано.")
                        time.sleep(0.2)
                    except: pass

                precise_entry = float(exchange.price_to_precision(symbol, entry_price))
                precise_amount = float(exchange.amount_to_precision(symbol, abs(p_size)))
                sl_side = 'sell' if is_long else 'buy'

                exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_entry})
                print(f"   🔥 Новий безубитковий Stop Loss виставлено на рівень входу: {precise_entry}")

        except Exception as e:
            print(f"❌ Помилка в модулі захисту/безубитку для {symbol}: {e}")

# --- УНІВЕРСАЛЬНЕ СТВОРЕННЯ ПАСТКИ ---
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
        print(f"✅ Лімітний ордер {side.upper()} виставлено! ID: {order['id']}")
        return order['id'], precise_amount, precise_price
    except Exception as e:
        print(f"❌ Помилка створення лімітки по {symbol}: {e}")
        return None, 0, 0

def handle_traps_and_timeouts(symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)

        if order['status'] in ['closed', 'filled']:
            print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Лімітка виконана по {symbol}! Передаємо під контроль авто-захисту.")
            del active_traps[symbol]

        elif order['status'] == 'canceled':
            del active_traps[symbol]

        elif time.time() - trap['placed_time'] >= TIMEOUT_SECONDS:
            elapsed_mins = (time.time() - trap['placed_time']) / 60
            print(f"⏰ [ТАЙМАУТ] Лімітка по {symbol} висить вже {elapsed_mins:.1f} хв. Видаляємо...")
            try: exchange.cancel_order(trap['order_id'], symbol)
            except: pass
            del active_traps[symbol]

    except Exception as e:
        if "NOT_FOUND" in str(e).upper():
            if symbol in active_traps: del active_traps[symbol]

def sync_existing_traps_on_startup():
    global active_traps
    print("🔄 Сканування біржі на наявність відкритих ліміток...")
    set_exchange_context()
    real_positions = get_active_positions()

    for symbol in SYMBOLS:
        if symbol in real_positions: continue
        try:
            open_orders = exchange.fetch_open_orders(symbol)
            for order in open_orders:
                if order.get('status') == 'open' and not order.get('stopPrice'):
                    active_traps[symbol] = {
                        'order_id': str(order['id']),
                        'placed_time': time.time(),
                        'side': order['side'].lower(),
                        'price': safe_float(order.get('price')),
                        'amount': safe_float(order.get('amount'))
                    }
                    print(f"🔗 Взято під контроль лімітку {order['side'].upper()} по {symbol} (ID: {order['id']})")
        except Exception:
            pass
    print(f"✅ Синхронізація завершена. Активних пасток: {len(active_traps)}")

# --- ГОЛОВНИЙ ЦИКЛ ---
def main_cycle():
    global last_heartbeat_hour
    print(f"🤖 Бот Lyra V2 [Оптимізований під WhiteBIT + Авто-Стопи] запущений.")
    exchange.load_markets()
    sync_existing_traps_on_startup()

    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()

            if real_positions:
                control_and_protect_positions(real_positions)

            # --- ЩОГОДИННИЙ ІНФОРМАТИВНИЙ ЗВІТ ---
            if current_time.hour != last_heartbeat_hour:
                print(f"\n⚡ [{current_time.strftime('%H:%M:%S')}] ========== МАКСИМАЛЬНИЙ ЗВІТ СИСТЕМИ ==========")
                try:
                    balance = exchange.fetch_balance({'type': 'main'})
                    print(f"💰 Доступний баланс (Main): {safe_float(balance.get('USDT', {}).get('free')):.2f} USDT")
                except: pass

                print("\n📊 СТАН РИНКУ, ПОЗИЦІЙ ТА ЛІМІТОК:")
                for symbol in SYMBOLS:
                    trend, last_p = check_global_trend(symbol)
                    
                    closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    vol_ratio_str = "0.00x"
                    dev_str = "0.00%"
                    
                    if closes_15m and len(volumes_15m) >= 21:
                        ema_12 = calculate_ema(closes_15m, 12)
                        avg_vol_20 = sum(volumes_15m[-21:-1]) / 20
                        vol_ratio = volumes_15m[-2] / avg_vol_20 if avg_vol_20 > 0 else 0
                        vol_ratio_str = f"{vol_ratio:.2f}x"
                        
                        if ema_12 > 0:
                            dev_pct = ((closes_15m[-1] - ema_12) / ema_12) * 100
                            dev_str = f"{dev_pct:+.2f}%"

                    pos_status = "Вільна"
                    if symbol in real_positions:
                        p = real_positions[symbol]
                        p_size = safe_float(p.get('contracts') or p.get('info', {}).get('amount'))
                        pnl = safe_float(p.get('unrealizedPnl') or p.get('info', {}).get('pnl'))

                        tp_val, sl_val = get_position_protection_levels(symbol)
                        pos_status = f"Є ПОЗИЦІЯ ({'LONG' if p_size > 0 else 'SHORT'}) | PnL: {pnl:.2f} USDT | Захист (TP: {tp_val} / SL: {sl_val})"

                    elif symbol in active_traps:
                        rem_time = TIMEOUT_SECONDS - (time.time() - active_traps[symbol]['placed_time'])
                        rem_min = max(0.0, rem_time / 60)
                        pos_status = f"ЧЕКАЄ ЛІМІТКА ({active_traps[symbol]['side'].upper()}) по {active_traps[symbol]['price']} (Таймаут через: {rem_min:.1f} хв)"

                    print(f"  • {symbol:<15} | Тренд: {trend:<10} | Об'єм: {vol_ratio_str:<6} | До EMA-12: {dev_str:<7} | {pos_status}")
                
                print("==================================================================\n")
                last_heartbeat_hour = current_time.hour

            # --- АНАЛІЗ ТА ТОРГІВЛЯ ---
            for symbol in SYMBOLS:
                # Двірник тепер розумний: аналізує об'єми та тренд перед видаленням лімітки на вхід
                clean_orphan_orders(symbol)

                if symbol in real_positions: 
                    continue  

                handle_traps_and_timeouts(symbol)
                if symbol in active_traps: continue

                closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                if not closes_15m or len(volumes_15m) < 21: continue

                ema_12 = calculate_ema(closes_15m, 12)
                avg_vol_20 = sum(volumes_15m[-21:-1]) / 20  
                global_trend, _ = check_global_trend(symbol)

                if volumes_15m[-2] > (avg_vol_20 * VOLUME_MULTIPLIER):
                    # LONG пастка
                    if global_trend == "LONG_ONLY" and closes_15m[-1] > ema_12:
                        print(f"🕸️ [СИГНАЛ LONG] {symbol}. Об'єм аномальний. Ставимо лімітку на EMA-12: {ema_12:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', ema_12)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'buy', 'price': prc, 'amount': size}

                    # SHORT пастка
                    elif global_trend == "SHORT_ONLY" and closes_15m[-1] < ema_12:
                        print(f"🕸️ [СИГНАЛ SHORT] {symbol}. Об'єм аномальний. Ставимо лімітку на EMA-12: {ema_12:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', ema_12)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_time': time.time(), 'side': 'sell', 'price': prc, 'amount': size}

            time.sleep(15)  

        except Exception as e:
            print(f"🚨 Помилка в головному циклі: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()
