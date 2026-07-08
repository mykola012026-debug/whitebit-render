import time
import ccxt

# ==========================================
# ⚙️ НАЛАШТУВАННЯ ТОРГОВОГО БОТА
# ==========================================
API_KEY = "69be7ce4-811d-4402-a720-3055ba257bc5"
API_SECRET = "M7CqL2K3X0gV79R1pW4uNjD5sY8zA2e6tB9"

# Список монет для торгівлі (Ф'ючерси WhiteBIT)
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ONDO/USDT:USDT', 
    'LINK/USDT:USDT', 'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 
    'FET/USDT:USDT', 'SOL/USDT:USDT', 'SUI/USDT:USDT'
]

# Параметри стратегії
TIMEFRAME_1H = '1h'
TIMEFRAME_15M = '15m'

VOLUME_THRESHOLD_MIN = 1.1
VOLUME_THRESHOLD_MAX = 1.4
RSI_SHORT_LEVEL = 45
RSI_LONG_LEVEL = 55

# Управління ризиками
RISK_PER_TRADE_USDT = 10.0  # Сума на одну угоду
TAKE_PROFIT_PCT = 0.012     # +1.2%
STOP_LOSS_PCT = 0.006       # -0.6%

# Глобальне сховище пасток (Ордери на вхід під контролем бота)
# Структура: { order_id: { "symbol": ..., "placed_at_candle_idx": ..., "target_price": ..., "amount": ..., "side": ... } }
active_traps = {}

# Ініціалізація підключення до WhiteBIT
exchange = ccxt.whitebit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
})

# ==========================================
# 🛠️ ДОПОМІЖНІ ФУНКЦІЇ ТА ТЕХНІЧНІ ІНДИКАТОРИ
# ==========================================

def calculate_ema(prices, period=12):
    """ Розрахунок Exponential Moving Average (EMA) """
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_rsi(prices, period=14):
    """ Розрахунок Relative Strength Index (RSI) """
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        return 100
        
    for i in range(period, len(prices) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_current_candle_index(symbol, timeframe='15m'):
    """ Отримання унікального індексу (timestamp) поточної свічки """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=1)
        if ohlcv:
            return ohlcv[0][0]
    except:
        pass
    return int(time.time() * 1000)

def has_trap_for_symbol(symbol):
    """ Перевірка, чи вже виставлена пастка по конкретному інструменту """
    for trap in active_traps.values():
        if trap['symbol'] == symbol:
            return True
    return False

# ==========================================
# 🔄 МОДУЛЬ СИНХРОНІЗАЦІЇ ТА ЗАХИСТУ ВІД ПЕРЕЗАПУСКІВ
# ==========================================

def sync_active_traps_with_exchange():
    """ Сканує відкриті ордери на біржі та відновлює active_traps в пам'яті """
    global active_traps
    print("🔄 Сканування відкритих ордерів на WhiteBIT для синхронізації пам'яті...")
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        open_orders = exchange.fetch_open_orders()
        
        current_idx = get_current_candle_index(SYMBOLS[0], TIMEFRAME_15M)
        synced_count = 0
        
        for order in open_orders:
            symbol = order.get('symbol')
            # Перевіряємо, чи цей ордер належить до нашого списку ф'ючерсів та чи є він лімітним
            if symbol in SYMBOLS and order.get('type') == 'limit':
                order_id = str(order['id'])
                
                # Пропускаємо стоп-ордери (TP/SL), у WhiteBIT вони зазвичай мають інші параметри або маркування
                if order.get('info', {}).get('type') in ['stopMarket', 'stopLimit'] or 'activationPrice' in order.get('info', {}):
                    continue
                    
                if order_id not in active_traps:
                    active_traps[order_id] = {
                        'symbol': symbol,
                        'placed_at_candle_idx': current_idx, # Даємо ордеру нові 30 хвилин життя з моменту перезапуску
                        'target_price': float(order['price']),
                        'amount': float(order['amount']),
                        'side': order['side'].lower()
                    }
                    synced_count += 1
                    
        print(f"✅ Синхронізація завершена. Взято під контроль пасток: {synced_count}\n")
    except Exception as e:
        print(f"⚠️ Помилка синхронізації пасток: {e}\n")

# ==========================================
# 🛡️ АВТОМАТИЧНЕ ВИСТАВЛЕННЯ СТОП-ОРДЕРІВ (TP/SL)
# ==========================================

