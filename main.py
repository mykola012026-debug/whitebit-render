import ccxt
import os
import time
from datetime import datetime

# --- НАЛАШТУВАННЯ СКАНЕРА & РИЗИКІВ (РЕЖИМ СНАЙПЕРА) ---
SCAN_MARKETS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "FET/USDT:USDT", 
    "ONDO/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT", "RENDER/USDT:USDT", "LINK/USDT:USDT"
]

TAKE_PROFIT_PCT = 0.05      # 5% руху ціни для тейку
STOP_LOSS_PCT = 0.035       # 3.5% руху ціни для стопу
VOLUME_MULTIPLIER = 2.2     # Вхід, якщо об'єм > норми в 2.2 рази
ANOMALY_COEF = 2.5          # Заборона входу, якщо свічка > норми в 2.5 рази
INVEST_PER_TRADE = 5.5      # Чиста інвестиція в одну угоду (USDT)
LEVERAGE = 3                # Кредитне плече

# --- ІНІЦІАЛІЗАЦІЯ API (WhiteBIT під твій Суб-рахунок без collateral) ---
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'  # Стандарт CCXT для безстрокових ф'ючерсів WhiteBIT
    }
})

def clean_symbol_name(symbol):
    """Приводить назву пари з біржі до чистого вигляду (наприклад, BTC)."""
    if not symbol: return ""
    return symbol.replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ЦИКЛУ СКАНИРУВАННЯ ---")
    
    # 1. БЕЗПЕЧНЕ ОТРИМАННЯ БАЛАНСУ
    free_balance = INVEST_PER_TRADE  # Дефолтний допуск на випадок помилки API
    try:
        balances = exchange.fetch_balance()
        free_balance = float(balances['free'].get('USDT', 0.0))
        print(f"💰 Вільний баланс з біржі: {free_balance:.2f} USDT")
    except Exception as e:
        print(f"⚠️ [ПОМИЛКА БАЛАНСУ] Не вдалося зчитати баланс: {e}")
        print(f"ℹ️ Продовжуємо роботу з дефолтним допуском: {free_balance} USDT")

    # 2. БЕЗПЕЧНЕ ОТРИМАННЯ АКТИВНИХ ПОЗИЦІЙ
    real_positions = {}
    try:
        # Для режиму swap використовуємо стандартний fetch_positions
        positions_raw = exchange.fetch_positions()
        if not positions_raw:
            # Спроба з параметрами, якщо біржа вимагає явного типу
            positions_raw = exchange.fetch_positions(params={'type': 'swap'})
            
        for pos in positions_raw:
            p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            # Мікро-допуск (> 0.000001) щоб бачити навіть лоти 0.01 на тестах
            if abs(p_size) > 0.000001:
                clean_name = clean_symbol_name(pos.get('symbol'))
                real_positions[clean_name] = pos
                
        print(f"📊 Активних позицій на біржі: {len(real_positions)}")
        if real_positions:
            for name, p in real_positions.items():
                print(f"   • {name} | {p.get('side').upper()} | Ціна входу: {p.get('entryPrice')} | Об'єм: {p.get('contracts') or p.get('size')}")
    except Exception as e:
        print(f"⚠️ [ПОМИЛКА ПОЗИЦІЙ] Не вдалося отримати список позицій: {e}")
        print("ℹ️ Продовжуємо цикл аналізу без врахування відкритих ззовні позицій.")

    # 3. АНАЛІЗ РИНКУ (КОЖНА ПАРА ІЗОЛЬОВАНА ВІД ПОМИЛОК ІНШИХ)
    for pair in SCAN_MARKETS:
        time.sleep(0.05)  # Захист від Rate Limit
        clean_pair = clean_symbol_name(pair)
        has_position = clean_pair in real_positions

        try:
            # Завантажуємо свічки для поточної пари
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98: 
                continue

            current_price = float(candles[-1][4])  # Ціна в моменті
            
            # --- БЛОК ПРОДАЖУ (МОНІТОРИНГ) ---
            if has_position:
                pos = real_positions[clean_pair]
                p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                direction = "LONG" if (p_size > 0 or pos.get('side') == 'long') else "SHORT"
                entry_price = float(pos.get('entryPrice', 0))

                if entry_price == 0: 
                    print(f"⚠️ {pair}: Біржа повернула нульову ціну входу. Пропускаємо моніторинг.")
                    continue

                # Розрахунок тригерів
                tp_price = entry_price * (1 + TAKE_PROFIT_PCT if direction == "LONG" else 1 - TAKE_PROFIT_PCT)
                sl_price = entry_price * (1 - STOP_LOSS_PCT if direction == "LONG" else 1 + STOP_LOSS_PCT)

                should_close = False
                reason = ""
                if direction == "LONG":
                    if current_price >= tp_price: should_close, reason = True, "TAKE_PROFIT 🟢"
                    elif current_price <= sl_price: should_close, reason = True, "STOP_LOSS 🔴"
                else:
                    if current_price <= tp_price: should_close, reason = True, "TAKE_PROFIT 🟢"
                    elif current_price >= sl_price: should_close, reason = True, "STOP_LOSS 🔴"

                if should_close:
                    print(f"🚨 [СИГНАЛ ЗАКРИТТЯ] {pair} досяг {reason} (Ціна: {current_price})")
                    close_side = 'sell' if direction == "LONG" else 'buy'
                    try:
                        exchange.create_order(pair, 'market', close_side, exchange.amount_to_precision(pair, abs(p_size)))
                        print(f"🏁 Позицію по {pair} успішно закрито.")
                    except Exception as order_close_err:
                        print(f"❌ Не вдалося виконати ордер закриття для {pair}: {order_close_err}")
                
                continue  # Переходимо до наступної пари, вхід по цій не шукаємо

            # --- БЛОК КУПІВЛІ (ПОШУК СИГНАЛІВ) ---
            c_open, c_high, c_low, c_close, c_vol = [float(candles[-2][i]) for i in range(1, 6)]
            
            past_candles = candles[:-2]
            avg_volume_24h = sum(float(c[5]) for c in past_candles) / len(past_candles)
            avg_atr_24h = sum(abs(float(c[2]) - float(c[3])) for c in past_candles) / len(past_candles)
            confirmed_spread = abs(c_high - c_low)

            volume_spike = c_vol >= (avg_volume_24h * VOLUME_MULTIPLIER)
            overextended = confirmed_spread > (avg_atr_24h * ANOMALY_COEF)

            if volume_spike and not overextended and free_balance >= INVEST_PER_TRADE:
                trade_direction = "LONG" if c_close > c_open else "SHORT"
                side = 'buy' if trade_direction == "LONG" else 'sell'
                
                print(f"🎯 [СИГНАЛ НА ВХІД] Аномалія на {pair} -> Вхід у {trade_direction}")
                
                amount_to_buy = (INVEST_PER_TRADE * LEVERAGE) / current_price
                
                # Перевірка мінімальних лімітів пари
                try:
                    market_info = exchange.market(pair)
                    min_amount = market_info['limits']['amount']['min']
                    if amount_to_buy < min_amount: amount_to_buy = min_amount
                except:
                    pass

                # Виставляємо плече та шлемо ордер
                try: 
                    exchange.set_leverage(LEVERAGE, pair)
                except: 
                    pass
                
                try:
                    exchange.create_order(pair, 'market', side, exchange.amount_to_precision(pair, amount_to_buy))
                    print(f"🔥 Успішний ринковий вхід по {pair} ({trade_direction})")
                except Exception as order_open_err:
                    print(f"❌ Помилка відкриття ордера для {pair}: {order_open_err}")

        except Exception as e:
            print(f"⚠️ [ПОМИЛКА ПАРИ] Збій обробки ринку {pair}: {e}")
            continue

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


# --- ГОЛОВНИЙ ТАЙМЕР (СПАТИ ДО СВІЧКИ) ---
if __name__ == "__main__":
    print("🤖 Бот-Снайпер ініціалізований. Працює автономно по API (режим Swap).")
    try: 
        exchange.load_markets()
        print("✅ Специфікації ринків завантажені.")
    except Exception as e: 
        print(f"⚠️ Не вдалося завантажити ринки при старті: {e}")
        
    last_processed_minute = -1
    
    while True:
        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.minute != last_processed_minute:
            if now.second >= 2:  # Даємо 2 секунди на закриття свічки на серверах біржі
                last_processed_minute = now.minute
                try: 
                    run_scanner_cycle()
                except Exception as main_crit_e: 
                    print(f"🚨 Критичний збій ядра циклу: {main_crit_e}")
                    
        if now.minute not in [0, 15, 30, 45]: 
            last_processed_minute = -1
            
        time.sleep(0.5)
