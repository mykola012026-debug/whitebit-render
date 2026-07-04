import ccxt
import time
from datetime import datetime

# ==================== НАЛАШТУВАННЯ ТОРГІВЛІ ====================
SCAN_MARKETS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "FET/USDT:USDT", 
    "ONDO/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT", "RENDER/USDT:USDT", "LINK/USDT:USDT"
]

VOLUME_MULTIPLIER = 2.2
INVEST_PER_TRADE = 5.5
LEVERAGE = 3

# ==================== НАЛАШТУВАННЯ API ====================
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'
    }
})

def safe_float(value, default=0.0):
    """Безпечне перетворення в float"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ПОВНОГО ДІАГНОСТИЧНОГО ЦИКЛУ ---")

    # ==================== 1. ПЕРЕВІРКА БАЛАНСІВ (3 ВАРІАНТИ) ====================
    print("\n--- 🔍 [КРОК 1] ЗАПИТИ БАЛАНСІВ ---")
    free_balance = 0.0

    # Спосіб А: Ф'ючерсний/Колатеральний рахунок
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        bal_collateral = exchange.fetch_balance({'type': 'swap'})
        usdt_collateral = bal_collateral.get('USDT', {}).get('free', 0.0)
        print(f"   🔹 Спосіб А (Collateral): {usdt_collateral} USDT вільних")
        if safe_float(usdt_collateral) > 0:
            free_balance = safe_float(usdt_collateral)
    except Exception as e:
        print(f"   ❌ Спосіб А видав помилку API: {e}")

    # Спосіб Б: Торговий баланс (Trade)
    try:
        exchange.options['accountsByType'] = {'spot': 'trade'}
        bal_trade = exchange.fetch_balance({'type': 'spot'})
        usdt_trade = bal_trade.get('USDT', {}).get('free', 0.0)
        print(f"   🔹 Спосіб Б (Trade/Spot): {usdt_trade} USDT вільних")
        if free_balance == 0.0 and safe_float(usdt_trade) > 0:
            free_balance = safe_float(usdt_trade)
    except Exception as e:
        print(f"   ❌ Спосіб Б видав помилку API: {e}")

    # Спосіб В: Головний загальний баланс (Main)
    try:
        exchange.options['accountsByType'] = {'spot': 'main'}
        bal_main = exchange.fetch_balance({'type': 'main'})
        usdt_main = bal_main.get('USDT', {}).get('free', 0.0)
        print(f"   🔹 Спосіб В (Main рахунок): {usdt_main} USDT вільних")
        if free_balance == 0.0 and safe_float(usdt_main) > 0:
            free_balance = safe_float(usdt_main)
    except Exception as e:
        print(f"   ❌ Спосіб В видав помилку API: {e}")

    print(f"📊 ПРИЙНЯТИЙ ДЛЯ РОЗРАХУНКУ ВХОДУ БАЛАНС: {free_balance:.2f} USDT")

    # ==================== 2. ПЕРЕВІРКА ПОЗИЦІЙ (3 ВАРІАНТИ) ====================
    print("\n--- 🔍 [КРОК 2] ЗАПИТИ ПОЗИЦІЙ ---")
    real_positions = {}

    # Варіант Позицій №1: Стандартний через CCXT з фільтром пар
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        print("   🤖 Варіант №1 (CCXT fetch_positions з SCAN_MARKETS)...")
        pos_v1 = exchange.fetch_positions(SCAN_MARKETS)
        print(f"      👉 Отримано рядків від API: {len(pos_v1)}")
        for pos in pos_v1:
            p_size = safe_float(pos.get('contracts') or pos.get('size'))
            symbol = pos.get('symbol', 'Невідомо')
            if abs(p_size) > 0.000001:
                print(f"      🎯 Активна позиція (Вар №1): {symbol} | size={p_size}")
                clean_name = str(symbol).replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()
                real_positions[clean_name] = pos
    except Exception as e:
        print(f"   ❌ Варіант №1 видав помилку API: {e}")

    # Варіант Позицій №2: Загальний CCXT запит без фільтрації
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        print("   🤖 Варіант №2 (CCXT fetch_positions без фільтра)...")
        pos_v2 = exchange.fetch_positions()
        print(f"      👉 Отримано рядків від API: {len(pos_v2)}")
        for pos in pos_v2:
            p_size = safe_float(pos.get('contracts') or pos.get('size') or pos.get('info', {}).get('size', 0))
            symbol = pos.get('symbol') or pos.get('info', {}).get('marketId', 'Невідомо')
            if abs(p_size) > 0.000001:
                print(f"      🎯 Активна позиція (Вар №2): {symbol} | size={p_size}")
                clean_name = str(symbol).replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()
                real_positions[clean_name] = pos
    except Exception as e:
        print(f"   ❌ Варіант №2 видав помилку API: {e}")

    # Варіант Позицій №3: Прямий ф'ючерсний ендпоінт WhiteBIT (в обхід CCXT)
    try:
        print("   🤖 Варіант №3 (Прямий privatePostCollateralAccountPositions)...")
        pos_v3 = exchange.privatePostCollateralAccountPositions()
        
        # Перевірка структури відповіді
        positions_list = []
        if isinstance(pos_v3, list):
            positions_list = pos_v3
        elif isinstance(pos_v3, dict):
            positions_list = pos_v3.get('result', []) or pos_v3.get('data', []) or list(pos_v3.values())
            if not isinstance(positions_list, list):
                positions_list = [pos_v3]

        print(f"      👉 Роспарсено рядків з сирих даних: {len(positions_list)}")
        for pos in positions_list:
            if not isinstance(pos, dict):
                continue
            p_size = safe_float(pos.get('size') or pos.get('contracts') or pos.get('amount', 0))
            market_id = pos.get('marketId') or pos.get('symbol') or 'Невідомо'
            if abs(p_size) > 0.000001:
                print(f"      🎯 Активна позиція (Вар №3): {market_id} | size={p_size}")
                clean_name = str(market_id).replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()
                real_positions[clean_name] = pos
    except Exception as e:
        print(f"   ❌ Варіант №3 видав помилку API: {e}")

    print(f"📊 РЕЗУЛЬТАТ: Всього унікальних монет зафіксовано в позиціях: {len(real_positions)}")

    # ==================== 3. СКАНУВАННЯ РИНКУ ТА ВХІД ====================
    print("\n--- 🔍 [КРОК 3] АНАЛІЗ РИНКУ (15m СВІЧКИ) ---")
    for pair in SCAN_MARKETS:
        time.sleep(0.2)
        
        clean_pair_name = pair.replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()
        has_position = clean_pair_name in real_positions

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=100)
            if not candles or len(candles) < 98:
                print(f"   ⚠️ {pair}: Недостатньо історичних свічок ({len(candles) if candles else 0}/98). Пропуск.")
                continue

            current_price = float(candles[-1][4])

            if has_position:
                print(f"   🔒 {pair}: Аналіз входу пропущено. По цій монеті ВЖЕ є відкрита позиція.")
                continue

            c_open = float(candles[-2][1])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            past_volumes = [float(c[5]) for c in candles[:-2]]
            avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0

            # Логування поточного стану об'ємів для прозорості
            required_vol = avg_volume * VOLUME_MULTIPLIER
            if c_vol < required_vol:
                print(f"   📊 {pair}: Аномалій не виявлено. Об'єм={c_vol:.1f} | Потрібно для сигналу={required_vol:.1f} (Середній={avg_volume:.1f})")
                continue

            # Якщо аномальний об'єм підтверджено
            if free_balance < INVEST_PER_TRADE:
                print(f"   🎯 [СИГНАЛ] {pair}: Виявлено аномальний об'єм! Але ВХІД ПРОПУЩЕНО через брак балансу ({free_balance:.2f} USDT)")
                continue

            direction = "LONG" if c_close > c_open else "SHORT"
            side = 'buy' if direction == "LONG" else 'sell'
            print(f"   🎯 [СИГНАЛ] {pair} -> Напрямок: {direction} | Об'єм: {c_vol:.1f} (Плече: {LEVERAGE}x)")

            amount_usdt = INVEST_PER_TRADE * LEVERAGE
            amount_contracts = amount_usdt / current_price
            precise_amount = exchange.amount_to_precision(pair, amount_contracts)
            
            try:
                print(f"      🚀 Надсилання маркет-ордера: {side.upper()} {precise_amount} по {pair}...")
                order = exchange.create_order(
                    symbol=pair,
                    type='market',
                    side=side,
                    amount=float(precise_amount)
                )
                print(f"      🔥 Вхід успішний! ID ордера: {order.get('id')}")
                free_balance -= INVEST_PER_TRADE
            except Exception as err:
                print(f"      ❌ Помилка виконання ордера: {err}")

        except Exception as e:
            print(f"   ⚠️ {pair}: Помилка обробки даних ринку: {e}")
            continue

    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений у режимі максимального логування")
    try:
        exchange.load_markets()
        print("✅ Специфікації ринків ф'ючерсів завантажено успішно")
    except Exception as e:
        print(f"❌ Критична помилка ініціалізації ринків: {e}")
        exit()

    last_minute = -1
    while True:
        now = datetime.now()
        
        # Перевірка через математичний залишок для виключення синтаксичних багів
        if now.minute % 15 == 0 and now.minute != last_minute:
            if now.second >= 2:
                last_minute = now.minute
                try:
                    run_scanner_cycle()
                except Exception as e:
                    print(f"💥 Критичний збій під час виконання циклу: {e}")
        else:
if now.minute % 15 != 0:last_minute = -1time.sleep(0.5)