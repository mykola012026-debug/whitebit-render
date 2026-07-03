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
    if not symbol:
        return ""
    return symbol.replace('/', '-').replace('_', '-').replace(':', '-').split('-')[0].upper()

def safe_float(value, default=0.0):
    """Безпечне перетворення в float"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def run_scanner_cycle():
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ЦИКЛУ СКАНИРУВАННЯ ---")

    # === 1. БАЛАНС ===
    free_balance = INVEST_PER_TRADE
    try:
        print("🔍 fetch_balance()")
        balances = exchange.fetch_balance()
        
        # Розширене отримання USDT
        free_usdt = (balances.get('free', {}) or {}).get('USDT') or \
                    balances.get('USDT', {}).get('free') or \
                    balances.get('total', {}).get('USDT') or 0
        
        free_balance = safe_float(free_usdt)
        print(f"✅ Баланс USDT: {free_balance:.2f}")
        
    except Exception as e:
        print(f"⚠️ Помилка балансу: {e}")

    # === 2. ПОЗИЦІЇ ===
    real_positions = {}
    try:
        print("🔍 fetch_positions()")
        positions_raw = exchange.fetch_positions()
        print(f"Отримано рядків: {len(positions_raw)}")

        for pos in positions_raw:
            p_size = safe_float(pos.get('contracts') or pos.get('size') or pos.get('info', {}).get('size', 0))
            if abs(p_size) > 0.000001:
                clean_name = clean_symbol_name(pos.get('symbol'))
                real_positions[clean_name] = pos
                print(f"   ✅ Позиція: {clean_name} | size={p_size} | side={pos.get('side')}")

        print(f"📊 Активних позицій: {len(real_positions)}")
    except Exception as e:
        print(f"⚠️ Помилка позицій: {e}")

    # === 3. СКАНУВАННЯ (скорочено для стабільності) ===
    for pair in SCAN_MARKETS:
        time.sleep(0.1)
        clean_pair = clean_symbol_name(pair)
        has_position = clean_pair in real_positions

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98:
                continue

            current_price = float(candles[-1][4])

            if has_position:
                # ... (твій код закриття без змін) ...
                continue

            # Блок входу (спрощений)
            c_open = float(candles[-2][1])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            past_volumes = [float(c[5]) for c in candles[:-2]]
            avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1

            if c_vol >= avg_volume * VOLUME_MULTIPLIER and free_balance >= INVEST_PER_TRADE:
                direction = "LONG" if c_close > c_open else "SHORT"
                side = 'buy' if direction == "LONG" else 'sell'
                print(f"🎯 [СИГНАЛ] {pair} -> {direction}")

                amount = (INVEST_PER_TRADE * LEVERAGE) / current_price
                try:
                    exchange.create_order(pair, 'market', side, exchange.amount_to_precision(pair, amount))
                    print(f"🔥 Вхід {pair}")
                except Exception as err:
                    print(f"❌ Ордер помилка: {err}")

        except Exception as e:
            continue

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений")
    exchange.load_markets()
    print("✅ Ринки завантажено")

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