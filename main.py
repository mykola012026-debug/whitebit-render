import ccxt
import json
import os
import time
import csv
import random
from datetime import datetime

# --- НАЛАШТУВАННЯ СКАНЕРА & РИЗИКІВ (РЕЖИМ СНАЙПЕРА) ---
SCAN_MARKETS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "FET/USDT:USDT", 
    "ONDO/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT", "RENDER/USDT:USDT", "LINK/USDT:USDT"
]
TAKE_PROFIT_PCT = 0.05      # 5% руху ціни
STOP_LOSS_PCT = 0.035       # 3.5% руху ціни
VOLUME_MULTIPLIER = 2.2     # Вхід ТІЛЬКИ якщо об'єм у 2.2 рази вищий за норму
ANOMALY_COEF = 2.5          # Якщо свічка більша за норму в 2.5 рази — вхід ЗАБОРОНЕНО
INVEST_PER_TRADE = 5.5      
LEVERAGE = 3
DRY_RUN = False 
RESET_DATA = False

# --- ІНІЦІАЛІЗАЦІЯ API ВІД WHITEBIT ---
exchange_config = {
    'apiKey': os.environ.get('WHITEBIT_API_KEY', '9dfcbc7d6c30802daf10d0bb50bf50d1'),
    'secret': os.environ.get('WHITEBIT_SECRET_KEY', '4ff8480b5bb8914e4dacf7ac40401762'),
    'enableRateLimit': True,
    'options': {
        'defaultType': 'futures',      
        'account-level': 'collateral'  
    }
}
exchange = ccxt.whitebit(exchange_config)

# --- РОБОТА З ЛОКАЛЬНОЮ БАЗОЮ ДАНИХ ---
DB_DIR = "/data" if (os.path.exists("/data") or os.environ.get("RENDER")) else "."
os.makedirs(DB_DIR, exist_ok=True)
DB_FILE = os.path.join(DB_DIR, "virtual_portfolio.json")

def load_data():
    global RESET_DATA
    if RESET_DATA:
        if os.path.exists(DB_FILE):
            try: os.remove(DB_FILE)
            except: pass
        RESET_DATA = False 
        return {"balance_usdt": 100.0, "active_trades": {}, "history": []}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                db = json.load(f)
                db.setdefault("active_trades", {})
                db.setdefault("history", [])
                db.setdefault("balance_usdt", 100.0)
                return db
        except: pass
    return {"balance_usdt": 100.0, "active_trades": {}, "history": []}