def set_tp_sl_protection(symbol, side, entry_price, amount):
    """ Виставляє пов'язані ордери Take Profit та Stop Loss """
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        
        if side == 'buy':  # Захист для LONG позиції
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
            sl_price = entry_price * (1 - STOP_LOSS_PCT)
            close_side = 'sell'
        else:  # Захист для SHORT позиції
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
            sl_price = entry_price * (1 + STOP_LOSS_PCT)
            close_side = 'buy'

        # Округлення цін відповідно до вимог біржі
        market_info = exchange.market(symbol)
        tp_price = float(exchange.price_to_precision(symbol, tp_price))
        sl_price = float(exchange.price_to_precision(symbol, sl_price))
        amount = float(exchange.amount_to_precision(symbol, amount))

        print(f"📡 Відправка захисних ордерів на WhiteBIT... (TP: {tp_price} | SL: {sl_price})")

        # 1. Виставлення Take Profit (Трейд-ордер типу stopMarket)
        tp_params = {
            'stopPrice': tp_price, 
            'activationPrice': tp_price,
            'reduceOnly': True
        }
        exchange.create_order(symbol, 'stopMarket', close_side, amount, params=tp_params)
        print(f"✅ [Захист] Take Profit активовано.")

        # 2. Виставлення Stop Loss (Трейд-ордер типу stopMarket)
        sl_params = {
            'stopPrice': sl_price, 
            'activationPrice': sl_price,
            'reduceOnly': True
        }
        exchange.create_order(symbol, 'stopMarket', close_side, amount, params=sl_params)
        print(f"✅ [Захист] Stop Loss активовано. Позиція повністю захищена!")

    except Exception as e:
        print(f"❌ Помилка під час спроби встановити захист TP/SL для {symbol}: {e}")

# ==========================================
# 🕵️ МОДУЛЬ СТРАТЕГІЇ ТА ПОШУКУ СИГНАЛІВ
# ==========================================

def scan_market_and_place_traps():
    """ Сканує ринок та розставляє лімітки (пастки) на EMA-12 """
    global active_traps
    
    for symbol in SYMBOLS:
        try:
            # Захист: якщо по цій монеті вже висить лімітка в пам'яті, ігноруємо її
            if has_trap_for_symbol(symbol):
                continue
                
            exchange.options['accountsByType'] = {'swap': 'collateral'}
            
            # 1. Визначення Глобального Тренду (1 година)
            ohlcv_1h = exchange.fetch_ohlcv(symbol, TIMEFRAME_1H, limit=50)
            closes_1h = [c[4] for c in ohlcv_1h]
            ema_1h = calculate_ema(closes_1h, 12)
            current_price = closes_1h[-1]
            
            trend = "NEUTRAL"
            if ema_1h:
                trend = "LONG_ONLY" if current_price > ema_1h else "SHORT_ONLY"
            
            # 2. Перевірка локального імпульсу (15 хвилин)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, TIMEFRAME_15M, limit=50)
            closes_15m = [c[4] for c in ohlcv_15m]
            volumes_15m = [c[5] for c in ohlcv_15m]
            
            rsi_15m = calculate_rsi(closes_15m, 14)
            ema_12_15m = calculate_ema(closes_15m, 12)
            
            # Розрахунок коефіцієнта об'єму (сплеску)
            last_volume = volumes_15m[-1]
            avg_volume = sum(volumes_15m[-11:-1]) / 10 if len(volumes_15m) >= 11 else last_volume
            vol_ratio = last_volume / avg_volume if avg_volume > 0 else 1.0
            
            # 3. Перевірка умов для відкриття пастки
            if trend == "SHORT_ONLY" and vol_ratio >= VOLUME_THRESHOLD_MIN and vol_ratio <= VOLUME_THRESHOLD_MAX:
                if rsi_15m and rsi_15m <= RSI_SHORT_LEVEL and ema_12_15m:
                    
                    # Ставимо пастку SHORT на лінію EMA-12
                    target_price = float(exchange.price_to_precision(symbol, ema_12_15m))
                    if target_price > current_price:  # EMA має бути вище ціни для шорт-лімітки
                        
                        amount = RISK_PER_TRADE_USDT / target_price
                        amount = float(exchange.amount_to_precision(symbol, amount))
                        
                        print(f"🕸️ [ПАСТКА SHORT] {symbol}. Коеф: {vol_ratio:.2f}. Лімітка на EMA-12: {target_price}")
                        order = exchange.create_order(symbol, 'limit', 'sell', amount, target_price)
                        order_id = str(order['id'])
                        
                        active_traps[order_id] = {
                            'symbol': symbol,
                            'placed_at_candle_idx': get_current_candle_index(symbol, TIMEFRAME_15M),
                            'target_price': target_price,
                            'amount': amount,
                            'side': 'sell'
                        }
                        print(f"✅ Лімітний ордер виставлено! ID: {order_id}")
                        
            elif trend == "LONG_ONLY" and vol_ratio >= VOLUME_THRESHOLD_MIN and vol_ratio <= VOLUME_THRESHOLD_MAX:
                if rsi_15m and rsi_15m >= RSI_LONG_LEVEL and ema_12_15m:
                    
                    # Ставимо пастку LONG на лінію EMA-12
                    target_price = float(exchange.price_to_precision(symbol, ema_12_15m))
                    if target_price < current_price:  # EMA має бути нижче ціни для лонг-лімітки
                        
                        amount = RISK_PER_TRADE_USDT / target_price
                        amount = float(exchange.amount_to_precision(symbol, amount))
                        
                        print(f"🕸️ [ПАСТКА LONG] {symbol}. Коеф: {vol_ratio:.2f}. Лімітка на EMA-12: {target_price}")
                        order = exchange.create_order(symbol, 'limit', 'buy', amount, target_price)
                        order_id = str(order['id'])
                        
                        active_traps[order_id] = {
                            'symbol': symbol,
                            'placed_at_candle_idx': get_current_candle_index(symbol, TIMEFRAME_15M),
                            'target_price': target_price,
                            'amount': amount,
                            'side': 'buy'
                        }
                        print(f"✅ Лімітний ордер виставлено! ID: {order_id}")
                        
        except Exception as e:
            print(f"⚠️ Помилка аналізу ринку для {symbol}: {e}")

