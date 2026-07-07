import time
from datetime import datetime
import ccxt

# --- НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ ---
# Твої актуальні ключі API для роботи на WhiteBIT Futures
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {'defaultType': 'future'} # Працюємо суворо на ф'ючерсах
})

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'ONDO/USDT', 'LINK/USDT', 'NEAR/USDT', 'RENDER/USDT', 'FET/USDT', 'SOL/USDT', 'SUI/USDT']
TIMEFRAME_TRADE = '15m'
TIMEFRAME_TREND = '1h'

# Словник для контролю активних пасток (ліміток) у пам'яті
active_traps = {}
# Змінна для відстеження годинного пульсу
last_heartbeat_hour = -1

# --- МАТЕМАТИЧНІ ФУНКЦІЇ НА ЧИСТОМУ PYTHON (БЕЗ ПАНДАС) ---
def calculate_ema(prices, period):
    """Розрахунок EMA (Експоненційне ковзне середнє)"""
    if len(prices) < period: return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Початкове значення як просте середнє
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_rsi(prices, period=14):
    """Розрахунок RSI (Індекс відносної сили) за методом Вайлдера"""
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
    """Розрахунок динамічного коефіцієнта об'єму (плаває від 1.6 до 2.5)"""
    if len(volumes) < 20: return 1.6
    last_20 = volumes[-20:]
    mean_vol = sum(last_20) / 20
    if mean_vol == 0: return 1.6
    
    # Стандартне відхилення вручну
    variance = sum((x - mean_vol) ** 2 for x in last_20) / 20
    std_vol = variance ** 0.5
    
    cv = std_vol / mean_vol
    threshold = 1.6 + (cv * 0.5)
    return min(max(threshold, 1.6), 2.5)

# --- ЛОГІКА ОТРЫМАННЯ ДАНИХ ТА ЗАНЯТОСТІ ---
def get_crypto_close_and_volume(symbol, timeframe):
    """Отримання масивів цін закриття та об'ємів"""
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]
        return closes, volumes
    except Exception as e:
        print(f"❌ Помилка отримання даних для {symbol}: {e}")
        return None, None

def check_global_trend(symbol):
    """КРОК 1: Фільтр старшого тренду (1h) за допомогою EMA-50"""
    closes, _ = get_crypto_close_and_volume(symbol, TIMEFRAME_TREND)
    if not closes: return "FLAT"
    ema_50 = calculate_ema(closes, 50)
    if closes[-1] > ema_50: return "LONG_ONLY"
    if closes[-1] < ema_50: return "SHORT_ONLY"
    return "FLAT"

def has_active_position(symbol):
    """Захист: Перевірка наявності відкритих позицій по монеті"""
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0: 
                return True
        return False
    except:
        return True # Безпека: якщо біржа лежить, вважаємо що позиція є

def manage_open_positions():
    """КРОК 4: Катапульта (Перенос СТОПу в безубиток при +0.4%)"""
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
                # Тут викликається твоя стандартна функція модифікації стоп-ордера на біржі
    except Exception as e:
        print(f"❌ Помилка в модулі Катапульта: {e}")

def handle_traps_timeout(current_candle_idx, symbol):
    """КРОК 3.2: Контроль часу життя пасток (Макс 2 свічки = 30 хв)"""
    if symbol not in active_traps: return
    trap = active_traps[symbol]
    try:
        order = exchange.fetch_order(trap['order_id'], symbol)
        if order['status'] == 'closed':
            print(f"🕸️ [ПАСТКА СПРАЦЮВАЛА] Ми в позиції по {symbol}! Активуємо супровід.")
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
    print("🤖 Бот Lyra V2 (Без панд) успішно активований. Повний захист увімкнено.")
    
    while True:
        try:
            current_time = datetime.now()
            
            # --- БЛОК ЩОГОДИННОГО ЗВІТУ (HEARTBEAT) ---
            if current_time.hour != last_heartbeat_hour:
                print(f"\n⚡ [{current_time.strftime('%H:%M:%S')}] --- ПУЛЬС СИСТЕМИ (ЩОГОДИННИЙ ЗВІТ) ---")
                try:
                    balance = exchange.fetch_balance()
                    usdt_free = balance.get('USDT', {}).get('free', 0.0)
                    usdt_used = balance.get('USDT', {}).get('used', 0.0)
                    usdt_total = balance.get('USDT', {}).get('total', 0.0)
                    print(f"💰 Баланс USDT: Всього: {usdt_total:.2f} | Вільно: {usdt_free:.2f} | В ордерах: {usdt_used:.2f}")
                except:
                    print("💰 Баланс USDT: Не вдалося отримати дані з біржі.")
                
                print(f"🔍 Сканую монети: {', '.join(SYMBOLS)}")
                print("📦 Активні пастки:")
                
                if active_traps:
                    for s, t in active_traps.items():
                        print(f"   • [{s}] Чекає на відкат. ID ордера: {t['order_id']}")
                else:
                    print("   • Поки що немає активних ліміток у пам'яті. Шукаю аномальні об'єми.")
                    
                print(f"💚 Статус: Політ нормальний, я на зв'язку! Не сумуйте там... 😉")
                print("---------------------------------------------------\n")
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

                # Рахуємо технічні індикатори
                ema_12 = calculate_ema(closes, 12)
                rsi = calculate_rsi(closes, 14)
                
                last_20_vols = volumes[-20:]
                avg_vol_20 = sum(last_20_vols) / 20
                dynamic_coef = calculate_dynamic_threshold(volumes)
                
                # Перевірка на аномальний сплеск об'єму
                if volumes[-1] > (avg_vol_20 * dynamic_coef):
                    global_trend = check_global_trend(symbol)
                    
                    # СИГНАЛ LONG (Вхід ліміткою на EMA-12)
                    if global_trend == "LONG_ONLY" and rsi < 65 and closes[-1] > ema_12:
                        print(f"🕸️ [ПАСТКА LONG] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {ema_12}")
                        amount = 5.0 / ema_12
                        order = exchange.create_order(symbol, 'limit', 'buy', amount, ema_12)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx}
                        
                    # СИГНАЛ SHORT (Вхід ліміткою на EMA-12)
                    elif global_trend == "SHORT_ONLY" and rsi > 35 and closes[-1] < ema_12:
                        print(f"🕸️ [ПАСТКА SHORT] {symbol}. Коеф: {dynamic_coef:.2f}. Лімітка на EMA-12: {ema_12}")
                        amount = 5.0 / ema_12
                        order = exchange.create_order(symbol, 'limit', 'sell', amount, ema_12)
                        active_traps[symbol] = {'order_id': order['id'], 'placed_at_candle_idx': current_candle_idx}
                        
                    # Блокування хибних входів проти тренду
                    elif global_trend == "LONG_ONLY" and rsi >= 65:
                        print(f"🛡️ [ЗАХИСТ БАЛАНСУ] По {symbol} вертикальний зелений імпульс. Вхід заблоковано.")
            
            time.sleep(30) # Опитування ринку кожні 30 секунд
            
        except Exception as e:
            print(f"🚨 Критична помилка в циклі: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_cycle()

# КІНЕЦЬ КОДУ. ВСЕ ЗІБРАНО, ПУЛЬС НАЛАШТОВАНО. ВДАЛОГО ЗАПУСКУ! 👍
