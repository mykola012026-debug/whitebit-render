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

# ==================== НАЛАШТУВАННЯ API ====================
exchange = ccxt.whitebit({
    'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1',
    'secret': '4ff8480b5bb8914e4dacf7ac40401762',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',        
        'accountsByType': {
            'swap': 'collateral',     
        }
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
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] --- СТАРТ ЦИКЛУ СКАНУВАННЯ ---")

    # === 1. ОТРИМАННЯ РЕАЛЬНОГО БАЛАНСУ ===
    free_balance = 0.0
    try:
        print("🔍 Запит балансу з рахунку Collateral...")
        balances = exchange.fetch_balance()
        
        free_usdt = balances.get('USDT', {}).get('free', 0.0)
        free_balance = safe_float(free_usdt)
        print(f"✅ Реальний баланс USDT (Free): {free_balance:.2f}")
        
    except Exception as e:
        print(f"⚠️ Помилка отримання балансу: {e}")
        return  

    # === 2. ОТРИМАННЯ РЕАЛЬНИХ ПОЗИЦІЙ ===
    real_positions = {}
    try:
        print("🔍 Запит активних ф'ючерсних позицій...")
        positions_raw = exchange.fetch_positions(SCAN_MARKETS)

        for pos in positions_raw:
            p_size = safe_float(pos.get('contracts') or pos.get('size'))
            market_symbol = pos.get('symbol')  
            
            if abs(p_size) > 0.000001 and market_symbol:
                real_positions[market_symbol] = pos
                print(f"   ✅ Знайдено позицію: {market_symbol} | Розмір={p_size} | Напрямок={pos.get('side')}")

        print(f"📊 Активних позицій у моніторингу: {len(real_positions)}")
    except Exception as e:
        print(f"⚠️ Помилка отримання позицій: {e}")

    # === 3. СКАНУВАННЯ СВІЧОК ТА ВХІД В УГОДИ ===
    for pair in SCAN_MARKETS:
        time.sleep(0.2)  
        
        has_position = pair in real_positions

        try:
            # Запитуємо 100 свічок, щоб гарантовано мати 98 історичних
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=100)
            if not candles or len(candles) < 98:
                print(f"🎚️ Недостатньо свічок для пари {pair}")
                continue

            current_price = float(candles[-1][4]) 

            if has_position:
                continue

            # Беремо дані з останньої закритиї свічки (індекс -2)
            c_open = float(candles[-2][1])   
            c_close = float(candles[-2][4])  
            c_vol = float(candles[-2][5])    

            past_volumes = [float(c[5]) for c in candles[:-2]]
            avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0

            if c_vol >= avg_volume * VOLUME_MULTIPLIER:
                
                if free_balance < INVEST_PER_TRADE:
                    print(f"📉 Сигнал по {pair} пропущено: на балансі {free_balance:.2f} USDT, а потрібно {INVEST_PER_TRADE} USDT")
                    continue

                direction = "LONG" if c_close > c_open else "SHORT"
                side = 'buy' if direction == "LONG" else 'sell'
                print(f"🎯 [СИГНАЛ] {pair} -> {direction} | Об'єм свічки: {c_vol:.1f} (Середній: {avg_volume:.1f})")

                amount_usdt = INVEST_PER_TRADE * LEVERAGE
                amount_contracts = amount_usdt / current_price
                
                precise_amount = exchange.amount_to_precision(pair, amount_contracts)
                
                try:
                    print(f"🚀 Надсилання маркет-ордера: {side.upper()} {precise_amount} контрактів по {pair}")
                    order = exchange.create_order(
                        symbol=pair,
                        type='market',
                        side=side,
                        amount=float(precise_amount) 
                    )
                    print(f"🔥 Вхід успішний! ID ордера: {order.get('id')}")
                    
                    free_balance -= INVEST_PER_TRADE
                    
                except Exception as err:
                    print(f"❌ Помилка виставлення ордера по {pair}: {err}")

        except Exception as e:
            print(f"⚠️ Помилка обробки ринку {pair}: {e}")
            continue

    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] --- ЦИКЛ ЗАВЕРШЕНО ---")


if __name__ == "__main__":
    print("🤖 Бот запущений")
    try:
        exchange.load_markets()
        print("✅ Ринки ф'ючерсів успішно завантажено")
    except Exception as e:
        print(f"❌ Критична помилка завантаження специфікацій ринків: {e}")
        exit()

    last_minute = -1
    while True:
        now = datetime.now()
        
        # Перевіряємо, чи ділитися поточна хвилина на 15 без залишку (тобто 0, 15, 30, 45)
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
