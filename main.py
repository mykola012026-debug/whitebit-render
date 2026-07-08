import time
from datetime import datetime
import ccxt

# --- НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ ---
API_KEY = '9dfcbc7d6c30802daf10d0bb50bf50d1'
API_SECRET = '4ff8480b5bb8914e4dacf7ac40401762'

exchange = ccxt.whitebit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
        'accountsByType': {
            'swap': 'collateral'
        }
    }
})

print("⏳ Завантаження ринків WhiteBIT...")
exchange.load_markets()
print("✅ Ринки успішно завантажені.")

SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ONDO/USDT:USDT', 'LINK/USDT:USDT', 
    'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'FET/USDT:USDT', 'SOL/USDT:USDT', 'SUI/USDT:USDT'
]
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

BASE_POSITION_VOLUME = 6.0  # Об'єм ордера в USDT
TP_PERCENT = 0.012          # +1.2%
SL_PERCENT = 0.006          # -0.6%

VOLUME_THRESHOLD_MIN = 1.1   
VOLUME_THRESHOLD_MAX = 1.4

active_traps = {}
last_heartbeat_hour = -1

# --- МАТЕМАТИЧНІ ФУНКЦІЇ ---
def calculate_ema(prices, period):
    if len(prices) < period: return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_rsi(prices, period=14):
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

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_dynamic_threshold(volumes):
    if len(volumes) < 20: return VOLUME_THRESHOLD_MIN
    last_20 = volumes[-20:]
    mean_vol = sum(last_20) / 20
    if mean_vol == 0: return VOLUME_THRESHOLD_MIN

    variance = sum((x - mean_vol) ** 2 for x in last_20) / 20
    std_vol = variance ** 0.5

    cv = std_vol / mean_vol
    threshold = VOLUME_THRESHOLD_MIN + (cv * 0.2)
    return min(max(threshold, VOLUME_THRESHOLD_MIN), VOLUME_THRESHOLD_MAX)

# --- ЛОГІКА ДАНИХ ТА МОНІТОРИНГУ ---
def get_crypto_close_and_volume(symbol, timeframe):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]
        return closes, volumes
    except:
        return None, None

def check_global_trend(symbol):
    closes, _ = get_crypto_close_and_volume(symbol, TIMEFRAME_TREND)
    if not closes: return "FLAT", 0, 0
    ema_50 = calculate_ema(closes, 50)
    last_price = closes[-1]

    if last_price > ema_50: return "LONG_ONLY", last_price, ema_50
    if last_price < ema_50: return "SHORT_ONLY", last_price, ema_50
    return "FLAT", last_price, ema_50

def has_active_position(symbol):
    try:
        positions = exchange.fetch_positions([symbol], params={'type': 'swap'})
        for pos in positions:
            if pos['symbol'] == symbol:
                p_size_raw = pos.get('contracts') or pos.get('info', {}).get('amount')
                if p_size_raw is not None and abs(float(p_size_raw)) > 0.000001: 
                    return True
        return False
    except:
        return False

# --- СИНХРОНІЗАЦІЯ ПРИ ПЕРЕЗАПУСКУ ---
def sync_existing_traps_on_startup():
    global active_traps
    print("🔄 Сканування біржі на наявність раніше відкритих ліміток...")
    try:
        for symbol in SYMBOLS:
            try:
                open_orders = exchange.fetch_open_orders(symbol, params={'type': 'swap'})
                for order in open_orders:
                    if order.get('type') == 'limit' and 'stopPrice' not in order and 'triggerPrice' not in order:
                        closes, _ = get_crypto_close_and_volume(symbol, TIMEFRAME_TRADE)
                        current_idx = len(closes) if closes else 100

                        active_traps[symbol] = {
                            'order_id': str(order['id']),
                            'placed_at_candle_idx': current_idx,
                            'side': order['side'].lower(),
                            'target_price': float(order['price']),
                            'amount': float(order['amount'])
                        }
                        print(f"🔗 Знайдено та взято під контроль ордер {order['side'].upper()} по {symbol} (ID: {order['id']})")
            except Exception as sym_err:
                print(f"⚠️ Не вдалося зчитати ордери для {symbol}: {sym_err}")
        print(f"✅ Синхронізація завершена. Взято під контроль пасток: {len(active_traps)}")
    except Exception as e:
        print(f"⚠️ Загальна помилка синхронізації старих ордерів: {e}")

