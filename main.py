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

# Коефіцієнт активації безубитку (0.4 = 40% від цілі Take Profit)
BREAKEVEN_TRIGGER_PCT = 0.4  

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

    # ==================== 2. ПЕРЕВІРКА ПОЗИЦІЙ ТА АВТО-ПІДЧИСТКА ====================
    print("\n--- 🔍 [КРОК 2] ЗАПИТИ ПОЗИЦІЙ ТА АВТО-ПІДЧИСТКА ---")
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

                # Зберігаємо позицію
                real_positions[symbol] = pos

                # ======= ДИНАМІЧНИЙ МОДУЛЬ БЕЗЗБИТКОВОСТІ (BREAKEVEN) =======
                target_pnl_to_activate = INVEST_PER_TRADE * LEVERAGE * TAKE_PROFIT_PCT * BREAKEVEN_TRIGGER_PCT

                if unrealized_pnl >= target_pnl_to_activate:
                    print(f"         🚀 [БЕЗЗБИТОК] {symbol} дав гарний профіт ({unrealized_pnl:.2f} USDT). Перевірка переносу SL...")

                    try:
                        open_orders = exchange.fetch_open_orders(symbol)
                        has_breakeven_sl = False
                        old_sl_id = None

                        # Шукаємо поточні стоп-ордери
                        for order in open_orders:
                            o_price = float(order.get('stopPrice') or order.get('info', {}).get('stopPrice', 0))
                            if o_price > 0:
                                is_long = p_size > 0
                                if abs(o_price - entry_price) / entry_price < 0.001:
                                    has_breakeven_sl = True
                                elif (is_long and o_price < entry_price) or (not is_long and o_price > entry_price):
                                    old_sl_id = order['id']

                        if has_breakeven_sl:
                            print(f"         🔒 Позиція {symbol} вже надійно захищена в 0. Пропуск.")
                        else:
                            if old_sl_id:
                                exchange.cancel_order(old_sl_id, symbol)
                                print(f"         ✅ Старий Stop Loss (ID: {old_sl_id}) скасовано.")
                                time.sleep(0.2)

                            sl_side = 'sell' if p_size > 0 else 'buy'
                            precise_entry_price = float(exchange.price_to_precision(symbol, entry_price))

                            exchange.create_order(
                                symbol=symbol,
                                type='market',
                                side=sl_side,
                                amount=abs(float(exchange.amount_to_precision(symbol, p_size))),
                                params={'stopPrice': precise_entry_price}
                            )
                            print(f"         🔥 Новий безубитковий Stop Loss виставлено на рівень: {precise_entry_price}")

                    except Exception as e_be:
                        print(f"         ❌ Помилка в модулі безубитку для {symbol}: {e_be}")

        # АВТОМАТИЧНА ПІДЧИСТКА ОРДЕРІВ-СИРІТ (ДВІРНИК)
        print("   🧹 Перевірка та очищення залишкових ордерів (якщо позицію закрито)...")
        for pair in SCAN_MARKETS:
            if pair not in real_positions:
                try:
                    open_orders = exchange.fetch_open_orders(pair)
                    if open_orders:
                        print(f"      ⚠️ Виявлено {len(open_orders)} залишкові ордери по {pair} без активної позиції. Скасування...")
                        for order in open_orders:
                            exchange.cancel_order(order['id'], pair)
                            print(f"         ✅ Ордер-привид ID {order['id']} успешно скасовано.")
                except Exception:
                    pass

    except Exception as e:
        print(f"   ❌ Помилка запиту позицій або підчистки API: {e}")

    if len(real_positions) == 0:
        print("   ℹ️ [ЛОГ] Жодних відкритих позицій на WhiteBIT не виявлено.")
    else:
        print(f"📊 РЕЗУЛЬТАТ: Всього унікальних монет зафіксовано в позиціях: {len(real_positions)}")

    # ==================== 3. СКАНУВАННЯ РИНКУ ТА ВХІД ====================
    print("\n--- 🔍 [КРОК 3] АНАЛІЗ РИНКУ (15m СВІЧКИ + ФІЛЬТРИ EMA/RSI) ---")
    no_anomaly_pairs = []

    for pair in SCAN_MARKETS:
        time.sleep(0.2)

        if pair in real_positions:
            print(f"   🔒 {pair}: Аналіз входу пропущено. По цій монеті ВЖЕ є відкрита позиція.")
            continue

        try:
            # Збільшено ліміт до 150 свічок для точного розрахунку довгої EMA
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=150)
            if not candles or len(candles) < 120:
                print(f"   ⚠️ {pair}: Недостатньо історичних свічок ({len(candles) if candles else 0}/120). Пропуск.")
                continue

            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]

            current_price = closes[-1]
            c_open = float(candles[-2][1])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            # --- ФУНКЦІЯ РОЗРАХУНКУ EMA ---
            def calculate_ema(prices, period):
                ema = [prices[0]]
                k = 2 / (period + 1)
                for price in prices[1:]:
                    ema.append(price * k + ema[-1] * (1 - k))
                return ema

            # --- ФУНКЦІЯ РОЗРАХУНКУ RSI ---
            def calculate_rsi(prices, period=14):
                gains = []
                losses = []
                for i in range(1, len(prices)):
                    diff = prices[i] - prices[i-1]
                    if diff > 0:
                        gains.append(diff)
                        losses.append(0)
                    else:
                        gains.append(0)
                        losses.append(abs(diff))
                
                avg_gain = sum(gains[:period]) / period
                avg_loss = sum(losses[:period]) / period
                
                if avg_loss == 0:
                    return 100
                
                for i in range(period, len(gains)):
                    avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                    avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                
                if avg_loss == 0:
                    return 100
                rs = avg_gain / avg_loss
                return 100 - (100 / (1 + rs))

            # Рахуємо індикатори на момент закриття останньої свічки (індекс -2)
            ema12 = calculate_ema(closes[:-1], 12)[-1]
            ema26 = calculate_ema(closes[:-1], 26)[-1]
            ema100 = calculate_ema(closes[:-1], 100)[-1]
            rsi14 = calculate_rsi(closes[:-1], 14)

            # 1. Фільтр перегріву свічки
            candle_change = abs(c_close - c_open) / c_open
            if candle_change > MAX_CANDLE_PCT:
                print(f"   ⚠️ {pair}: Імпульс занадто великий ({candle_change*100:.2f}% > {MAX_CANDLE_PCT*100}%). Ризик розвороту, пропуск.")
                continue

            # 2. Перевірка аномального об'єму
            past_volumes = volumes[:-2]
            avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0
            required_vol = avg_volume * VOLUME_MULTIPLIER

            if c_vol < required_vol:
                no_anomaly_pairs.append(pair)
                continue

            # Визначаємо напрямок за закриттям свічки
            candle_direction = "LONG" if c_close > c_open else "SHORT"

            # 3. ФІЛЬТРАЦІЯ ЗА EMA ТА RSI (Захист від розпилу)
            if candle_direction == "LONG":
                # Умови для LONG: ціна вище за EMA-100, швидка EMA вище середньої, RSI не перегрітий (< 65)
                if c_close > ema100 and ema12 > ema26 and rsi14 < 65:
                    pass
                else:
                    print(f"   ❌ {pair}: Об'єм виявлено, але LONG скасовано індикаторами. RSI: {rsi14:.1f}, Ціна: {c_close:.4f} (EMA100: {ema100:.4f})")
                    continue

            elif candle_direction == "SHORT":
                # Умови для SHORT: ціна нижче за EMA-100, швидка EMA нижче середньої, RSI не на дні (> 35)
                if c_close < ema100 and ema12 < ema26 and rsi14 > 35:
                    pass
                else:
                    print(f"   ❌ {pair}: Об'єм виявлено, але SHORT скасовано індикаторами. RSI: {rsi14:.1f}, Ціна: {c_close:.4f} (EMA100: {ema100:.4f})")
                    continue

            # 4. Перевірка балансу
            if free_balance < INVEST_PER_TRADE:
                print(f"   🎯 [СИГНАЛ] {pair}: Сигнал підтверджено! Але ВХІД ПРОПУЩЕНО через брак балансу ({free_balance:.2f} USDT)")
                continue

            side = 'buy' if candle_direction == "LONG" else 'sell'
            print(f"   🚀 [ВХІД] {pair} -> Напрямок: {candle_direction} | Об'єм: {c_vol:.1f} | RSI: {rsi14:.1f}")

            # 5. Розрахунок об'єму з урахуванням лімітів
            amount_usdt = INVEST_PER_TRADE * LEVERAGE
            amount_contracts = amount_usdt / current_price

            market_info = exchange.market(pair)
            min_qty = safe_float(market_info['limits']['amount']['min'], 0.001)

            if amount_contracts < min_qty:
                amount_contracts = min_qty

            precise_amount = exchange.amount_to_precision(pair, amount_contracts)

            # 6. Вхід у позицію
            try:
                print(f"      Надсилання маркет-ордера: {side.upper()} {precise_amount} по {pair}...")
                order = exchange.create_order(
                    symbol=pair,
                    type='market',
                    side=side,
                    amount=float(precise_amount)
                )
                print(f"      🔥 Вхід успішний! ID ордера: {order.get('id')}")
                free_balance -= INVEST_PER_TRADE

                time.sleep(0.5)

                # Початкові Stop Loss та Take Profit
                sl_side = 'sell' if side == 'buy' else 'buy'
                tp_side = sl_side

                if candle_direction == "LONG":
                    sl_price = current_price * (1 - STOP_LOSS_PCT)
                    tp_price = current_price * (1 + TAKE_PROFIT_PCT)
                else:
                    sl_price = current_price * (1 + STOP_LOSS_PCT)
                    tp_price = current_price * (1 - TAKE_PROFIT_PCT)

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
    print("🤖 Бот запущений у режимі автоматичного контролю ризиків та підчистки ордерів")
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
