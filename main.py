import ccxt
import time
from datetime import datetime

# ==================== НАЛАШТУВАННЯ ТОРГІВЛІ ====================
SCAN_MARKETS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "FET/USDT:USDT", 
    "ONDO/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT", "RENDER/USDT:USDT", "LINK/USDT:USDT"
]

VOLUME_MULTIPLIER = 1.6
INVEST_PER_TRADE = 5.5
LEVERAGE = 10

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
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ПОВНОГО ДІАГНОСТИЧНОГО ЦИКЛУ ---")

    # ==================== 1. ПЕРЕВІРКА БАЛАНСІВ ====================
    print("\n--- 🔍 [КРОК 1] ЗАПИТИ БАЛАНСІВ ---")
    free_balance = 0.0

    # Спосіб В: Головний загальний баланс (Main) - Основний і найстабільніший
    try:
        exchange.options['accountsByType'] = {'spot': 'main'}
        bal_main = exchange.fetch_balance({'type': 'main'})
        usdt_main = bal_main.get('USDT', {}).get('free', 0.0)
        print(f"   🔹 Спосіб В (Main рахунок): {usdt_main} USDT вільних")
        if safe_float(usdt_main) > 0:
            free_balance = safe_float(usdt_main)
    except Exception as e:
        print(f"   ❌ Спосіб В видав помилку API: {e}")

    # Спосіб Б: Торговий баланс (Trade) - Резервний (Fallback)
    try:
        exchange.options['accountsByType'] = {'spot': 'trade'}
        bal_trade = exchange.fetch_balance({'type': 'spot'})
        usdt_trade = bal_trade.get('USDT', {}).get('free', 0.0)
        print(f"   🔹 Спосіб Б (Trade/Spot): {usdt_trade} USDT вільних")
        if free_balance == 0.0 and safe_float(usdt_trade) > 0:
            free_balance = safe_float(usdt_trade)
    except Exception as e:
        print(f"   ❌ Спосіб Б видав помилку API: {e}")

    print(f"📊 ПРИЙНЯТИЙ ДЛЯ РОЗРАХУНКУ ВХОДУ БАЛАНС: {free_balance:.2f} USDT")

    # ==================== 2. ПЕРЕВІРКА ПОЗИЦІЙ ====================
    print("\n--- 🔍 [КРОК 2] ЗАПИТИ ПОЗИЦІЙ ---")
    real_positions = {}

    try:
        exchange.options['accountsByType'] = {'swap': 'collateral'}
        print("   🤖 Запит активних позицій через CCXT (fetch_positions)...")
        positions = exchange.fetch_positions(SCAN_MARKETS)
        print(f"      👉 Отримано рядків від API: {len(positions)}")
        
        for pos in positions:
            # Читаємо розмір позиції з урахуванням специфіки WhiteBIT
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            symbol = pos.get('symbol', 'Невідомо')
            
            if abs(p_size) > 0.000001:
                # Визначаємо напрямок позиції (Лонг/Шорт)
                direction = "📈 LONG" if p_size > 0 else "📉 SHORT"
                
                # Витягуємо метрики ціни та PnL
                entry_price = safe_float(pos.get('entryPrice') or pos.get('info', {}).get('entryPrice'))
                current_price = safe_float(pos.get('markPrice') or pos.get('info', {}).get('basePrice'))
                unrealized_pnl = safe_float(pos.get('unrealizedPnl') or pos.get('info', {}).get('pnl'))
                
                # Красивий індикатор для PnL (зелений плюс або червоний мінус)
                pnl_marker = "🟢 +" if unrealized_pnl >= 0 else "🔴 "
                
                # Інформативний вивід у лог
                print(f"      🎯 {symbol} | {direction} | Розмір: {abs(p_size)}")
                print(f"         Вхід: {entry_price:.4f} -> Поточна: {current_price:.4f} | PnL: {pnl_marker}{unrealized_pnl:.2f} USDT")
                
                clean_name = str(symbol).replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()
                real_positions[clean_name] = pos
                
    except Exception as e:
        print(f"   ❌ Помилка запиту позицій API: {e}")

    # Фінальний звіт по позиціях
    if len(real_positions) == 0:
        print("   ℹ️ [ЛОГ] Жодних відкритих позицій на WhiteBIT не виявлено.")
    else:
        print(f"📊 РЕЗУЛЬТАТ: Всього унікальних монет зафіксовано в позиціях: {len(real_positions)}")

    # ==================== 3. СКАНУВАННЯ РИНКУ ТА ВХІД ====================
    print("\n--- 🔍 [КРОК 3] АНАЛІЗ РИНКУ (15m СВІЧКИ) ---")
    no_anomaly_pairs = []

    for pair in SCAN_MARKETS:
        time.sleep(0.2)  # Захист від лімітів API (Rate Limit)

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

            required_vol = avg_volume * VOLUME_MULTIPLIER
            if c_vol < required_vol:
                no_anomaly_pairs.append(pair)
                continue

            if free_balance < INVEST_PER_TRADE:
                print(f"   🎯 [СИГНАЛ] {pair}: Виявлено об'єм! Але ВХІД ПРОПУЩЕНО через брак балансу ({free_balance:.2f} USDT)")
                continue

            direction = "LONG" if c_close > c_open else "SHORT"
            side = 'buy' if direction == "LONG" else 'sell'
            print(f"   🎯 [СИГНАЛ] {pair} -> Напрямок: {direction} | Об'єм: {c_vol:.1f}")

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

    # Вивід спокійних пар одним компактним рядком
    if no_anomaly_pairs:
        print(f"   📊 Аномалій не виявлено по парах: {', '.join(no_anomaly_pairs)}")

    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений у режимі інфо-логування позицій")
    try:
        exchange.load_markets()
        print("✅ Специфікації ринків ф'ючерсів завантажено успішно")
    except Exception as e:
        print(f"❌ Критична помилка ініціалізації ринків: {e}")
        exit()

    last_minute = -1
    while True:
        now = datetime.now()

        # Запуск кожні 15 хвилин (на 2-й секунді хвилини)
        if now.minute % 15 == 0 and now.minute != last_minute:
            if now.second >= 2:
                last_minute = now.minute
                try:
                    run_scanner_cycle()
                except Exception as e:
                    print(f"💥 Критичний збій під час виконання циклу: {e}")
        else:
            if now.minute % 15 != 0:
                last_minute = -1

        time.sleep(0.5)