# ==========================================
# ⏱️ МОДУЛЬ ЖИТТЄВОГО ЦИКЛУ ПАСТОК ТА НАЛИВУ
# ==========================================

def handle_traps_timeout_and_fills():
    """ Слідкує за виконанням ордерів або видаляє їх після 2 свічок (30 хв) """
    global active_traps
    orders_to_clean = []
    
    for order_id, trap in list(active_traps.items()):
        symbol = trap['symbol']
        try:
            exchange.options['accountsByType'] = {'swap': 'collateral'}
            order = exchange.fetch_order(order_id, symbol)
            
            # Сценарій 1: Пастка спрацювала (Ордер виконано)
            if order['status'] == 'closed':
                print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Ордер повністю виконано по {symbol}!")
                
                # Захист від збоїв ціни API: якщо біржа повернула криве поле price, беремо нашу розрахункову ціну
                filled_price = float(order.get('price') or 0)
                if filled_price == 0 or filled_price > trap['target_price'] * 2 or filled_price < trap['target_price'] * 0.5:
                    filled_price = trap['target_price']
                    
                amount = float(order.get('amount') or trap['amount'])
                side = trap['side']
                
                # Викликаємо автоматичний захист TP/SL
                set_tp_sl_protection(symbol, side, filled_price, amount)
                orders_to_clean.append(order_id)
                continue
                
            # Сценарій 2: Таймаут пастки (Пройшло 2 свічки по 15 хвилин)
            current_candle_idx = get_current_candle_index(symbol, TIMEFRAME_15M)
            if current_candle_idx - trap['placed_at_candle_idx'] >= 2 * 15 * 60 * 1000:
                print(f"⏱️ [ТАЙМАУТ] Пастка по {symbol} застаріла (висіла 30 хв). Видаляємо ордер з біржі...")
                exchange.cancel_order(order_id, symbol)
                orders_to_clean.append(order_id)
                
        except Exception as e:
            # Якщо ордер видалено на біржі вручну користувачем
            if "Order not found" in str(e) or "ORDER_NOT_FOUND" in str(e):
                orders_to_clean.append(order_id)
            else:
                print(f"⚠️ Помилка обробки статусу пастки {order_id}: {e}")
                
    # Очищення пам'яті
    for order_id in orders_to_clean:
        if order_id in active_traps:
            del active_traps[order_id]

# ==========================================
# 🚀 МОДУЛЬ «КАТАПУЛЬТА» (КЕРУВАННЯ ВІДКРИТИМИ УГОДАМИ)
# ==========================================

def manage_open_positions():
    """ Моніторить відкриті позиції та сигналізує про можливість переносу стопу """
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        positions = exchange.fetch_positions(SYMBOLS)
        
        for pos in positions:
            symbol = pos['symbol']
            p_size = float(pos.get('contracts') or pos.get('info', {}).get('amount', 0) or 0)
            
            # Пропускаємо монети, де немає відкритих позицій
            if symbol not in SYMBOLS or abs(p_size) == 0: 
                continue

            # Екстрений захист від NoneType з боку WhiteBIT під час оновлення даних
            if pos.get('entryPrice') is None or pos.get('markPrice') is None:
                continue

            entry_price = float(pos['entryPrice'])
            current_price = float(pos['markPrice'])
            
            if entry_price == 0: 
                continue
            
            side = 'long' if p_size > 0 else 'short'
            
            # Розрахунок чистого руху ціни у відсотках
            if side == 'long':
                p_diff = (current_price - entry_price) / entry_price
            else:
                p_diff = (entry_price - current_price) / entry_price

            # Сигнал Катапульти, якщо пройшли більше ніж +0.4%
            if p_diff >= 0.004:
                print(f"🚀 [КАТАПУЛЬТА] {symbol} пройшов {p_diff*100:.2f}%. Рекомендується перенести стоп у безубиток по ціні {entry_price}")
                
    except Exception as e:
        print(f"❌ Помилка в модулі Катапульта: {e}")

