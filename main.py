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
INVEST_PER_TRADE = 1.0      # 🔥 Об'єм однієї угоди знижено до 1.0 USDT за запитом

# ⚠️ РЕЖИМ ТЕСТУВАННЯ (DRY RUN)
# Змініть на False, коли будете готові до реальних торгів на реальні гроші
DRY_RUN = True 

# ⚠️ ВСТАНОВІТЬ В True НА ОДИН ЗАПУСК, ЩОБ ПОВНІСТЮ ОЧИСТИТИ ІСТОРІЮ В ЛОГАХ
RESET_DATA = False 

# ==========================================
# ІНІЦІАЛІЗАЦІЯ API
# ==========================================
exchange_config = {
    'apiKey': os.environ.get('WHITEBIT_API_KEY', '9dfcbc7d6c30802daf10d0bb50bf50d1'),
    'secret': os.environ.get('WHITEBIT_SECRET_KEY', ''), # Сюди встав свій секретний ключ, якщо тестуєш локально
    'enableRateLimit': True,
    'options': {
        'defaultType': 'spot' # Тільки спотовий ринок (LONG позиції)
    }
}
exchange = ccxt.whitebit(exchange_config)

# Шлях до бази даних з урахуванням специфіки Render
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
        print("🧹 Виявлено запит на очищення даних. Скидаємо лог до 100 USDT...")
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
            writer.writerow(["Загальний капітал", f"{total_equity:.2f} USDT"])
            writer.writerow(["Вільний баланс", f"{data.get('balance_usdt', 100.0):.2f} USDT"])
            writer.writerow(["Заморожено в угодах", f"{invested_now:.2f} USDT"])
            writer.writerow([])

            writer.writerow(["АКТИВНІ УГОДИ"])
            active = data.get("active_trades", {})
            if active:
                writer.writerow(["Пара", "Напрямок", "Ціна входу", "Кількість монет", "Інвестовано", "Take Profit", "Stop Loss", "Час відкриття"])
                for pair, t in active.items():
                    writer.writerow([
                        t.get("pair"), t.get("direction"), t.get("buy_price"), t.get("amount"),
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
                        t.get("invested_amount"), f"{t.get('pnl', 0):+.4f}", t.get("status"), t.get("close_time")
                    ])
    except Exception as e:
        print(f"❌ Помилка запису файлів БД: {e}")

