import ccxt
import time
from datetime import datetime

# ==================== НАЛАШТУВАННЯ ТОРГІВЛІ ====================
SCAN_MARKETS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "FET/USDT:USDT", 
    "ONDO/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT", "RENDER/USDT:USDT", "LINK/USDT:USDT"
]

TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = 0.035
VOLUME_MULTIPLIER = 2.2
ANOMALY_COEF = 2.5
INVEST_PER_TRADE = 5.5
LEVERAGE = 3

# ==================== API З ПОДВІЙНИМ НАЛАШТУВАННЯМ ====================
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'  # Початковий режим для ф'ючерсів
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
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ КОМПЛЕКСНОЇ ДІАГНОСТИКИ ---")

    # ==================== БЛОК 1: ПОШУК БАЛАНСУ (3 ВАРІАНТИ) ====================
    free_balance = 0.0
    print("\n--- 🔍 ТЕСТУВАННЯ БАЛАНСІВ ---")

    # Варіант А: Чистий ф'ючерсний/колатеральний баланс
    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        bal_collateral = exchange.fetch_balance({'type': 'swap'})
        usdt_collateral = bal_collateral.get('USDT', {}).get('free', 0.0)
        print(f"💰 Спосіб А (Collateral/Swap): {usdt_collateral} USDT free")
        if usdt_collateral > 0:
            free_balance = safe_float(usdt_collateral)
    except Exception as e:
        print(f"❌ Спосіб А видав помилку: {e}")

    # Варіант Б: Торговий баланс (Trade)
    try:
        exchange.options['accountsByType'] = {'spot': 'trade'}
        bal_trade = exchange.fetch_balance({'type': 'spot'})
        usdt_trade = bal_trade.get('USDT', {}).get('free', 0.0)
        print(f"💰 Спосіб Б (Trade/Spot): {usdt_trade} USDT free")
        if free_balance == 0.0 and usdt_trade > 0:
            free_balance = safe_float(usdt_trade)
    except Exception as e:
        print(f"❌ Спосіб Б видав помилку: {e}")

    # Варіант В: Головний загальний баланс (Main)
    try:
        exchange.options['accountsByType'] = {'spot': 'main'}
        bal_main = exchange.fetch_balance({'type': 'main'})
        usdt_main = bal_main.get('USDT', {}).get('free', 0.0)
        print(f"💰 Спосіб В (Main рахунок): {usdt_main} USDT free")
        if free_balance == 0.0 and usdt_main > 0:
            free_balance = safe_float(usdt_main)
    except Exception as e:
        print(f"❌ Спосіб В видав помилку: {e}")

    print(f"📊 ПРИЙНЯТИЙ ДЛЯ ТОРГІВЛІ БАЛАНС: {free_balance:.2f} USDT")

    # ==================== БЛОК 2: ПОШУК ПОЗИЦІЙ (2 ВАРІАНТИ) ====================
    print("\n--- 🔍 ТЕСТУВАННЯ ПОЗИЦІЙ ---")
    real_positions = {}
    
    # Скидаємо опцію для роботи з деривативами
    exchange.options['accountsByType'] = {'swap': 'collateral'}

    # Варіант 1: Стандартний запит через фільтр пар
    try:
        print("🤖 Варіант позицій №1 (з фільтром пар)...")
        positions_raw = exchange.fetch_positions(SCAN_MARKETS)
        for pos in positions_raw:
            p_size = safe_float(pos.get('contracts') or pos.get('size'))
            # Перевіряємо також сирі дані від WhiteBIT всередині info
            if 'info' in pos and isinstance(pos['info'], dict):
                p_size = p_size or safe_float(pos['info'].get('size'))
            
            market_symbol = pos.get('symbol')
            if abs(p_size) > 0.000001 and market_symbol:
                real_positions[market_symbol] = pos
                print(f"   🎯 Знайдено (Вар 1): {market_symbol} | size={p_size} | {pos.get('side')}")
    except Exception as e:
        print(f"❌ Варіант позицій №1 видав помилку: {e}")

    # Варіант 2: Загальний запит без фільтрації (актуально, якщо символи у CCXT відрізняються)
    if not real_positions:
        try:
            print("🤖 Варіант позицій №2 (загальний запит без фільтра)...")
            positions_all = exchange.fetch_positions()
            for pos in positions_all:
                p_size = safe_float(pos.get('contracts') or pos.get('size') or pos.get('info', {}).get('size', 0))
                market_symbol = pos.get('symbol')
                
                # Якщо CCXT не розпарсив символ, пробуємо витягти базову назву з інфо ринку
                if not market_symbol and 'info' in pos:
                    market_symbol = pos['info'].get('marketId') or pos['info'].get('symbol')

                if abs(p_size) > 0.000001 and market_symbol:
                    # Приводимо до нашого формату для сумісності
                    for trade_pair in SCAN_MARKETS:
                        if market_symbol in trade_pair or trade_pair in market_symbol:
                            real_positions[trade_pair] = pos
                            print(f"   🎯 Знайдено (Вар 2): {trade_pair} (як {market_symbol}) | size={p_size}")
        except Exception as e:
            print(f"❌ Варіант позицій №2 видав помилку: {e}")

    print(f"📊 РЕАЛЬНО ЗНАЙДЕНО ПОЗИЦІЙ: {len(real_positions)}")

    # ==================== БЛОК 3: ТОРГОВА ЛОГІКА ====================
    print("\n--- 🔍 СКАНУВАННЯ РИНКУ ---")
    for pair in SCAN_MARKETS:
        time.sleep(0.2)
        has_position = pair in real_positions

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=100)
            if not candles or len(candles) < 98:
                continue

            current_price = float(candles[-1][4])

            if has_position:
                continue

            c_open = float(candles[-2][1])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            past_volumes = [float(c[5]) for c in candles[:-2]]
            avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0

            if c_vol >= avg_volume * VOLUME_MULTIPLIER:
                if free_balance < INVEST_PER_TRADE:
                    print(f"📉 Пропущено {pair}: баланс {free_balance:.2f} < {INVEST_PER_TRADE}")
                    continue

                direction = "LONG" if c_close > c_open else "SHORT"
                side = 'buy' if direction == "LONG" else 'sell'
                print(f"🎯 [СИГНАЛ] {pair} -> {direction} | Vol: {c_vol:.1f} (Avg: {avg_volume:.1f})")

                amount_usdt = INVEST_PER_TRADE * LEVERAGE
                amount_contracts = amount_usdt / current_price
                precise_amount = exchange.amount_to_precision(pair, amount_contracts)
                
                try:
                    print(f"🚀 Маркет-ордер: {side.upper()} {precise_amount} по {pair}")
                    order = exchange.create_order(
                        symbol=pair,
                        type='market',
                        side=side,
                        amount=float(precise_amount)
                    )
                    print(f"🔥 Вхід успішний! ID: {order.get('id')}")
                    free_balance -= INVEST_PER_TRADE
                except Exception as err:
                    print(f"❌ Помилка ордера по {pair}: {err}")

        except Exception as e:
            continue

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений")
    try:
        exchange.load_markets()
        print("✅ Ринки ф'ючерсів успішно завантажено")
    except Exception as e:
        print(f"❌ Критична помилка завантаження ринків: {e}")
        exit()

    last_minute = -1
    while True:
        now = datetime.now()
        if now.minute % 15 == 0 and now.minute != last_minute:
            if now.second >= 2:
                last_minute = now.minute
                try:
                    run_scanner_cycle()
                except Exception as e:
                    print(f"💥 Критичний збій усередині циклу: {e}")
        else:
            if now.minute % 15 != 0:
                last_minute = -1
        time.sleep(0.5)