# --- АВТОМАТИЧНИЙ ЗАХИСТ ПОЗИЦІЇ (TP/SL) ---
def set_tp_sl_protection(symbol, side, filled_price, amount):
    try:
        if side == 'buy':
            tp_price = filled_price * (1 + TP_PERCENT)
            sl_price = filled_price * (1 - SL_PERCENT)
            close_side = 'sell'
        else:
            tp_price = filled_price * (1 - TP_PERCENT)
            sl_price = filled_price * (1 + SL_PERCENT)
            close_side = 'buy'

        tp_price_formatted = float(exchange.price_to_precision(symbol, tp_price))
        sl_price_formatted = float(exchange.price_to_precision(symbol, sl_price))
        amount_formatted = float(exchange.amount_to_precision(symbol, amount))

        print(f"🛡️ [ЗАХИСТ ВХОДУ] Виставляємо TP/SL для {symbol} ({side.upper()})")

        tp_params = {'triggerPrice': tp_price_formatted, 'activationPrice': tp_price_formatted, 'reduceOnly': True, 'type': 'swap'}
        tp_order = exchange.create_order(symbol, 'stopMarket', close_side, amount_formatted, params=tp_params)
        print(f"   🎯 Take Profit виставлено на рівень: {tp_price_formatted} (ID: {tp_order['id']})")

        sl_params = {'triggerPrice': sl_price_formatted, 'activationPrice': sl_price_formatted, 'reduceOnly': True, 'type': 'swap'}
        sl_order = exchange.create_order(symbol, 'stopMarket', close_side, amount_formatted, params=sl_params)
        print(f"   🛑 Stop Loss виставлено на рівень: {sl_price_formatted} (ID: {sl_order['id']})")

    except Exception as e:
        print(f"❌ Помилка під час спроби встановити захист TP/SL для {symbol}: {e}")