def save_data(data):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)
        invested_now = sum(t.get("invested_amount", 0) for t in data.get("active_trades", {}).values())
        total_equity = data.get("balance_usdt", 100.0) + invested_now
        csv_path = os.path.join(DB_DIR, "trades_history.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["ЗАГАЛЬНА СТАТИСТИКА"])
            writer.writerow(["Загальний капітал", f"{total_equity:.2f} USDT"])
            writer.writerow(["Вільний баланс", f"{data.get('balance_usdt', 100.0):.2f} USDT"])
            writer.writerow([])
            writer.writerow(["АКТИВНІ УГОДИ"])
            active = data.get("active_trades", {})
            if active:
                writer.writerow(["Пара", "Напрямок", "Ціна входу", "Інвестовано", "Take Profit", "Stop Loss"])
                for pair, t in active.items():
                    writer.writerow([t.get("pair"), t.get("direction"), format_price(pair, t.get("buy_price")), f"{t.get('invested_amount'):.2f}", format_price(pair, t.get("take_profit")), format_price(pair, t.get("stop_loss"))])
            else: writer.writerow(["Немає активних угод"])
            writer.writerow([])
            writer.writerow(["ІСТОРІЯ ЗАКРИТИХ УГОД"])
            history = data.get("history", [])
            if history:
                writer.writerow(["Пара", "Напрямок", "Ціна входу", "Ціна виходу", "Результат (PnL)", "Статус"])
                for t in history:
                    p = t.get("pair")
                    writer.writerow([p, t.get("direction"), format_price(p, t.get("buy_price")), format_price(p, t.get("exit_price")), f"{t.get('pnl', 0):+.2f}", t.get("status")])
    except Exception as e: print(f"❌ Помилка запису файлів: {e}")

def format_price(pair, price):
    if price is None: return "0.0"
    try: return exchange.price_to_precision(pair, price)
    except: return f"{price:.6f}".rstrip('0').rstrip('.') if price < 1.0 else f"{price:.2f}"

def fetch_safe_balance():
    for attempt in range(3):
        try: return exchange.fetch_balance()
        except: time.sleep(1 + random.uniform(0.5, 1.5))

def clean_symbol_name(symbol):
    if not symbol: return ""
    s = symbol.replace('/', '-').replace('_', '-').replace(':', '-')
    return s.split('-')[0].upper()

# --- ОСНОВНИЙ МОДУЛЬ АНАЛІЗУ ТА ТОРГІВЛІ ---
def run_scanner_cycle():
    data = load_data()
    if not DRY_RUN:
        try:
            balances = fetch_safe_balance()
            data["balance_usdt"] = float(balances['free'].get('USDT', 0.0))
        except Exception as e:
            print(f"❌ Не вдалося отримати баланс з біржі: {e}")
            return

    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] Скан 15m | Вільний баланс: {data['balance_usdt']:.2f} USDT")

    # Збір реальних позицій з біржі
    real_active_positions = {}
    if not DRY_RUN:
        try:
            real_positions = exchange.fetch_positions(params={'type': 'futures'})
            if not real_positions:
                real_positions = exchange.fetch_positions(params={'type': 'swap'})

            print(f"  🔍 [DEBUG] Знайдено позицій на біржі: {len(real_positions)}")
            print("  📝 [ДЕТАЛЬНИЙ ДЕБАГ ПОЗИЦІЙ]:")

            for pos in real_positions:
                p_size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                symbol_raw = pos.get('symbol', 'UNKNOWN')
                clean_name = clean_symbol_name(symbol_raw)

                print(f"    -> Біржа дала символ: '{symbol_raw}' | Чисте ім'я бота: '{clean_name}' | Розмір: {p_size}")

                if abs(p_size) > 0:
                    real_active_positions[clean_name] = pos
        except Exception as e:
            print(f"  ⚠️ Не вдалося отримати список позицій з біржі: {e}")

    for pair in SCAN_MARKETS:
        free_balance = data["balance_usdt"]
        time.sleep(0.05)

        clean_pair = clean_symbol_name(pair)
        real_pos = real_active_positions.get(clean_pair)
        real_position_exists = real_pos is not None

        # --- ЕТАП 1: АВТОПІДХОПЛЕННЯ ---
        if real_position_exists and pair not in data["active_trades"]:
            try:
                p_size = float(real_pos.get('contracts', 0) or real_pos.get('size', 0) or 0)
                direction = "SHORT" if (p_size < 0 or real_pos.get('side') == 'short') else "LONG"

                real_entry_price = float(real_pos.get('entryPrice') or 0)
                if real_entry_price == 0:
                    try: 
                        ticker = exchange.fetch_ticker(pair)
                        real_entry_price = float(ticker['last'])
                    except: pass

                if real_entry_price > 0:
                    tp_raw = real_entry_price * (1 + TAKE_PROFIT_PCT if direction == "LONG" else 1 - TAKE_PROFIT_PCT)
                    sl_raw = real_entry_price * (1 - STOP_LOSS_PCT if direction == "LONG" else 1 + STOP_LOSS_PCT)
                    contracts = abs(p_size)
                    est_invested = (contracts * real_entry_price) / LEVERAGE
                    if est_invested <= 0: est_invested = INVEST_PER_TRADE

                    print(f"  📥 [АВТОПІДХОПЛЕННЯ] Синхронізовано активну позицію для {pair} з біржі.")
                    data["active_trades"][pair] = {
                        "pair": pair, "direction": direction, "buy_price": real_entry_price,
                        "invested_amount": round(est_invested, 2), "take_profit": tp_raw, "stop_loss": sl_raw,
                        "status": "OPEN", "open_time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
            except Exception as e_adopt:
                print(f"  ❌ Помилка обробки автопідхоплення для {pair}: {e_adopt}")

        # --- ЕТАП 2: ОТРИМАННЯ РИНКОВИХ ДАНИХ ДЛЯ МОНІТОРИНГУ ТА СИГНАЛІВ ---
        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98: continue

            c_open = float(candles[-2][1])
            c_high = float(candles[-2][2])
            c_low = float(candles[-2][3])
            c_close = float(candles[-2][4])
            c_vol = float(candles[-2][5])

            curr_low = float(candles[-1][3])
            curr_high = float(candles[-1][2])
            current_price = float(candles[-1][4])

            past_volumes = [float(candle[5]) for candle in candles[:-2]]
            avg_volume_24h = sum(past_volumes) / len(past_volumes)

            past_atr = [abs(float(c[2]) - float(c[3])) for c in candles[:-2]]
            avg_atr_24h = sum(past_atr) / len(past_atr)

            confirmed_spread = abs(c_high - c_low)

            market = {
                "open_price": c_open, "close_price": c_close, "volume": c_vol,
                "avg_volume_24h": avg_volume_24h, "current_low": curr_low,
                "current_high": curr_high, "confirmed_spread": confirmed_spread, "avg_atr": avg_atr_24h
            }
        except:
            continue

        # --- ЕТАП 3: МОНІТОРИНГ ТА ЗАКРИТТЯ ---
        if pair in data["active_trades"]:
            if not real_position_exists and not DRY_RUN:
                del data["active_trades"][pair]
                continue

            trade = data["active_trades"][pair]
            direction = trade.get("direction", "LONG")
            p_in = trade['buy_price']
            invested = trade["invested_amount"]
            closed, exit_p, reason = False, current_price, ""

            if direction == "LONG":
                if market["current_low"] <= trade["stop_loss"]: exit_p, closed, reason = trade["stop_loss"], True, "STOP_LOSS 🔴"
                elif market["current_high"] >= trade["take_profit"]: exit_p, closed, reason = trade["take_profit"], True, "TAKE_PROFIT 🟢"
            else:
                if market["current_high"] >= trade["stop_loss"]: exit_p, closed, reason = trade["stop_loss"], True, "STOP_LOSS 🔴"
                elif market["current_low"] <= trade["take_profit"]: exit_p, closed, reason = trade["take_profit"], True, "TAKE_PROFIT 🟢"

            if closed:
                pnl = invested * ((exit_p - p_in) / p_in if direction == "LONG" else (p_in - exit_p) / p_in)
                if DRY_RUN: data["balance_usdt"] += (invested + pnl)

                if not DRY_RUN:
                    try:
                        close_side = 'sell' if direction == "LONG" else 'buy'
                        contracts = abs(float(real_pos.get('contracts', 0) or real_pos.get('size', 0) or 0))

                        if contracts > 0:
                            exchange.create_order(pair, 'market', close_side, exchange.amount_to_precision(pair, contracts))
                        else:
                            exchange.create_order(pair, 'market', close_side, exchange.amount_to_precision(pair, invested * LEVERAGE / current_price))
                    except Exception as close_err: 
                        print(f"  ❌ Помилка виконання ордера закриття на біржі: {close_err}")

                trade.update({"status": reason, "exit_price": exit_p, "close_time": time.strftime("%Y-%m-%d %H:%M:%S"), "pnl": pnl})
                data["history"].append(trade)
                del data["active_trades"][pair]
                print(f"  🏁 Закрито {pair}! Результат: {pnl:+.2f} USDT ({reason})")

        # --- ЕТАП 4: ПОШУК НОВИХ СИГНАЛІВ НА ВХІД ---
        else:
            if not real_position_exists:
                current_volume = market["volume"]
                avg_volume = market["avg_volume_24h"]
                volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
                is_green_candle = market["close_price"] > market["open_price"]
                overextended = market["confirmed_spread"] > (market["avg_atr"] * ANOMALY_COEF)

                if volume_spike and not overextended and free_balance >= 5.0:
                    direction = "LONG" if is_green_candle else "SHORT"
                    real_entry_price = current_price

                    if not DRY_RUN:
                        try:
                            print(f"  📢 [РЕАЛ] Вхід у {direction} по {pair}...")
                            side = 'buy' if direction == "LONG" else 'sell'
                            try: exchange.set_leverage(LEVERAGE, pair)
                            except: pass

                            market_info = exchange.market(pair)
                            min_amount = market_info['limits']['amount']['min']

                            amount_to_buy = (INVEST_PER_TRADE * LEVERAGE) / current_price
                            if amount_to_buy < min_amount: amount_to_buy = min_amount

                            formatted_amount = exchange.amount_to_precision(pair, amount_to_buy)
                            if ((float(formatted_amount) * current_price) / LEVERAGE) > free_balance: continue

                            exchange.create_order(pair, 'market', side, formatted_amount)

                        except Exception as e:
                            print(f"  ❌ Помилка входу: {e}")
                            continue
                    else:
                        data["balance_usdt"] -= INVEST_PER_TRADE

                    tp_raw = real_entry_price * (1 + TAKE_PROFIT_PCT if direction == "LONG" else 1 - TAKE_PROFIT_PCT)
                    sl_raw = real_entry_price * (1 - STOP_LOSS_PCT if direction == "LONG" else 1 + STOP_LOSS_PCT)

                    print(f"  🔥 ВХІД {direction} НА {pair}! (Вхід: {real_entry_price:.4f}, SL: {sl_raw:.4f}, TP: {tp_raw:.4f})")
                    data["active_trades"][pair] = {
                        "pair": pair, "direction": direction, "buy_price": real_entry_price,
                        "invested_amount": INVEST_PER_TRADE, "take_profit": tp_raw, "stop_loss": sl_raw,
                        "status": "OPEN", "open_time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

    save_data(data)
    active_count = len(data["active_trades"])
    print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] Цикл завершено | Позицій в базі бота: {active_count}")

# --- БЛОК 3: ТАЙМЕР ТА ЦИКЛ ЗАПУСКУ ---
if __name__ == "__main__":
    print("🤖 Бот запущенний, база даних синхронізована.")
    try: exchange.load_markets()
    except Exception as e: print(f"⚠️ Помилка завантаження ринків: {e}")
    last_processed_minute = -1
    while True:
        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.minute != last_processed_minute:
            if now.second >= 2: 
                last_processed_minute = now.minute
                try: 
                    run_scanner_cycle()
                except Exception as main_e: 
                    print(f"🚨 Помилка в циклі виконання: {main_e}")
        if now.minute not in [0, 15, 30, 45]: last_processed_minute = -1
        time.sleep(0.5)