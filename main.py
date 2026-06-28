import ccxt
import json
import os
import time
import csv
from datetime import datetime

# ==========================================
# НАЛАШТУВАННЯ СКАНЕРА & БІРЖІ
# ==========================================
SCAN_MARKETS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "FET/USDT", 
    "ONDO/USDT", "NEAR/USDT", "SUI/USDT"
]

TAKE_PROFIT_PCT = 0.03      # Ціль: +3%
STOP_LOSS_PCT = 0.015       # Захист: -1.5%
VOLUME_MULTIPLIER = 2.5     # Коефіцієнт аномального об'єму
INVEST_PER_TRADE = 1.0      # 🔥 Об'єм однієї угоди знижено до 1.0 USDT

# ⚠️ РЕЖИМ ТЕСТУВАННЯ (DRY RUN)
# True — віртуальні торги (симуляція з реальними цінами). 
# False — РЕАЛЬНІ ТОРГИ на біржі за твоїми ключами!
DRY_RUN = True 

# ⚠️ ВСТАНОВІТЬ В True НА ОДИН ЗАПУСК, ЩОБ ПОВНІСТЮ ОЧИСТИТИ ІСТОРІЮ І ПОЧАТИ ЗО 100$
RESET_DATA = False 

# ==========================================
# ІНІЦІАЛІЗАЦІЯ API (Ключі прописані)
# ==========================================
exchange_config = {
    'apiKey': os.environ.get('WHITEBIT_API_KEY', '9dfcbc7d6c30802daf10d0bb50bf50d1'),
    'secret': os.environ.get('WHITEBIT_SECRET_KEY', '4ff8480b5bb8914e4dacf7ac40401762'),
    'enableRateLimit': True,
    'options': {
        'defaultType': 'margin' # Залишаємо стабільний режим, який не видає помилок мережі
    }
}
exchange = ccxt.whitebit(exchange_config)

# Шляхи до бази даних
if os.path.exists("/data") or os.environ.get("RENDER"): 
    DB_DIR = "/data"
    os.makedirs(DB_DIR, exist_ok=True)
    DB_FILE = os.path.join(DB_DIR, "virtual_portfolio.json")
else:
    DB_DIR = "."
    DB_FILE = "virtual_portfolio.json"

# ==========================================
# МОДУЛЬ РОБОТИ З ДАНИМИ
# ==========================================
def load_data():
    global RESET_DATA
    if RESET_DATA:
        print("🧹 Виявлено запит на очищення даних. Скидаємо портфель до 100 USDT...")
        if os.path.exists(DB_FILE):
            try: os.remove(DB_FILE)
            except: pass
        RESET_DATA = False 
        return {"balance_usdt": 100.0, "active_trades": {}, "history": []}

    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                db = json.load(f)
                if "active_trades" not in db: db["active_trades"] = {}
                if "history" not in db: db["history"] = []
                if "balance_usdt" not in db: db["balance_usdt"] = 100.0
                return db
        except Exception:
            pass
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
            writer.writerow(["Загальний капітал (Вільні + в угодах)", f"{total_equity:.2f} USDT"])
            writer.writerow(["Вільний баланс", f"{data.get('balance_usdt', 100.0):.2f} USDT"])
            writer.writerow(["Заморожено в угодах", f"{invested_now:.2f} USDT"])
            writer.writerow([])

            writer.writerow(["АКТИВНІ УГОДИ"])
            active = data.get("active_trades", {})
            if active:
                writer.writerow(["Пара", "Напрямок", "Ціна входу", "Інвестовано", "Take Profit", "Stop Loss", "Час відкриття"])
                for pair, t in active.items():
                    writer.writerow([
                        t.get("pair"), t.get("direction"), t.get("buy_price"), 
                        t.get("invested_amount"), t.get("take_profit"), t.get("stop_loss"), t.get("open_time")
                    ])
            else:
                writer.writerow(["Немає активних угод"])
            writer.writerow([])

            writer.writerow(["ІСТОРІЯ ЗАКРИТИХ УГОД"])
            history = data.get("history", [])
            if history:
                writer.writerow(["Пара", "Напрямок", "Ціна входу", "Ціна виходу", "Інвестовано", "Результат (PnL)", "Статус", "Час закриття"])
                for t in history:
                    writer.writerow([
                        t.get("pair"), t.get("direction"), t.get("buy_price"), t.get("exit_price"), 
                        t.get("invested_amount"), f"{t.get('pnl', 0):+.2f}", t.get("status"), t.get("close_time")
                    ])
            else:
                writer.writerow(["Історія порожня"])
    except Exception as e:
        print(f"❌ Помилка запису файлів: {e}")

def format_price(pair, price):
    if price is None: return "0.0"
    try:
        return exchange.price_to_precision(pair, price)
    except:
        if price < 1.0: return f"{price:.6f}".rstrip('0').rstrip('.')
        return f"{price:.2f}"

