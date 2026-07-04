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

# ==================== API ====================
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'
    }
})

def clean_symbol_name(symbol):
    if not symbol: return ""
    return symbol.replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()

def safe_float(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except:
        return default

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ЦИКЛУ СКАНИРУВАННЯ ---")

    # === БАЛАНС ===
    free_balance = INVEST_PER_TRADE
    try:
        balances = exchange.fetch_balance()
        free_usdt = (balances.get('free', {}) or {}).get('USDT') or balances.get('USDT', {}).get('free') or 0
        free_balance = safe_float(free_usdt)
        print(f"💰 Баланс USDT: {free_balance:.2f}")
    except Exception as e:
        print(f"⚠️ Помилка балансу: {e}")

    # === ПОЗИЦІЇ ===
    real_positions = {}
    try:
        positions_raw = exchange.fetch_positions()
        print(f"🔍 Отримано рядків позицій: {len(positions_raw)}")

        for pos in positions_raw:
            p_size = safe_float(pos.get('contracts') or pos.get('size') or 0)
            if abs(p_size) > 0.000001:
                clean_name = clean_symbol_name(pos.get('symbol'))
                real_positions[clean_name] = pos
                print(f"   ✅ Активна позиція: {clean_name} | size={p_size}")

        print(f"📊 Активних позицій: {len(real_positions)}")
    except Exception as e:
        print(f"⚠️ Помилка позицій: {e}")

    # === СКАНУВАННЯ ===
    for pair in SCAN_MARKETS:
        time.sleep(0.1)
        clean_pair = clean_symbol_name(pair)
        has_position = clean_pair in real_positions

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98:
                continue

            current_price = float(candles[-1][4])

            # === ЗАКРИТТЯ ===
            if has_position:
                # ... (твій код закриття) ...
                continue

            # === АНАЛІЗ СИГНАЛУ ===
            c_open = float(candles[-2][1])
            c_high = float(candles[-2][2])
            c_low = float(candles[-2][3])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            past_candles = candles[:-2]
            avg_volume = sum(float(c[5]) for c in past_candles) / len(past_candles)
            avg_atr = sum(abs(float(c[2]) - float(c[3])) for c in past_candles) / len(past_candles)

            volume_spike = c_vol >= (avg_volume * VOLUME_MULTIPLIER)
            overextended = abs(c_high - c_low) > (avg_atr * ANOMALY_COEF)

            signal = volume_spike and not overextended and free_balance >= INVEST_PER_TRADE

            print(f"📊 {pair}: Vol spike={volume_spike}, Over={overextended}, Баланс OK={free_balance >= INVEST_PER_TRADE} → СИГНАЛ={'✅ ТАК' if signal else '❌ НІ'}")

            if signal:
                direction = "LONG" if c_close > c_open else "SHORT"
                side = 'buy' if direction == "LONG" else 'sell'
                print(f"🎯 [СИГНАЛ НА ВХІД] {pair} → {direction}")

                amount = (INVEST_PER_TRADE * LEVERAGE) / current_price
                try:
                    exchange.set_leverage(LEVERAGE, pair)
                    exchange.create_order(pair, 'market', side, exchange.amount_to_precision(pair, amount))
                    print(f"🔥 Вхід виконано по {pair}")
                except Exception as err:
                    print(f"❌ Помилка ордера: {err}")

        except Exception as e:
            print(f"⚠️ Помилка {pair}: {e}")
            continue

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений")
    try:
        exchange.load_markets()
        print("✅ Ринки завантажено")
    except Exception as e:
        print(f"⚠️ Помилка ринків: {e}")

    last_minute = -1
    while True:
        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.minute != last_minute:
            if now.second >= 2:
                last_minute = now.minute
                run_scanner_cycle()
        else:
            last_minute = -1 if now.minute not in [0, 15, 30, 45] else last_minute
        time.sleep(0.5)