# --- МОНІТОР СТАНУ ТА ТАЙМАУТІВ ---
def handle_traps_timeout(current_candle_idx, symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        order = exchange.fetch_order(trap['order_id'], symbol, params={'type': 'swap'})

        if order['status'] == 'closed':
            print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Ордер повністю виконано по {symbol}!")
            filled_price = float(order.get('average') or order.get('price') or trap['target_price'])
            amount = float(order.get('amount', trap['amount']))
            side = trap['side']

            set_tp_sl_protection(symbol, side, filled_price, amount)
            del active_traps[symbol]
            return

        elif order['status'] == 'canceled':
            print(f"🗑️ Ордер по {symbol} було скасовано ззовні. Видаляємо з пам'яті бота.")
            del active_traps[symbol]
            return

        if current_candle_idx - trap['placed_at_candle_idx'] >= 2:
            print(f"⏰ [ТАЙМАУТ ПАСТКИ] Минуло 30 хв. Видаляємо лімітку по {symbol}...")
            try:
                exchange.cancel_order(trap['order_id'], symbol, params={'type': 'swap'})
            except:
                pass
            del active_traps[symbol]

    except Exception as e:
        if "Order not found" in str(e) or "ORDER_NOT_FOUND" in str(e):
            if symbol in active_traps: del active_traps[symbol]
        else:
            print(f"❌ Помилка перевірки стану пастки для {symbol}: {e}")

# --- МОДУЛЬ КАТАПУЛЬТА (ПОВНІСТЮ АВТОНОМНИЙ ДЛЯ КОЖНОЇ МОНЕТИ) ---
def manage_open_positions():
    try:
        # Запитуємо позиції тільки для нашого списку токенів
        positions = exchange.fetch_positions(SYMBOLS, params={'type': 'swap'})
        
        for pos in positions:
            # Ізолюємо обробку кожної конкретної позиції, щоб порожні пари не ламали весь цикл
            try:
                symbol = pos.get('symbol')
                if not symbol or symbol not in SYMBOLS: 
                    continue
                
                # Перевірка наявності даних про розмір позиції
                p_size_raw = pos.get('contracts') or pos.get('info', {}).get('amount')
                if p_size_raw is None:
                    continue
                    
                p_size = float(p_size_raw)
                if abs(p_size) < 0.000001: 
                    continue  # Позиції немає, йдемо далі

                # Тільки якщо позиція РЕАЛЬНО існує, перевіряємо ціни
                entry_raw = pos.get('entryPrice')
                mark_raw = pos.get('markPrice')
                
                if entry_raw is None or mark_raw is None:
                    continue  # Біржа ще не віддала ціни для цієї свіжої позиції, пропуск

                entry_price = float(entry_raw)
                current_price = float(mark_raw)
                if entry_price == 0: 
                    continue
                
                side = 'long' if p_size > 0 else 'short'
                p_diff = (current_price - entry_price) / entry_price if side == 'long' else (entry_price - current_price) / entry_price

                if p_diff >= 0.004:
                    print(f"🚀 [КАТАПУЛЬТА] {symbol} пройшов {p_diff*100:.2f}%. Рекомендується перенести стоп в БУ по {entry_price}")
            
            except (ValueError, TypeError):
                # Пропускаємо помилки конвертації окремої монети
                continue
            except Exception as item_err:
                # Будь-яка інша проблема з одним токеном не заважає перевірити решту
                continue

    except Exception as e:
        print(f"❌ Критична помилка в модулі Катапульта: {e}")

# --- ГОЛОВНИЙ ЦИКЛ БОТА ---
def main_cycle():
    global last_heartbeat_hour
    print(f"🤖 Бот Lyra V2 з авто-синхронізацією ліміток запущений.")

    sync_existing_traps_on_startup()

    while True:
        try:
            current_time = datetime.now()

            # --- ЩОГОДИННИЙ СИСТЕМНИЙ ЗВІТ ---
            if current_time.hour != last_heartbeat_hour:
                print(f"\n⚡ [{current_time.strftime('%H:%M:%S')}] ========== МАКСИМАЛЬНИЙ ЗВІТ СИСТЕМИ ==========")

                try:
                    balance = exchange.fetch_balance(params={'type': 'swap'})
                    usdt_total = float(balance.get('total', {}).get('USDT', 0.0) or 0.0)
                    usdt_free = float(balance.get('free', {}).get('USDT', 0.0) or 0.0)
                    usdt_used = float(balance.get('used', {}).get('USDT', 0.0) or 0.0)
                    print(f"💰 БАЛАНС USDT (Collateral) -> Всього: {usdt_total:.2f} | Вільно: {usdt_free:.2f} | В ордерах: {usdt_used:.2f}")
                except Exception as b_err:
                    print(f"💰 БАЛАНС USDT -> Не вдалося порахувати баланс: {b_err}")

                print("\n📊 СТАН РИНКУ ТА ІНДИКАТОРІВ:")
                for symbol in SYMBOLS:
                    trend, last_p, ema_50_1h = check_global_trend(symbol)
                    closes_15m, volumes_15m = get_crypto_close_and_volume(symbol, TIMEFRAME_TRADE)

                    if closes_15m and volumes_15m:
                        rsi_15m = calculate_rsi(closes_15m, 14)
                        last_vol = volumes_15m[-1]
                        avg_vol = sum(volumes_15m[-20:]) / 20
                        coef = calculate_dynamic_threshold(volumes_15m)
                        vol_status = f"{last_vol:.1f}/{avg_vol*coef:.1f}"

                        pos_status = "Є ПОЗИЦІЯ" if has_active_position(symbol) else "Вільна"
                        print(f"  • {symbol:<16} | Тренд 1h: {trend:<10} | Ціна: {last_p:<8.4f} | RSI 15m: {rsi_15m:.1f} | Об'єм: {vol_status} | Стан: {pos_status}")

                print("\n📦 АКТИВНІ ПАСТКИ В ПАМ'ЯТІ:")
                if active_traps:
                    for s, t in active_traps.items():
                        print(f"   • [{s}] Лімітний ордер {t['side'].upper()} чекає на ціні {t['target_price']}. ID: {t['order_id']}")
                else:
                    print("   • Жодних виставлених ліміток зараз немає.")

                print(f"\n💚 Статус: Скрипт повністю захищений від перезапусків.")
                print("==================================================================\n")
                last_heartbeat_hour = current_time.hour

            # --- УПРАВЛІННЯ ВІДКРИТИМИ УГОДАМИ ---
            manage_open_positions()

            # --- АНАЛІЗ ТА ТОРГІВЛЯ ---
            for symbol in SYMBOLS:
                if has_active_position(symbol): continue

                closes, volumes = get_crypto_close_and_volume(symbol, TIMEFRAME_TRADE)
                if not closes or not volumes: continue

                current_candle_idx = len(closes)
                handle_traps_timeout(current_candle_idx, symbol)
                if symbol in active_traps: continue

                ema_12 = calculate_ema(closes, 12)
                rsi = calculate_rsi(closes, 14)

                avg_vol_20 = sum(volumes[-20:]) / 20
                dynamic_coef = calculate_dynamic_threshold(volumes)

                if volumes[-1] > (avg_vol_20 * dynamic_coef):
                    global_trend, _, _ = check_global_trend(symbol)

                    # LONG ПАСТКА
                    if global_trend == "LONG_ONLY" and rsi < 65 and closes[-1] > ema_12:
                        print(f"🕸️ [ПАСТКА LONG] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {ema_12}")

                        raw_amount = BASE_POSITION_VOLUME / ema_12
                        market_info = exchange.market(symbol)
                        min_qty = float(market_info['limits']['amount']['min'] or 0.001)
                        if raw_amount < min_qty: raw_amount = min_qty

                        amount = float(exchange.amount_to_precision(symbol, raw_amount))
                        price = float(exchange.price_to_precision(symbol, ema_12))

                        try:
                            order = exchange.create_order(symbol, 'limit', 'buy', amount, price, params={'type': 'swap'})
                            print(f"✅ Лімітний ордер LONG виставлено! ID: {order['id']}")

                            active_traps[symbol] = {
                                'order_id': str(order['id']), 
                                'placed_at_candle_idx': current_candle_idx,
                                'side': 'buy',
                                'target_price': price,
                                'amount': amount
                            }
                        except ccxt.InsufficientFunds:
                            print(f"⚠️ Мало маржинального балансу для LONG по {symbol}.")
                        except Exception as order_err:
                            print(f"❌ Помилка створення LONG по {symbol}: {order_err}")

                    # SHORT ПАСТКА
                    elif global_trend == "SHORT_ONLY" and rsi > 35 and closes[-1] < ema_12:
                        print(f"🕸️ [ПАСТКА SHORT] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {ema_12}")

                        raw_amount = BASE_POSITION_VOLUME / ema_12
                        market_info = exchange.market(symbol)
                        min_qty = float(market_info['limits']['amount']['min'] or 0.001)
                        if raw_amount < min_qty: raw_amount = min_qty

                        amount = float(exchange.amount_to_precision(symbol, raw_amount))
                        price = float(exchange.price_to_precision(symbol, ema_12))

                        try:
                            order = exchange.create_order(symbol, 'limit', 'sell', amount, price, params={'type': 'swap'})
                            print(f"✅ Лімітний ордер SHORT виставлено! ID: {order['id']}")

                            active_traps[symbol] = {
                                'order_id': str(order['id']), 
                                'placed_at_candle_idx': current_candle_idx,
                                'side': 'sell',
                                'target_price': price,
                                'amount': amount
                            }
                        except ccxt.InsufficientFunds:
                            print(f"⚠️ Мало маржинального балансу для SHORT по {symbol}.")
                        except Exception as order_err:
                            print(f"❌ Помилка створення SHORT по {symbol}: {order_err}")

            time.sleep(30)

        except Exception as e:
            print(f"🚨 Критична помилка в циклі: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()