def format_price(pair, price):
    if price is None: return "0.0"
    try: return exchange.price_to_precision(pair, price)
    except: return f"{price:.4f}"

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
            print(f"❌ Не вдалося отримати реальний баланс з WhiteBIT: {e}")
            return

    active_count = len(data["active_trades"])
    invested_amount = sum(t.get("invested_amount", 0) for t in data["active_trades"].values())
    total_equity = data["balance_usdt"] + invested_amount

    print(f"\n⚡ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Скан аномалій (15m)... Режим: {'🤖 ТЕСТ (DRY_RUN)' if DRY_RUN else '🔥 РЕАЛ (LIVE)'}")
    print(f"💰 КАПІТАЛ: {total_equity:.2f} USDT | Вільний: {data['balance_usdt']:.2f} USDT | В угодах: {invested_amount:.2f} USDT")
    print("-" * 50)

    for pair in SCAN_MARKETS:
        free_balance = data["balance_usdt"]
        time.sleep(0.5) 

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98:
                print(f"  ⚠️ [{pair}] Недостатньо свічок для аналізу.")
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
        except ccxt.NetworkError as e:
            print(f"  ❌ Помилка мережі для {pair} (можливо, ліміт IP на Render): {e}")
            continue
        except ccxt.ExchangeError as e:
            print(f"  ❌ Помилка біржі WhiteBIT для {pair}: {e}")
            continue
        except Exception as e:
            print(f"  ⚠️ Загальна помилка API (fetch_ohlcv) для {pair}: {e}")
            continue

        # Лог стану маркету (щоб бачити, що монета просканована успішно)
        print(f"  📊 [{pair}] Поточна ціна: {format_price(pair, current_price)} | Об'єм: {market['volume']:.1f} (Базовий: {market['avg_volume_24h']:.1f})")

        # --------------------------------------------------------
        # КРОК 1: МЕНЕДЖМЕНТ ВЖЕ ВІДКРИТИХ ПОЗИЦІЙ (LONG)
        # --------------------------------------------------------
        if pair in data["active_trades"]:
            trade = data["active_trades"][pair]
            p_in = trade['buy_price']
            coins_amount = trade.get("amount", 0.0)

            print(f"    ⏳ Контроль позиції {pair}. Вхід: {format_price(pair, p_in)}")

            closed = False
            exit_p = current_price
            reason = ""

            if market["current_low"] <= trade["stop_loss"]:
                exit_p = trade["stop_loss"]
                closed = True
                reason = "STOP_LOSS 🔴"
            elif market["current_high"] >= trade["take_profit"]:
                exit_p = trade["take_profit"]
                closed = True
                reason = "TAKE_PROFIT 🟢"

            if closed:
                if not DRY_RUN:
                    try:
                        print(f"    📢 [РЕАЛ] Надсилаю ордер на ПРОДАЖ {pair}...")
                        formatted_amount = exchange.amount_to_precision(pair, coins_amount)
                        
                        order = exchange.create_order(pair, 'market', 'sell', formatted_amount)
                        
                        if 'average' in order and order['average']:
                            exit_p = float(order['average'])
                        elif 'price' in order and order['price']:
                            exit_p = float(order['price'])
                        print(f"    ✅ [РЕАЛ] Продано успішно по ціні: {format_price(pair, exit_p)}")
                    except Exception as e:
                        print(f"    ❌ [РЕАЛ] Помилка закриття позиції: {e}. Спробуємо на наступному кроці.")
                        continue 

                pnl_pct = (exit_p - p_in) / p_in
                pnl = trade["invested_amount"] * pnl_pct

                if DRY_RUN:
                    data["balance_usdt"] += (trade["invested_amount"] + pnl)

                trade["status"] = reason
                trade["exit_price"] = exit_p
                trade["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                trade["pnl"] = pnl

                data["history"].append(trade)
                del data["active_trades"][pair]
                print(f"    🏁 Позиція {pair} закрита. Результат: {pnl:+.4f} USDT.")
            else:
                p_change = ((current_price - p_in) / p_in) * 100
                print(f"    💸 Нефіксований PnL: {p_change:+.2f}% (СЛ: {format_price(pair, trade['stop_loss'])} | ТП: {format_price(pair, trade['take_profit'])})")

        # --------------------------------------------------------
        # КРОК 2: ПОШУК ТОЧОК ВХОДУ (ТІЛЬКИ LONG)
        # --------------------------------------------------------
        else:
            volume_spike = market["volume"] >= (market["avg_volume_24h"] * VOLUME_MULTIPLIER)
            is_green_candle = market["close_price"] > market["open_price"]
            overextended = market["confirmed_spread"] > (market["avg_atr"] * 3.0)

            if volume_spike and is_green_candle:
                if overextended:
                    print(f"    🙅‍♂️ Сигнал пропущено: свічка занадто розтягнута (Overextended).")
                elif free_balance >= INVEST_PER_TRADE:
                    real_entry_price = current_price
                    executed_amount = INVEST_PER_TRADE / current_price 

                    if not DRY_RUN:
                        try:
                            print(f"    📢 [РЕАЛ] Купівля {pair} на суму {INVEST_PER_TRADE} USDT...")
                            order = exchange.create_order(pair, 'market', 'buy', amount=None, price=None, params={'cost': INVEST_PER_TRADE})
                            
                            if 'average' in order and order['average']:
                                real_entry_price = float(order['average'])
                            elif 'price' in order and order['price']:
                                real_entry_price = float(order['price'])
                                
                            if 'filled' in order and order['filled']:
                                executed_amount = float(order['filled'])
                            else:
                                executed_amount = INVEST_PER_TRADE / real_entry_price
                                
                            print(f"    ✅ [РЕАЛ] Ордер виконано. Куплено {executed_amount} монет по {format_price(pair, real_entry_price)}")
                        except Exception as e:
                            print(f"    ❌ [РЕАЛ] Не вдалося відкрити ордер купівлі: {e}")
                            continue

                    if DRY_RUN:
                        data["balance_usdt"] -= INVEST_PER_TRADE

                    tp = real_entry_price * (1 + TAKE_PROFIT_PCT)
                    sl = real_entry_price * (1 - STOP_LOSS_PCT)

                    data["active_trades"][pair] = {
                        "pair": pair,
                        "direction": "LONG",
                        "buy_price": real_entry_price,
                        "amount": executed_amount, 
                        "invested_amount": INVEST_PER_TRADE,
                        "take_profit": tp,
                        "stop_loss": sl,
                        "status": "OPEN",
                        "open_time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    print(f"    🚀 Угоду зафіксовано в системі. ТП: {format_price(pair, tp)} | СЛ: {format_price(pair, sl)}")
                else:
                    print(f"    🙅‍♂️ Недостатньо балансу для входу (Вільний: {free_balance:.2f} USDT).")
            else:
                print(f"    💤 Аномальних сплесків не виявлено.")
        print("-" * 30)

    save_data(data)

# ==========================================
# ГОЛОВНИЙ АВТОНОМНИЙ ЦИКЛ
# ==========================================
if __name__ == "__main__":
    print("🤖 Автономний Бот-Снайпер 15m (Тільки LONG) запущений!")
    
    try:
        print("📦 Завантаження ринкових специфікацій з WhiteBIT...")
        exchange.load_markets()
        print("✅ Маркети завантажено. Бот готовий до роботи.")
    except Exception as e:
        print(f"⚠️ Помилка завантаження ринків: {e}")

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
