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

# --- ІНІЦІАЛІЗАЦІЯ API (WhiteBIT) ---
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'futures', 
        'account-level': 'collateral'
    }
})

def clean_symbol_name(symbol):
    """Приводить назву пари з біржі до чистого вигляду (наприклад, BTC)."""
    if not symbol: return ""
    return symbol.replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ЦИКЛУ СКАНИРУВАННЯ ---")
    
    # 1. ОТРИМУЄМО АКТУАЛЬНИЙ БАЛАНС З БІРЖІ
    try:
        balances = exchange.fetch_balance()
        free_balance = float(balances['free'].get('USDT', 0.0))
    except Exception as e:
        print(f"❌ Помилка отримання балансу з біржі: {e}")
        return

    # 2. ОТРИМУЄМО АКТИВНІ ПОЗИЦІЇ З БІРЖІ В МОМЕНТІ
    real_positions = {}
    try:
        positions_raw = exchange.fetch_positions(params={'type': 'futures'}) or exchange.fetch_positions(params={'type': 'swap'})
        for pos in positions_raw:
            p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            # Якщо розмір позиції не нульовий — вона активна
            if abs(p_size) > 0:
                clean_name = clean_symbol_name(pos.get('symbol'))
                real_positions[clean_name] = pos
    except Exception as e:
        print(f"❌ Помилка отримання позицій з біржі: {e}")
        return

    # Вивід поточного стану суб-рахунку в консоль
    print(f"💰 Вільний баланс: {free_balance:.2f} USDT | Активних позицій: {len(real_positions)}")
    if real_positions:
        print("📊 Поточні позиції на біржі:")
        for name, p in real_positions.items():
            print(f"   • {name} | {p.get('side').upper()} | Ціна входу: {p.get('entryPrice')} | Об'єм: {p.get('contracts') or p.get('size')}")

    # 3. АНАЛІЗ РИНКУ, МОНІТОРИНГ ТА СИГНАЛИ
    for pair in SCAN_MARKETS:
        time.sleep(0.05)  # Захист від Rate Limit (лімітів запитів API)
        clean_pair = clean_symbol_name(pair)
        has_position = clean_pair in real_positions

        try:
            # Завантажуємо 15-хвилинні свічки
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98: continue

            current_price = float(candles[-1][4])  # Ціна останнього тікера (поточна в моменті)
            
            # --- БЛОК ПРОДАЖУ (МОНІТОРИНГ ІСНУЮЧОЇ УГОДИ) ---
            if has_position:
                pos = real_positions[clean_pair]
                p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                direction = "LONG" if (p_size > 0 or pos.get('side') == 'long') else "SHORT"
                entry_price = float(pos.get('entryPrice', 0))

                if entry_price == 0: continue  # Якщо ціна входу чомусь пуста, пропускаємо від гріха

                # Розраховуємо математичні цілі
                tp_price = entry_price * (1 + TAKE_PROFIT_PCT if direction == "LONG" else 1 - TAKE_PROFIT_PCT)
                sl_price = entry_price * (1 - STOP_LOSS_PCT if direction == "LONG" else 1 + STOP_LOSS_PCT)

                # Перевірка виходу за ціною "в моменті"
                should_close = False
                reason = ""
                if direction == "LONG":
                    if current_price >= tp_price: should_close, reason = True, "TAKE_PROFIT 🟢"
                    elif current_price <= sl_price: should_close, reason = True, "STOP_LOSS 🔴"
                else:
                    if current_price <= tp_price: should_close, reason = True, "TAKE_PROFIT 🟢"
                    elif current_price >= sl_price: should_close, reason = True, "STOP_LOSS 🔴"

                if should_close:
                    print(f"🚨 [ЗАКРИТТЯ ПОЗИЦІЇ] {pair} досяг рівня {reason} (Ціна: {current_price})")
                    close_side = 'sell' if direction == "LONG" else 'buy'
                    
                    # Закриваємо позицію повним ринковим ордером на весь об'єм
                    exchange.create_order(pair, 'market', close_side, exchange.amount_to_precision(pair, abs(p_size)))
                    print(f"🏁 Позицію по {pair} успішно ліквідовано ботом.")
                
                continue  # Якщо позиція відкрита, пошук нових сигналів на вхід по цій парі ігноруємо

            # --- БЛОК КУПІВЛІ (ПОШУК СИГНАЛІВ НА ВХІД) ---
            # Аналізуємо свічку, яка Щойно Закрилась (індекс -2)
            c_open, c_high, c_low, c_close, c_vol = [float(candles[-2][i]) for i in range(1, 6)]
            
            past_candles = candles[:-2]
            avg_volume_24h = sum(float(c[5]) for c in past_candles) / len(past_candles)
            avg_atr_24h = sum(abs(float(c[2]) - float(c[3])) for c in past_candles) / len(past_candles)
            confirmed_spread = abs(c_high - c_low)

            volume_spike = c_vol >= (avg_volume_24h * VOLUME_MULTIPLIER)
            overextended = confirmed_spread > (avg_atr_24h * ANOMALY_COEF)

            # Перевіряємо умови та наявність мінімального балансу
            if volume_spike and not overextended and free_balance >= INVEST_PER_TRADE:
                trade_direction = "LONG" if c_close > c_open else "SHORT"
                side = 'buy' if trade_direction == "LONG" else 'sell'
                
                print(f"🎯 [СИГНАЛ НА ВХІД] Виявлено аномалію на {pair} -> Сигнал у {trade_direction}")
                
                # Об'єм ордера з урахуванням кредитного плеча
                amount_to_buy = (INVEST_PER_TRADE * LEVERAGE) / current_price
                
                # Підганяємо об'єм під мінімальні ліміти контракту біржі
                market_info = exchange.market(pair)
                min_amount = market_info['limits']['amount']['min']
                if amount_to_buy < min_amount: amount_to_buy = min_amount

                # Намагаємось виставити плече на біржі (якщо ще не виставлено)
                try: exchange.set_leverage(LEVERAGE, pair)
                except: pass
                
                # Відкриваємо позицію маркет-ордером
                exchange.create_order(pair, 'market', side, exchange.amount_to_precision(pair, amount_to_buy))
                print(f"🔥 Успішно зайшли в ринок по {pair} ({trade_direction}) за ціною {current_price}")

        except Exception as e:
            print(f"⚠️ [ПОМИЛКА МОДУЛЯ] Не вдалося обробити пару {pair}: {e}")

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")

# --- БЛОК ЗАПУСКУ ТА ТАЙМЕРА (15-ХВИЛИНКИ) ---
if __name__ == "__main__":
    print("🤖 Снайпер-Бот запущений та готовий до роботи.")
    try: 
        exchange.load_markets()
        print("✅ Ринки успішно завантажені з WhiteBIT.")
    except Exception as e: 
        print(f"⚠️ Помилка завантаження специфікацій ринків: {e}")
        
    last_processed_minute = -1
    
    while True:
        now = datetime.now()
        # Спрацьовує точно на початку кожної 15-ї хвилини години (00, 15, 30, 45)
        if now.minute in [0, 15, 30, 45] and now.minute != last_processed_minute:
            if now.second >= 2:  # Пауза 2 секунди, щоб свічка на біржі гарантовано закрилась і прогрузилась
                last_processed_minute = now.minute
                try: 
                    run_scanner_cycle()
                except Exception as main_e: 
                    print(f"🚨 Помилка в головному циклі виконання: {main_e}")
                    
        if now.minute not in [0, 15, 30, 45]: 
            last_processed_minute = -1
            
        time.sleep(0.5)
