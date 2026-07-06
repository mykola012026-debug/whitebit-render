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

# Налаштування ризик-менеджменту (у відсотках від ціни актива)
STOP_LOSS_PCT = 0.012   # 1.2% (при 10х плечі = -12% від маржі)
TAKE_PROFIT_PCT = 0.025  # 2.5% (при 10х плечі = +25% від маржі)
MAX_CANDLE_PCT = 0.025   # 2.5% фільтр перегріву (ігноруємо занадто гігантські свічки)

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

    try:
        exchange.options['accountsByType'] = {'spot': 'main'}
        bal_main = exchange.fetch_balance({'type': 'main'})
        usdt_main = bal_main.get('USDT', {}).get('free', 0.0)
        if safe_float(usdt_main) > 0:
            free_balance = safe_float(usdt_main)
            print(f"   🔹 Отримано баланс (Main): {free_balance:.2f} USDT")
    except Exception:
        pass

    if free_balance == 0.0:
        try:
            exchange.options['accountsByType'] = {'spot': 'trade'}
            bal_trade = exchange.fetch_balance({'type': 'spot'})
            usdt_trade = bal_trade.get('USDT', {}).get('free', 0.0)
            if safe_float(usdt_trade) > 0:
                free_balance = safe_float(usdt_trade)
                print(f"   🔹 Отримано баланс (Trade/Spot): {free_balance:.2f} USDT")
        except Exception:
            pass

    if free_balance == 0.0:
        print("   ⚠️ [ПОПЕРЕДЖЕННЯ] Не вдалося отримати актуальний баланс. Використовуємо 0.0")
    else:
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
            p_size = safe_float(pos.get('contracts') or pos.get('info', {}).get('amount'))
            symbol = pos.get('symbol')

            if symbol and abs(p_size) > 0.000001:
                direction = "📈 LONG" if p_size > 0 else "📉 SHORT"
                entry_price = safe_float(pos.get('entryPrice') or pos.get('info', {}).get('entryPrice'))
                current_price = safe_float(pos.get('markPrice') or pos.get('info', {}).get('basePrice'))
                unrealized_pnl = safe_float(pos.get('unrealizedPnl') or pos.get('info', {}).get('pnl'))

                pnl_marker = "🟢 +" if unrealized_pnl >= 0 else "🔴 "
                print(f"      🎯 {symbol} | {direction} | Розмір: {abs(p_size)}")
                print(f"         Вхід: {entry_price:.4f} -> Поточна: {current_price:.4f} | PnL: {pnl_marker}{unrealized_pnl:.2f} USDT")

                # Зберігаємо позицію за її точним системним ім'ям CCXT
                real_positions[symbol] = pos

    except Exception as e:
        print(f"   ❌ Помилка запиту позицій API: {e}")

    if len(real_positions) == 0:
        print("   ℹ️ [ЛОГ] Жодних відкритих позицій на WhiteBIT не виявлено.")
    else:
        print(f"📊 РЕЗУЛЬТАТ: Всього унікальних монет зафіксовано в позиціях: {len(real_positions)}")

    # ==================== 3. СКАНУВАННЯ РИНКУ ТА ВХІД ====================
    print("\n--- 🔍 [КРОК 3] АНАЛІЗ РИНКУ (15m СВІЧКИ) ---")
    no_anomaly_pairs = []

    for pair in SCAN_MARKETS:
        time.sleep(0.2)

        # Пряма та надійна перевірка наявності позиції
        if pair in real_positions:
            print(f"   🔒 {pair}: Аналіз входу пропущено. По цій монеті ВЖЕ є відкрита позиція.")
            continue

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=100)
            if not candles or len(candles) < 98:
                print(f"   ⚠️ {pair}: Недостатньо історичних свічок ({len(candles) if candles else 0}/98). Пропуск.")
                continue

            current_price = float(candles[-1][4])
            c_open = float(candles[-2][1])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            # 1. Фільтр перегріву свічки (захист від покупок на хаях)
            candle_change = abs(c_close - c_open) / c_open
            if candle_change > MAX_CANDLE_PCT:
                print(f"   ⚠️ {pair}: Імпульс занадто великий ({candle_change*100:.2f}% > {MAX_CANDLE_PCT*100}%). Ризик розвороту, пропуск.")
                continue

            # 2. Перевірка аномального об'єму
            past_volumes = [float(c[5]) for c in candles[:-2]]
            avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0
            required_vol = avg_volume * VOLUME_MULTIPLIER

            if c_vol < required_vol:
                no_anomaly_pairs.append(pair)
                continue

            # 3. Перевірка балансу
            if free_balance < INVEST_PER_TRADE:
                print(f"   🎯 [СИГНАЛ] {pair}: Виявлено об'єм! Але ВХІД ПРОПУЩЕНО через брак балансу ({free_balance:.2f} USDT)")
                continue

            direction = "LONG" if c_close > c_open else "SHORT"
            side = 'buy' if direction == "LONG" else 'sell'
            print(f"   🎯 [СИГНАЛ] {pair} -> Напрямок: {direction} | Об'єм: {c_vol:.1f}")

            # 4. Розрахунок об'єму з урахуванням мінімальних лімітів біржі (Фікс для BTC)
            amount_usdt = INVEST_PER_TRADE * LEVERAGE
            amount_contracts = amount_usdt / current_price

            market_info = exchange.market(pair)
            min_qty = safe_float(market_info['limits']['amount']['min'], 0.001)

            if amount_contracts < min_qty:
                print(f"   ℹ️ {pair}: Розрахований об'єм {amount_contracts:.6f} менший за мінімальний лот {min_qty}. Округляємо до {min_qty}")
                amount_contracts = min_qty

            precise_amount = exchange.amount_to_precision(pair, amount_contracts)

            # 5. Вхід у позицію
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
                
                # Захист від лімітів перед надсиланням стопів
                time.sleep(0.5)

                # 6. Виставлення захисних ордерів (Stop Loss та Take Profit)
                # Для WhiteBIT використовуємо тип 'stop_market' або 'market' із параметром stopPrice
                sl_side = 'sell' if side == 'buy' else 'buy'
                tp_side = sl_side

                if direction == "LONG":
                    sl_price = current_price * (1 - STOP_LOSS_PCT)
                    tp_price = current_price * (1 + TAKE_PROFIT_PCT)
                else:
                    sl_price = current_price * (1 + STOP_LOSS_PCT)
                    tp_price = current_price * (1 - TAKE_PROFIT_PCT)

                # Округлюємо ціни стопів під правила біржі
                precise_sl_price = float(exchange.price_to_precision(pair, sl_price))
                precise_tp_price = float(exchange.price_to_precision(pair, tp_price))

                # Відправляємо Stop Loss
                try:
                    exchange.create_order(
                        symbol=pair,
                        type='market',
                        side=sl_side,
                        amount=float(precise_amount),
                        params={'stopPrice': precise_sl_price}
                    )
                    print(f"      🛑 Stop Loss виставлено на рівень: {precise_sl_price}")
                except Exception as e_sl:
                    print(f"      ❌ Не вдалося виставити Stop Loss: {e_sl}")

                # Відправляємо Take Profit
                try:
                    exchange.create_order(
                        symbol=pair,
                        type='market',
                        side=tp_side,
                        amount=float(precise_amount),
                        params={'stopPrice': precise_tp_price}
                    )
                    print(f"      🟢 Take Profit виставлено на рівень: {precise_tp_price}")
                except Exception as e_tp:
                    print(f"      ❌ Не вдалося виставити Take Profit: {e_tp}")

            except Exception as err:
                print(f"      ❌ Помилка виконання вхідного ордера: {err}")

        except Exception as e:
            print(f"   ⚠️ {pair}: Помилка обробки даних ринку: {e}")
            continue

    if no_anomaly_pairs:
        print(f"   📊 Аномалій не виявлено по парах: {', '.join(no_anomaly_pairs)}")

    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений у режимі автоматичного контролю ризиків (SL/TP)")
    try:
        exchange.load_markets()
        print("✅ Специфікації ринків ф'ючерсів завантажено успішно")
    except Exception as e:
        print(f"❌ Критична помилка ініціалізації ринків: {e}")
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
                    print(f"💥 Критичний збій під час виконання циклу: {e}")
        else:
            if now.minute % 15 != 0:
                last_minute = -1

        time.sleep(0.5)
