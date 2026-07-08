import time
from datetime import datetime
import ccxt

# --- НАЛАШТУВАННЯ ---
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ONDO/USDT:USDT', 'LINK/USDT:USDT', 
    'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'FET/USDT:USDT', 'SOL/USDT:USDT', 'SUI/USDT:USDT'
]
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

BASE_POSITION_VOLUME = 5.5  # Об'єм входу в USDT
LEVERAGE = 10
VOLUME_MULTIPLIER = 1.6     # Аномальний об'єм (> ніж середній * 1.6)
TP_PERCENT = 0.025          # +2.5% (при 10х = +25% до маржі)
SL_PERCENT = 0.012          # -1.2% (при 10х = -12% до маржі)

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
    """ КРОК 4: Моніторить і повертає всі реальні позиції на біржі """
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

def clean_orphan_orders(symbol):
    """ КРОК 5: Видаляє все зайве (ордери-сироти) по парі, якщо позиції немає """
    try:
        set_exchange_context()
        open_orders = exchange.fetch_open_orders(symbol)
        if open_orders:
            print(f"🧹 [ДВІРНИК] Очищення залишків по {symbol}...")
            for order in open_orders:
                exchange.cancel_order(order['id'], symbol)
                print(f"   ✅ Ордер ID {order['id']} скасовано.")
    except Exception:
        pass

# --- КРОК 3: ЗАХИСТ СТОПАМИ (МАРКЕТ-СТОПИ З TRIGGERPRICE) ---
def set_tp_sl_protection(symbol, side, filled_price, amount_contracts):
    try:
        sl_side = 'sell' if side == 'buy' else 'buy'
        
        if side == 'buy':
            sl_price = filled_price * (1 - SL_PERCENT)
            tp_price = filled_price * (1 + TP_PERCENT)
        else:
            sl_price = filled_price * (1 + SL_PERCENT)
            tp_price = filled_price * (1 - TP_PERCENT)

        precise_sl = float(exchange.price_to_precision(symbol, sl_price))
        precise_tp = float(exchange.price_to_precision(symbol, tp_price))
        precise_amount = float(exchange.amount_to_precision(symbol, amount_contracts))

        print(f"🛡️ Виставляємо TP/SL захист для {symbol} ({side.upper()})")
        
        # Stop Loss
        exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_sl})
        print(f"   🛑 Stop Loss: {precise_sl}")
        
        # Take Profit
        exchange.create_order(symbol, 'market', sl_side, precise_amount, params={'stopPrice': precise_tp})
        print(f"   🟢 Take Profit: {precise_tp}")
        
    except Exception as e:
        print(f"❌ Помилка встановлення захисту для {symbol}: {e}")

# --- КРОК 1 & 5: СИНХРОНІЗАЦІЯ ПАСТОК ТА МОНІТОР СТАНУ ---
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
                # Шукаємо саме вхідні лімітки (у них зазвичай немає stopPrice в info)
                if order.get('status') == 'open' and not order.get('stopPrice'):
                    closes, _ = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                    active_traps[symbol] = {
                        'order_id': str(order['id']),
                        'placed_idx': len(closes) if closes else 100,
                        'side': order['side'].lower(),
                        'price': safe_float(order.get('price')),
                        'amount': safe_float(order.get('amount'))
                    }
                    print(f"🔗 Взято під контроль лімітку {order['side'].upper()} по {symbol} (ID: {order['id']})")
        except Exception:
            pass
    print(f"✅ Синхронізація завершена. Активних пасток: {len(active_traps)}")

def handle_traps_and_timeouts(symbol, current_idx):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        set_exchange_context()
        order = exchange.fetch_order(trap['order_id'], symbol)

        if order['status'] == 'closed':
            print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Лімітка виконана по {symbol}!")
            filled_price = safe_float(order.get('average') or order.get('price') or trap['price'])
            amount = safe_float(order.get('amount', trap['amount']))
            
            set_tp_sl_protection(symbol, trap['side'], filled_price, amount)
            del active_traps[symbol]
            
        elif order['status'] == 'canceled':
            del active_traps[symbol]
            
        elif current_idx - trap['placed_idx'] >= 2:  # 2 свічки по 15м = 30 хвилин таймаут
            print(f"⏰ [ТАЙМАУТ] Видаляємо застарілу лімітку по {symbol}...")
            try: exchange.cancel_order(trap['order_id'], symbol)
            except: pass
            del active_traps[symbol]
            
    except Exception as e:
        if "NOT_FOUND" in str(e).upper():
            if symbol in active_traps: del active_traps[symbol]

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