# ==========================================
# 📊 МОДУЛЬ ЗВІТНОСТІ ТА МОНІТОРИНГУ
# ==========================================

def print_system_report():
    """ Виводить у консоль поточний стан балансу, індикаторів та пасток """
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        balance = exchange.fetch_balance()
        usdt_info = balance.get('USDT', {})
        
        total_bal = usdt_info.get('total', 0.0)
        free_bal = usdt_info.get('free', 0.0)
        used_bal = usdt_info.get('used', 0.0)
        
        print("\n⚡ ========== МАКСИМАЛЬНИЙ ЗВІТ СИСТЕМИ ==========")
        print(f"💰 БАЛАНС USDT (Collateral) -> Всього: {total_bal:.2f} | Вільно: {free_bal:.2f} | В ордерах: {used_bal:.2f}\n")
        print("📊 СТАН РИНКУ ТА ІНДИКАТОРІВ:")
        
        for symbol in SYMBOLS:
            try:
                ohlcv_1h = exchange.fetch_ohlcv(symbol, TIMEFRAME_1H, limit=20)
                closes_1h = [c[4] for c in ohlcv_1h]
                ema_1h = calculate_ema(closes_1h, 12)
                current_price = closes_1h[-1]
                trend = "LONG_ONLY" if ema_1h and current_price > ema_1h else "SHORT_ONLY"
                
                ohlcv_15m = exchange.fetch_ohlcv(symbol, TIMEFRAME_15M, limit=30)
                closes_15m = [c[4] for c in ohlcv_15m]
                volumes_15m = [c[5] for c in ohlcv_15m]
                rsi_15m = calculate_rsi(closes_15m, 14)
                
                last_volume = volumes_15m[-1]
                avg_volume = sum(volumes_15m[-11:-1]) / 10 if len(volumes_15m) >= 11 else last_volume
                
                status = "Вільна"
                for trap in active_traps.values():
                    if trap['symbol'] == symbol:
                        status = f"Пастка активна ({trap['side'].upper()})"
                        
                rsi_str = f"{rsi_15m:.1f}" if rsi_15m else "N/A"
                print(f"  • {symbol.ljust(16)} | Тренд 1h: {trend.ljust(10)} | Ціна: {current_price:<10} | RSI 15m: {rsi_str:<4} | Об'єм: {last_volume:.1f}/{avg_volume:.1f} | Стан: {status}")
            except:
                print(f"  • {symbol.ljust(16)} | Помилка збору даних індикаторів.")
                
        print("\n📦 АКТИВНІ ПАСТКИ В ПАМ'ЯТІ:")
        if not active_traps:
            print("    • Немає активних ліміток на біржі.")
        else:
            for o_id, trap in active_traps.items():
                print(f"    • [{trap['symbol']}] Лімітний ордер {trap['side'].upper()} чекає на ціні {trap['target_price']}. ID: {o_id}")
                
        print("\n💚 Статус: Скрипт захищений від перезапусків.")
        print("==================================================\n")
    except Exception as e:
        print(f"⚠️ Помилка генерації звіту системи: {e}")

# ==========================================
# 🏁 ГОЛОВНИЙ ЦИКЛ ЗАПУСКУ БОТА
# ==========================================

if __name__ == "__main__":
    print("🚀 Запуск торгового бота на WhiteBIT Futures...")
    
    # Крок 1. Первинна синхронізація з біржею (захист від втрати даних при перезапуску)
    sync_active_traps_with_exchange()
    
    report_timer = 0
    
    while True:
        # Крок 2. Скануємо ринок та розставляємо нові пастки
        scan_market_and_place_traps()
        
        # Крок 3. Перевіряємо таймаути старих пасток та факт виконання налитих ордерів
        handle_traps_timeout_and_fills()
        
        # Крок 4. Запускаємо стеження модуля "Катапульта" за діючими угодами
        manage_open_positions()
        
        # Крок 5. Вивід великого звіту один раз на 5 хвилин (300 секунд)
        if report_timer >= 300:
            print_system_report()
            report_timer = 0
            
        time.sleep(10)  # Пауза між ітераціями циклу (10 секунд)
        report_timer += 10