# ==========================================
# ОДИН ЦИКЛ СКАНУВАННЯ
# ==========================================
def run_scanner_cycle():
    data = load_data()

    if not DRY_RUN:
        try:
            balances = exchange.fetch_balance()
            data["balance_usdt"] = float(balances['free'].get('USDT', 0.0))
        except Exception as e:
            print(f"❌ Не вдалося отримати реальний баланс з біржі: {e}")
            return

    active_count = len(data["active_trades"])
    invested_amount = active_count * INVEST_PER_TRADE
    total_equity = data["balance_usdt"] + invested_amount

    print(f"\n⚡ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Скан аномалій (15m)... Режим: {'🤖 ТЕСТ (DRY_RUN)' if DRY_RUN else '🔥 РЕАЛ (LIVE)'}")
    print(f"💰 ЗАГАЛЬНИЙ КАПІТАЛ: {total_equity:.2f} USDT (В угодах: {invested_amount:.2f} USDT)")
    print(f"💵 Вільний баланс: {data['balance_usdt']:.2f} USDT")
    print(f"📊 Активних позицій: {active_count} із {len(SCAN_MARKETS)}")
    print("-" * 50)

    for pair in SCAN_MARKETS:
        free_balance = data["balance_usdt"]
        time.sleep(0.5)

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98:
                print(f"  ⚠️ [{pair}] Недостатньо свічок для аналізу")
                continue

            past_volumes = [float(candle[5]) for candle in candles[:-2]]
            avg_volume_24h = sum(past_volumes) / len(past_volumes)

            past_atr = [abs(float(c[2]) - float(c[3])) for c in candles[:-2]]
            avg_atr_24h = sum(past_atr) / len(past_atr)

            confirmed_candle = candles[-2]
            confirmed_spread = abs(float(confirmed_candle[2]) - float(confirmed_candle[3]))

            current_candle = candles[-1]
            current_price = float(current_candle[4])

            market = {
                "open_price": float(confirmed_candle[1]),
                "close_price": float(confirmed_candle[4]),
                "volume": float(confirmed_candle[5]),
                "avg_volume_24h": avg_volume_24h,
                "current_low": float(current_candle[3]),
                "current_high": float(current_candle[2]),
                "confirmed_spread": confirmed_spread,
                "avg_atr": avg_atr_24h
            }
        except Exception as e:
            print(f"  ⚠️ Помилка API (fetch_ohlcv) для {pair}: {e}")
            continue

        # --------------------------------------------------------
        # КРОК 1: МЕНЕДЖМЕНТ ВЖЕ ВІДКРИТИХ ПОЗИЦІЙ
        # --------------------------------------------------------
        if pair in data["active_trades"]:
            trade = data["active_trades"][pair]
            direction = trade.get("direction", "LONG")
            p_in = trade['buy_price']
            invested = trade["invested_amount"]

            print(f"  ⏳ Контроль {direction} {pair}. Вхід: {format_price(pair, p_in)} | Поточна: {format_price(pair, current_price)}")

            closed = False
            exit_p = current_price
            reason = ""

            if direction == "LONG":
                if market["current_low"] <= trade["stop_loss"]:
                    exit_p = trade["stop_loss"]
                    closed = True
                    reason = "STOP_LOSS 🔴"
                elif market["current_high"] >= trade["take_profit"]:
                    exit_p = trade["take_profit"]
                    closed = True
                    reason = "TAKE_PROFIT 🟢"

            elif direction == "SHORT":
                if market["current_high"] >= trade["stop_loss"]:
                    exit_p = trade["stop_loss"]
                    closed = True
                    reason = "STOP_LOSS 🔴"
                elif market["current_low"] <= trade["take_profit"]:
                    exit_p = trade["take_profit"]
                    closed = True
                    reason = "TAKE_PROFIT 🟢"

            if closed:
                if not DRY_RUN:
                    try:
                        print(f"  📢 [РЕАЛ] Надсилаю ордер на ЗАКРИТТЯ позиції {pair}...")
                        side = 'sell' if direction == "LONG" else 'buy'
                        amount_to_close = invested / p_in 
                        formatted_amount = exchange.amount_to_precision(pair, amount_to_close)

                        order = exchange.create_order(pair, 'market', side, formatted_amount)

                        if 'price' in order and order['price']:
                            exit_p = float(order['price'])
                        elif 'average' in order and order['average']:
                            exit_p = float(order['average'])
                        print(f"  ✅ [РЕАЛ] Ордер виконано по ціні: {format_price(pair, exit_p)}")
                    except Exception as e:
                        print(f"  ❌ [РЕАЛ] Помилка виконання ордера закриття: {e}. Переносимо спробу на наступний тік.")
                        continue

                if direction == "LONG":
                    pnl_pct = (exit_p - p_in) / p_in
                else:
                    pnl_pct = (p_in - exit_p) / p_in

                pnl = invested * pnl_pct

                if DRY_RUN:
                    data["balance_usdt"] += (invested + pnl)

                trade["status"] = reason
                trade["exit_price"] = exit_p
                trade["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                trade["pnl"] = pnl

                data["history"].append(trade)
                del data["active_trades"][pair]
                print(f"  🏁 Позиція {pair} закрита! Результат: {pnl:+.2f} USDT.")
            else:
                p_change = ((current_price - p_in) / p_in) * 100 if direction == "LONG" else ((p_in - current_price) / p_in) * 100
                print(f"  💸 Результат: {p_change:+.2f}% (СЛ: {format_price(pair, trade['stop_loss'])} | ТП: {format_price(pair, trade['take_profit'])})")

        # --------------------------------------------------------
        # КРОК 2: ПОШУК ТОЧОК ВХОДУ
        # --------------------------------------------------------
        else:
            current_volume = market["volume"]
            avg_volume = market["avg_volume_24h"]
            volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
            is_green_candle = market["close_price"] > market["open_price"]
            overextended = market["confirmed_spread"] > (market["avg_atr"] * 3.0)

            print(f"  📊 [{pair}] Об'єм: {current_volume:.1f} | Базовий: {avg_volume:.1f}")

            if volume_spike:
                if overextended:
                    print(f"  🙅‍♂️ Сигнал пропущено: свічка занадто розтягнута (Overextended).")
                elif free_balance >= INVEST_PER_TRADE:
                    ratio = current_volume / avg_volume
                    direction = "LONG" if is_green_candle else "SHORT"

                    real_entry_price = current_price

                    if not DRY_RUN:
                        try:
                            print(f"  📢 [РЕАЛ] Відкриваю {direction} ордер на {pair}...")
                            side = 'buy' if direction == "LONG" else 'sell'
                            amount_to_buy = INVEST_PER_TRADE / current_price
                            formatted_amount = exchange.amount_to_precision(pair, amount_to_buy)

                            order = exchange.create_order(pair, 'market', side, formatted_amount)

                            if 'price' in order and order['price']:
                                real_entry_price = float(order['price'])
                            elif 'average' in order and order['average']:
                                real_entry_price = float(order['average'])
                            print(f"  ✅ [РЕАЛ] Ордер відкрито успішно по ціні {format_price(pair, real_entry_price)} USDT")
                        except Exception as e:
                            print(f"  ❌ [РЕАЛ] Не вдалося відкрити ордер: {e}")
                            continue

                    if DRY_RUN:
                        data["balance_usdt"] -= INVEST_PER_TRADE

                    if direction == "LONG":
                        tp = real_entry_price * (1 + TAKE_PROFIT_PCT)
                        sl = real_entry_price * (1 - STOP_LOSS_PCT)
                    else:
                        tp = real_entry_price * (1 - TAKE_PROFIT_PCT)
                        sl = real_entry_price * (1 + STOP_LOSS_PCT)

                    print(f"  🔥 СИГНАЛ ({direction}) НА {pair}! Об'єм х{ratio:.1f}")
                    data["active_trades"][pair] = {
                        "pair": pair,
                        "direction": direction,
                        "buy_price": real_entry_price,
                        "invested_amount": INVEST_PER_TRADE,
                        "take_profit": tp,
                        "stop_loss": sl,
                        "status": "OPEN",
                        "open_time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    print(f"  🚀 Угоду зафіксовано. ТП: {format_price(pair, tp)} | СЛ: {format_price(pair, sl)}")
                else:
                    print(f"  🙅‍♂️ Недостатньо коштів на балансі (Вільний: {free_balance:.2f} USDT).")
            else:
                print(f"  💤 Аномальних сплесків не виявлено.")
        print("-" * 30)

    save_data(data)

# ==========================================
# ГОЛОВНИЙ ЦИКЛ СТАРТУ
# ==========================================
if __name__ == "__main__":
    print("🤖 Автономний Бот-Снайпер 15m запущений!")

    try:
        print("📦 Завантаження ринкових даних з біржі...")
        exchange.load_markets()
        print("✅ Дані завантажено. Бот готовий до роботи.")
    except Exception as e:
        print(f"⚠️ Не вдалося завантажити специфікації ринків: {e}")

    last_processed_minute = -1

    while True:
        now = datetime.now()

        if now.minute in [0, 15, 30, 45] and now.minute != last_processed_minute:
            if now.second >= 2:
                last_processed_minute = now.minute
                try:
                    run_scanner_cycle()
                except Exception as e:
                    print(f"⚠️ Критична помилка в циклі: {e}")

        if now.minute not in [0, 15, 30, 45]:
            last_processed_minute = -1

        time.sleep(0.5)