# --- ГОЛОВНИЙ ЦИКЛ ---
def main_cycle():
    global last_heartbeat_hour
    print(f"🤖 Бот Lyra V2 [Оптимізований під WhiteBIT] запущений.")
    exchange.load_markets()
    sync_existing_traps_on_startup()

    while True:
        try:
            current_time = datetime.now()
            real_positions = get_active_positions()

            # --- КРОК 4: ЩОГОДИННИЙ ЗВІТ СИСТЕМИ ---
            if current_time.hour != last_heartbeat_hour:
                print(f"\n⚡ [{current_time.strftime('%H:%M:%S')}] ========== МАКСИМАЛЬНИЙ ЗВІТ СИСТЕМИ ==========")
                try:
                    balance = exchange.fetch_balance({'type': 'main'})
                    print(f"💰 Доступний баланс (Main): {safe_float(balance.get('USDT', {}).get('free')):.2f} USDT")
                except: pass

                print("\n📊 СТАН РИНКУ, ПОЗИЦІЙ ТА ЛІМІТОК:")
                for symbol in SYMBOLS:
                    trend, last_p = check_global_trend(symbol)
                    pos_status = "Вільна"
                    if symbol in real_positions:
                        p = real_positions[symbol]
                        p_size = safe_float(p.get('contracts') or p.get('info', {}).get('amount'))
                        pnl = safe_float(p.get('unrealizedPnl') or p.get('info', {}).get('pnl'))
                        pos_status = f"Є ПОЗИЦІЯ ({'LONG' if p_size > 0 else 'SHORT'}) | PnL: {pnl:.2f} USDT"
                    elif symbol in active_traps:
                        pos_status = f"ЧЕКАЄ ЛІМІТКА ({active_traps[symbol]['side'].upper()}) по {active_traps[symbol]['price']}"
                    
                    print(f"  • {symbol:<15} | Тренд 1h: {trend:<10} | Ціна: {last_p:<9.4f} | Стан: {pos_status}")
                print("==================================================================\n")
                last_heartbeat_hour = current_time.hour

            # --- АНАЛІЗ ТА ТОРГІВЛЯ ---
            for symbol in SYMBOLS:
                # КРОК 5: Якщо позиції немає, але залишилися старі ордери-сироти (наприклад, стопи від минулої угоди) — чистимо їх
                if symbol not in real_positions and symbol not in active_traps:
                    clean_orphan_orders(symbol)

                if symbol in real_positions: 
                    continue  # По монеті вже йде активна торгівля

                closes_15m, volumes_15m = get_ohlcv_data(symbol, TIMEFRAME_TRADE)
                if not closes_15m or len(volumes_15m) < 21: continue

                current_idx = len(closes_15m)
                handle_traps_and_timeouts(symbol, current_idx)
                if symbol in active_traps: continue

                # Аналіз умов для пастки
                ema_12 = calculate_ema(closes_15m, 12)
                avg_vol_20 = sum(volumes_15m[-21:-1]) / 20  # Середній об'єм без поточної свічки
                global_trend, _ = check_global_trend(symbol)

                # Перевірка на аномальний об'єм на попередній закритій свічці
                if volumes_15m[-2] > (avg_vol_20 * VOLUME_MULTIPLIER):
                    
                    # LONG пастка
                    if global_trend == "LONG_ONLY" and closes_15m[-1] > ema_12:
                        print(f"🕸️ [СИГНАЛ LONG] {symbol}. Об'єм аномальний. Ставимо лімітку на EMA-12: {ema_12:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'buy', ema_12)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_idx': current_idx, 'side': 'buy', 'price': prc, 'amount': size}

                    # SHORT пастка
                    elif global_trend == "SHORT_ONLY" and closes_15m[-1] < ema_12:
                        print(f"🕸️ [СИГНАЛ SHORT] {symbol}. Об'єм аномальний. Ставимо лімітку на EMA-12: {ema_12:.4f}")
                        oid, size, prc = place_trap_order(symbol, 'sell', ema_12)
                        if oid:
                            active_traps[symbol] = {'order_id': oid, 'placed_idx': current_idx, 'side': 'sell', 'price': prc, 'amount': size}

            time.sleep(15)  # Перевірка кожні 15 секунд

        except Exception as e:
            print(f"🚨 Помилка в головному циклі: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()
