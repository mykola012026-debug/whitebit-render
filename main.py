import time, ccxt
from datetime import datetime

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'ONDO/USDT:USDT', 'LINK/USDT:USDT', 'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'FET/USDT:USDT', 'SOL/USDT:USDT', 'SUI/USDT:USDT', 'BNB/USDT:USDT', 'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT', 'BCH/USDT:USDT', 'TRX/USDT:USDT']
BASE_VOL, LEV, TIMEOUT = 6, 10, 3600
TP_P, SL_P = 0.008, 0.006

exchange = ccxt.whitebit({'apiKey': '9dfcbc7d6c30802daf10d0bb50bf50d1', 'secret': '4ff8480b5bb8914e4dacf7ac40401762', 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

active_traps, history_cache, last_hour = {}, {}, -1

def calc_ema(prices, n):
    if len(prices) < n: return 0
    k = 2 / (n + 1)
    ema = sum(prices[:n]) / n
    for p in prices[n:]: ema = p * k + ema * (1 - k)
    return ema

def get_data(symbol, tf='15m', limit=100):
    try:
        b = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        return [x[4] for x in b], [x[5] for x in b]
    except: return None, None

def check_trend(symbol):
    c, _ = get_data(symbol, '1h')
    if not c or len(c) < 50: return "FLAT", 0
    return ("LONG_ONLY" if c[-1] > calc_ema(c, 50) else "SHORT_ONLY"), c[-1]

def protect(positions):
    for s, p in positions.items():
        try:
            size, entry = float(p.get('contracts') or 0), float(p.get('entryPrice') or 0)
            is_long = size > 0
            orders = exchange.fetch_open_orders(s)
            has_sl = any(float(o.get('stopPrice', 0)) > 0 for o in orders if (is_long and float(o.get('stopPrice', 0)) < entry) or (not is_long and float(o.get('stopPrice', 0)) > entry))
            has_tp = any(float(o.get('stopPrice', 0)) > 0 for o in orders if (is_long and float(o.get('stopPrice', 0)) > entry) or (not is_long and float(o.get('stopPrice', 0)) < entry))
            
            if not has_sl or not has_tp:
                sl_prc = entry * (1 - SL_P if is_long else 1 + SL_P)
                tp_prc = entry * (1 + TP_P if is_long else 1 - TP_P)
                if not has_sl: 
                    exchange.create_order(s, 'market', 'sell' if is_long else 'buy', abs(size), {'stopPrice': float(exchange.price_to_precision(s, sl_prc))})
                    print(f"🛑 [PROTECT] {s.split('/')[0]} SL: {sl_prc:.4f}")
                if not has_tp: 
                    exchange.create_order(s, 'market', 'sell' if is_long else 'buy', abs(size), {'stopPrice': float(exchange.price_to_precision(s, tp_prc))})
                    print(f"🟢 [PROTECT] {s.split('/')[0]} TP: {tp_prc:.4f}")
                if s in active_traps: del active_traps[s]
        except Exception as e: print(f"Err protect {s}: {e}")

def cleaner(s, positions):
    try:
        if s in history_cache and s not in positions: del history_cache[s]
        
        # Обробка статусів та таймаутів активних пасток
        if s in active_traps:
            o = exchange.fetch_order(active_traps[s]['id'], s)
            if o['status'] in ['closed', 'filled']:
                print(f"🕸️ [TRAP FILLED] {s.split('/')[0]} | Позицію відкрито.")
                del active_traps[s]
                return
            elif o['status'] == 'canceled':
                del active_traps[s]
                return
            elif time.time() - active_traps[s]['time'] >= TIMEOUT:
                print(f"⏰ [TIMEOUT] {s.split('/')[0]} | Знімаємо лімітку.")
                try: exchange.cancel_order(active_traps[s]['id'], s)
                except: pass
                del active_traps[s]
                return

        for o in exchange.fetch_open_orders(s):
            if not o.get('stopPrice'):
                c, _ = get_data(s)
                if c and len(c) > 25:
                    ema25 = calc_ema(c, 25)
                    trend, _ = check_trend(s)
                    if (trend == "LONG_ONLY" and c[-1] <= ema25) or (trend == "SHORT_ONLY" and c[-1] >= ema25):
                        exchange.cancel_order(o['id'], s)
                        print(f"🧹 [CLEAN] {s.split('/')[0]}")
                        if s in active_traps: del active_traps[s]
    except: pass

def main():
    global last_hour
    exchange.load_markets()
    while True:
        try:
            pos = {p['symbol']: p for p in exchange.fetch_positions(SYMBOLS) if float(p.get('contracts') or 0) > 0}
            if pos: protect(pos)
            
            curr = datetime.now()
            if curr.hour != last_hour:
                print(f"\n📊 {curr.strftime('%H:%M')} | Моніторинг активний...")
                last_hour = curr.hour
            
            for s in SYMBOLS:
                cleaner(s, pos)
                if s in pos or s in active_traps: continue  # Тепер чітко ігноруємо, якщо пастка вже є
                
                c, v = get_data(s)
                if not c or len(c) < 25: continue
                ema25, trend = calc_ema(c, 25), check_trend(s)[0]
                ratio = v[-2] / (sum(v[-21:-1]) / 20) if sum(v[-21:-1]) > 0 else 0
                
                if ratio >= 1.1:
                    off = 0.003 if ratio >= 1.8 else 0.004 if ratio >= 1.4 else 0.005
                    target = c[-1] * (1 - off if trend == "LONG_ONLY" else 1 + off)
                    if (trend == "LONG_ONLY" and c[-1] > ema25) or (trend == "SHORT_ONLY" and c[-1] < ema25):
                        order = exchange.create_order(s, 'limit', 'buy' if trend == "LONG_ONLY" else 'sell', (BASE_VOL*LEV)/target, target)
                        # Записуємо в базу, щоб уникнути дублювання:
                        active_traps[s] = {'id': order['id'], 'time': time.time()}
                        print(f"🕸️ [TRAP] {s.split('/')[0]} | {ratio:.1f}x | {target:.4f}")
            time.sleep(15)
        except Exception as e: print(f"Err cycle: {e}"); time.sleep(10)

if __name__ == "__main__": main()
