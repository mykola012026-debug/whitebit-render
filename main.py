import time
from datetime import datetime
import ccxt

# --- НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ ---
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# Потрібно завантажити специфікації ринків для точного округлення
print("⏳ Завантаження ринків WhiteBIT...")
exchange.load_markets()
print("✅ Ринки успішно завантажені.")

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'ONDO/USDT', 'LINK/USDT', 'NEAR/USDT', 'RENDER/USDT', 'FET/USDT', 'SOL/USDT', 'SUI/USDT']
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

# Базова вартість позиції. З плечем 10 застава буде всього ~2 USDT на угоду
BASE_POSITION_VOLUME = 20.0  

active_traps = {}
last_heartbeat_hour = -1

# --- МАТЕМАТИЧНІ ФУНКЦІЇ НА ЧИСТОМУ PYTHON ---
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
    if len(volumes) < 20: return 1.6
    last_20 = volumes[-20:]
    mean_vol = sum(last_20) / 20
    if mean_vol == 0: return 1.6

    variance = sum((x - mean_vol) ** 2 for x in last_20) / 20
    std_vol = variance ** 0.5

    cv = std_vol / mean_vol
    threshold = 1.6 + (cv * 0.5)
    return min(max(threshold, 1.6), 2.5)

# --- ЛОГІКА ДАНИХ ТА МОНІТОРИНГУ ---
def get_crypto_close_and_volume(symbol, timeframe):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]
        return closes, volumes
    except Exception as e:
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
        positions = exchange.fetch_positions()
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0: 
                return True
        return False
    except:
        return True

def manage_open_positions():
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
    except Exception as e:
        print(f"❌ Помилка в модулі Катапульта: {e}")

def handle_traps_timeout(current_candle_idx, symbol):
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        order = exchange.fetch_order(trap['order_id'], symbol)
        if order['status'] == 'closed':
            print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Ми в позиції по {symbol}!")
            del active_traps[symbol]
            return
        elif order['status'] == 'canceled':
            del active_traps[symbol]
            return

        if current_candle_idx - trap['placed_at_candle_idx'] >= 2:
            print(f"⏰ [ТАЙМАУТ ПАСТКИ] Минуло 30 хв. Видаляємо лімітку по {symbol}")
            exchange.cancel_order(trap['order_id'], symbol)
            del active_traps[symbol]
    except Exception as e:
        print(f"❌ Помилка перевірки пастки для {symbol}: {e}")

# --- ГОЛОВНИЙ ЦИКЛ БОТА ---
def main_cycle():
    global last_heartbeat_hour
    print("🤖 Бот Lyra V2 Макс-Інфо активований. Повний моніторинг запущено.")

    while True:
        try:
            current_time = datetime.now()

            # --- РОЗШИРЕНИЙ БЛОК ЩОГОДИННОГО ЗВІТУ ---
            if current_time.hour != last_heartbeat_hour:
                print(f"\n⚡ [{current_time.strftime('%H:%M:%S')}] ========== МАКСИМАЛЬНИЙ ЗВІТ СИСТЕМИ ==========")

                # 1. Фінанси
                try:
                    balance = exchange.fetch_balance()
                    usdt_free = balance.get('USDT', {}).get('free', 0.0)
                    usdt_used = balance.get('USDT', {}).get('used', 0.0)
                    usdt_total = balance.get('USDT', {}).get('total', 0.0)
                    print(f"💰 БАЛАНС USDT -> Всього: {usdt_total:.2f} | Вільно: {usdt_free:.2f} | В ордерах: {usdt_used:.2f}")
                except:
                    print("💰 БАЛАНС USDT -> Помилка отримання даних.")

                # 2. Повний зріз по кожній монеті
                print("\n📊 СТАН РИНКУ ТА ІНДИКАТОРІВ:")
                for symbol in SYMBOLS:
                    trend, last_p, ema_50_1h = check_global_trend(symbol)
                    closes_15m, volumes_15m = get_crypto_close_and_volume(symbol, TIMEFRAME_TRADE)

                    if closes_15m and volumes_15m:
                        rsi_15m = calculate_rsi(closes_15m, 14)
                        ema_12_15m = calculate_ema(closes_15m, 12)

                        last_vol = volumes_15m[-1]
                        avg_vol = sum(volumes_15m[-20:]) / 20
                        coef = calculate_dynamic_threshold(volumes_15m)
                        vol_status = f"{last_vol:.1f}/{avg_vol*coef:.1f}"

                        pos_status = "Є ПОЗИЦІЯ" if has_active_position(symbol) else "Вільна"

                        print(f"  • {symbol:<11} | Тренд 1h: {trend:<10} | Ціна: {last_p:<8.4f} | RSI 15m: {rsi_15m:.1f} | Об'єм: {vol_status} | Стан: {pos_status}")

                # 3. Активні пастки
                print("\n📦 АКТИВНІ ПАСТКИ В ПАМ'ЯТІ:")
                if active_traps:
                    for s, t in active_traps.items():
                        print(f"   • [{s}] Лімітний ордер чекає на пролив. ID: {t['order_id']}")
                else:
                    print("   • Жодних виставлених ліміток зараз немає.")

                print(f"\n💚 Статус: Скрипт працює стабільно. Наступний звіт рівно за годину.")
                print("==================================================================\n")
                last_heartbeat_hour = current_time.hour

            # --- ОСНОВНА ТОРГОВА ЛОГІКА ---
            manage_open_positions()

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

                    if global_trend == "LONG_ONLY" and rsi < 65 and closes[-1] > ema_12:
                        print(f"🕸️ [ПАСТКА LONG] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {ema_12}")
                        
                        # Розрахунок об'єму та точне округлення під WhiteBIT
                        raw_amount = BASE_POSITION_VOLUME / ema_12
                        amount = float(exchange.amount_to_precision(symbol, raw_amount))
                        price = float(exchange.price_to_precision(symbol, ema_12))
                        
                        order = exchange.create_order(symbol, 'limit', 'buy', amount, price)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx}

                    elif global_trend == "SHORT_ONLY" and rsi > 35 and closes[-1] < ema_12:
                        print(f"🕸️ [ПАСТКА SHORT] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {ema_12}")
                        
                        # Розрахунок об'єму та точне округлення під WhiteBIT
                        raw_amount = BASE_POSITION_VOLUME / ema_12
                        amount = float(exchange.amount_to_precision(symbol, raw_amount))
                        price = float(exchange.price_to_precision(symbol, ema_12))
                        
                        order = exchange.create_order(symbol, 'limit', 'sell', amount, price)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx}

            time.sleep(30)

        except Exception as e:
            print(f"🚨 Помилка в циклі: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()
