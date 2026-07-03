import ccxt
import time
from datetime import datetime

# ==================== НАЛАШТУВАННЯ ====================
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

# ==================== API КЛЮЧІ (тестовий субрахунок) ====================
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'
    }
})

def clean_symbol_name(symbol):
    if not symbol:
        return ""
    return symbol.replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ЦИКЛУ СКАНИРУВАННЯ ---")

    # === БАЛАНС ===
    free_balance = INVEST_PER_TRADE
    try:
        balances = exchange.fetch_balance(params={'type': 'swap'})
        free_balance = float(balances.get('free', {}).get('USDT', 0.0))
        
        if free_balance < 1:
            balances2 = exchange.fetch_balance(params={'account': 'collateral'})
            free_balance = float(balances2.get('free', {}).get('USDT', 0.0))
            
        print(f"💰 Вільний баланс (collateral): {free_balance:.2f} USDT")
    except Exception as e:
        print(f"⚠️ [ПОМИЛКА БАЛАНСУ] {e}")
        print(f"ℹ️ Продовжуємо з дефолтом: {free_balance} USDT")

    # === ПОЗИЦІЇ ===
    real_positions = {}
    try:
        positions_raw = exchange.fetch_positions(params={'type': 'swap'})
        
        for pos in positions_raw:
            p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if abs(p_size) > 0.000001:
                clean_name = clean_symbol_name(pos.get('symbol'))
                real_positions[clean_name] = pos

        print(f"📊 Активних позицій на біржі: {len(real_positions)}")
        if real_positions:
            for name, p in real_positions.items():
                print(f"   • {name} | {p.get('side', 'unknown').upper()} | Об'єм: {p.get('contracts') or p.get('size')}")
    except Exception as e:
        print(f"⚠️ [ПОМИЛКА ПОЗИЦІЙ] {e}")

    # === СКАНУВАННЯ РИНКУ ===
    for pair in SCAN_MARKETS:
        time.sleep(0.1)
        clean_pair = clean_symbol_name(pair)
        has_position = clean_pair in real_positions

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98: 
                continue

            current_price = float(candles[-1][4])

            # БЛОК ПРОДАЖУ (МОНІТОРИНГ)
            if has_position:
                pos = real_positions[clean_pair]
                p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                direction = "LONG" if (p_size > 0 or pos.get('side') == 'long') else "SHORT"
                entry_price = float(pos.get('entryPrice', 0))

                if entry_price == 0: 
                    print(f"⚠️ {pair}: нульова ціна входу")
                    continue

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
                    print(f"🚨 [ЗАКРИТТЯ] {pair} -> {reason}")
                    close_side = 'sell' if direction == "LONG" else 'buy'
                    try:
                        exchange.create_order(pair, 'market', close_side, exchange.amount_to_precision(pair, abs(p_size)))
                        print(f"🏁 Позицію {pair} закрито")
                    except Exception as err:
                        print(f"❌ Помилка закриття: {err}")
                continue

            # БЛОК ВХОДУ
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

                print(f"🎯 [СИГНАЛ] {pair} -> {trade_direction}")

                amount_to_buy = (INVEST_PER_TRADE * LEVERAGE) / current_price

                try:
                    market_info = exchange.market(pair)
                    min_amount = market_info['limits']['amount']['min']
                    if amount_to_buy < min_amount:
                        amount_to_buy = min_amount
                except:
                    pass

                try: 
                    exchange.set_leverage(LEVERAGE, pair)
                except: 
                    pass

                try:
                    exchange.create_order(pair, 'market', side, exchange.amount_to_precision(pair, amount_to_buy))
                    print(f"🔥 Вхід виконано по {pair} ({trade_direction})")
                except Exception as order_err:
                    print(f"❌ Помилка ордера {pair}: {order_err}")

        except Exception as e:
            print(f"⚠️ [ПОМИЛКА ПАРИ] {pair}: {e}")
            continue

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    print("🤖 Бот-Снайпер (тестовий субрахунок) запущений")
    try: 
        exchange.load_markets()
        print("✅ Ринки завантажені")
    except Exception as e: 
        print(f"⚠️ Помилка ринків: {e}")

    last_processed_minute = -1

    while True:
        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.minute != last_processed_minute:
            if now.second >= 2:
                last_processed_minute = now.minute
                try: 
                    run_scanner_cycle()
                except Exception as e: 
                    print(f"🚨 Критична помилка: {e}")

        if now.minute not in [0, 15, 30, 45]: 
            last_processed_minute = -1

        time.sleep(0.5)