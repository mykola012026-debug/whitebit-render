import ccxt
import json
import os
import time
import csv
import random
from datetime import datetime

# --- НАЛАШТУВАННЯ СКАНЕРА & РИЗИКІВ ---
SCAN_MARKETS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "FET/USDT:USDT", 
    "ONDO/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT", "RENDER/USDT:USDT", "LINK/USDT:USDT"
]
TAKE_PROFIT_PCT = 0.03      
STOP_LOSS_PCT = 0.015       
VOLUME_MULTIPLIER = 2.5     
INVEST_PER_TRADE = 5.5      
LEVERAGE = 20
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

    active_count = len(data["active_trades"])
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] Скан 15m | Вільний баланс: {data['balance_usdt']:.2f} USDT | Позицій: {active_count}")

    for pair in SCAN_MARKETS:
        free_balance = data["balance_usdt"]
        time.sleep(0.3)
        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98: continue
            past_volumes = [float(candle[5]) for candle in candles[:-2]]
            avg_volume_24h = sum(past_volumes) / len(past_volumes)
            past_atr = [abs(float(c[2]) - float(c[3])) for c in candles[:-2]]
            avg_atr_24h = sum(past_atr) / len(past_atr)
            confirmed_candle = candles[-2]
            confirmed_spread = abs(float(confirmed_candle[2]) - float(confirmed_candle[3]))
            current_price = float(candles[-1][4])
            market = {
                "open_price": float(confirmed_candle[1]),
                "close_price": float(confirmed_candle[4]),
                "volume": float(confirmed_candle[5]),
                "avg_volume_24h": avg_volume_24h,
                "current_low": float(candles[-1][3]),
                "current_high": float(candles[-1][2]),
                "confirmed_spread": confirmed_spread,
                "avg_atr": avg_atr_24h
            }
        except: continue

        # --- БЛОК 1: МОНІТОРИНГ ВЖЕ ВІДКРИТИХ ПОЗИЦІЙ ---
        if pair in data["active_trades"]:
            trade = data["active_trades"][pair]
            direction = trade.get("direction", "LONG")
            p_in = trade['buy_price']
            invested = trade["invested_amount"]
            closed, exit_p, reason = False, current_price, ""

            if DRY_RUN:
                if direction == "LONG":
                    if market["current_low"] <= trade["stop_loss"]: exit_p, closed, reason = trade["stop_loss"], True, "STOP_LOSS 🔴"
                    elif market["current_high"] >= trade["take_profit"]: exit_p, closed, reason = trade["take_profit"], True, "TAKE_PROFIT 🟢"
                else:
                    if market["current_high"] >= trade["stop_loss"]: exit_p, closed, reason = trade["stop_loss"], True, "STOP_LOSS 🔴"
                    elif market["current_low"] <= trade["take_profit"]: exit_p, closed, reason = trade["take_profit"], True, "TAKE_PROFIT 🟢"
            else:
                try:
                    sl_id, tp_id = trade.get("sl_order_id"), trade.get("tp_order_id")
                    sl_status = exchange.fetch_order(sl_id, pair) if sl_id else {'status': 'open'}
                    tp_status = exchange.fetch_order(tp_id, pair) if tp_id else {'status': 'open'}

                    if sl_status['status'] == 'closed':
                        exit_p, closed, reason = float(sl_status.get('average', trade["stop_loss"])), True, "STOP_LOSS 🔴 (БІРЖА)"
                        if tp_id:
                            try: exchange.cancel_order(tp_id, pair)
                            except: pass
                    elif tp_status['status'] == 'closed':
                        exit_p, closed, reason = float(tp_status.get('average', trade["take_profit"])), True, "TAKE_PROFIT 🟢 (БІРЖА)"
                        if sl_id:
                            try: exchange.cancel_order(sl_id, pair)
                            except: pass
                except:
                    if direction == "LONG" and market["current_low"] <= trade["stop_loss"]: exit_p, closed, reason = trade["stop_loss"], True, "STOP_LOSS 🔴 (ФОЛБЕК)"
                    elif direction == "LONG" and market["current_high"] >= trade["take_profit"]: exit_p, closed, reason = trade["take_profit"], True, "TAKE_PROFIT 🟢 (ФОЛБЕК)"
                    elif direction == "SHORT" and market["current_high"] >= trade["stop_loss"]: exit_p, closed, reason = trade["stop_loss"], True, "STOP_LOSS 🔴 (ФОЛБЕК)"
                    elif direction == "SHORT" and market["current_low"] <= trade["take_profit"]: exit_p, closed, reason = trade["take_profit"], True, "TAKE_PROFIT 🟢 (ФОЛБЕК)"

            if closed:
                pnl = invested * ((exit_p - p_in) / p_in if direction == "LONG" else (p_in - exit_p) / p_in)
                if DRY_RUN: data["balance_usdt"] += (invested + pnl)
                trade.update({"status": reason, "exit_price": exit_p, "close_time": time.strftime("%Y-%m-%d %H:%M:%S"), "pnl": pnl})
                data["history"].append(trade)
                del data["active_trades"][pair]
                print(f"  🏁 Закрито {pair}! Результат: {pnl:+.2f} USDT ({reason})")

        # --- БЛОК 2: ПОШУК СИГНАЛІВ ТА СТВОРЕННЯ ОРДЕРІВ ---
        else:
            current_volume = market["volume"]
            avg_volume = market["avg_volume_24h"]
            volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
            is_green_candle = market["close_price"] > market["open_price"]
            overextended = market["confirmed_spread"] > (market["avg_atr"] * 3.0)

            if volume_spike and not overextended and free_balance >= 2.0:
                direction = "LONG" if is_green_candle else "SHORT"
                real_entry_price, sl_order_id, tp_order_id = current_price, None, None

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

                        order = exchange.create_order(pair, 'market', side, formatted_amount)
                        if 'trades' in order and order['trades']: real_entry_price = float(order['trades'][0].get('price', current_price))
                        elif order.get('average', 0) > 0: real_entry_price = float(order['average'])
                        elif order.get('price', 0) > 0: real_entry_price = float(order['price'])

                        # Розрахунок рівнів
                        tp_raw = real_entry_price * (1 + TAKE_PROFIT_PCT if direction == "LONG" else 1 - TAKE_PROFIT_PCT)
                        sl_raw = real_entry_price * (1 - STOP_LOSS_PCT if direction == "LONG" else 1 + STOP_LOSS_PCT)
                        
                        # Безпечне округлення відповідно до специфікації ринку
                        price_precision = market_info.get('precision', {}).get('price', 2)
                        f_sl = round(sl_raw, price_precision)
                        f_tp = round(tp_raw, price_precision)

                        trigger_side = 'sell' if direction == "LONG" else 'buy'

                        # 1. Виставлення STOP LOSS (stopMarket)
                        try:
                            sl_order = exchange.create_order(
                                symbol=pair,
                                type='stopMarket',
                                side=trigger_side,
                                amount=formatted_amount,
                                price=None,
                                params={'stopPrice': f_sl, 'activationPrice': f_sl, 'reduceOnly': True}
                            )
                            sl_order_id = sl_order.get('id')
                        except Exception as e: 
                            print(f"  ⚠️ Помилка SL: {e}")

                        # 2. Виставлення TAKE PROFIT (marketIfTouched)
                        try:
                            tp_order = exchange.create_order(
                                symbol=pair,
                                type='marketIfTouched',
                                side=trigger_side,
                                amount=formatted_amount,
                                price=None,
                                params={'stopPrice': f_tp, 'activationPrice': f_tp, 'reduceOnly': True}
                            )
                            tp_order_id = tp_order.get('id')
                        except Exception as e:
                            # Фолбек на випадок специфічного синтаксису API WhiteBIT v4 в CCXT
                            try:
                                tp_order = exchange.create_order(
                                    symbol=pair,
                                    type='stopMarket',
                                    side=trigger_side,
                                    amount=formatted_amount,
                                    price=None,
                                    params={'stopPrice': f_tp, 'activationPrice': f_tp, 'reduceOnly': True, 'type': 'marketIfTouched'}
                                )
                                tp_order_id = tp_order.get('id')
                            except Exception as e2:
                                print(f"  ⚠️ Помилка TP: {e2}")

                        # Якщо один з захисних ордерів не виставився — маркетно закриваємо позицію задля безпеки
                        if not sl_order_id or not tp_order_id:
                            print("  🚨 Критична помилка виставлення SL/TP. Закриваємо позицію!")
                            exchange.create_order(pair, 'market', 'sell' if direction == "LONG" else 'buy', formatted_amount)
                            continue
                            
                        tp, sl = f_tp, f_sl
                    except Exception as e:
                        print(f"  ❌ Помилка відкриття позиції: {e}")
                        continue
                else:
                    tp = real_entry_price * (1 + TAKE_PROFIT_PCT if direction == "LONG" else 1 - TAKE_PROFIT_PCT)
                    sl = real_entry_price * (1 - STOP_LOSS_PCT if direction == "LONG" else 1 + STOP_LOSS_PCT)
                    data["balance_usdt"] -= INVEST_PER_TRADE

                print(f"  🔥 СИГНАЛ {direction} НА {pair}! (Вхід: {real_entry_price}, SL: {sl}, TP: {tp})")
                data["active_trades"][pair] = {
                    "pair": pair, "direction": direction, "buy_price": real_entry_price,
                    "invested_amount": INVEST_PER_TRADE, "take_profit": tp, "stop_loss": sl,
                    "status": "OPEN", "open_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "sl_order_id": sl_order_id, "tp_order_id": tp_order_id
                }
    save_data(data)

# --- БЛОК 3: ТАЙМЕР ТА ЦИКЛ ЗАПУСКУ ---
if __name__ == "__main__":
    print("🤖 Бот запущенний.")
    try: exchange.load_markets()
    except Exception as e: print(f"⚠️ Помилка ринків: {e}")
    last_processed_minute = -1
    while True:
        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.minute != last_processed_minute:
            if now.second >= 2: 
                last_processed_minute = now.minute
                try: run_scanner_cycle()
                except Exception as main_e: print(f"🚨 Помилка в циклі: {main_e}")
        if now.minute not in [0, 15, 30, 45]: last_processed_minute = -1
        time.sleep(0.5)
