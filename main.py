import ccxt
import json
import os
import time
import csv
from datetime import datetime

# ==========================================
# НАЛАШТУВАННЯ СКАНЕРА
# ==========================================
SCAN_MARKETS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "FET/USDT", 
    "ONDO/USDT", "NEAR/USDT", "SUI/USDT"
]

TAKE_PROFIT_PCT = 0.03      # Ціль: +3%
STOP_LOSS_PCT = 0.015       # Захист: -1.5%
VOLUME_MULTIPLIER = 2.5     # Коефіцієнт аномального об'єму
INVEST_PER_TRADE = 10.0     # Об'єм однієї угоди

if os.path.exists("/data") or os.environ.get("RENDER"): 
    DB_DIR = "/data"
    if not os.path.exists(DB_DIR):
        try:
            os.makedirs(DB_DIR, exist_ok=True)
        except Exception as e:
            print(f"⚠️ Не вдалося створити папку /data, збереження буде локальним: {e}")
            DB_DIR = "."
    DB_FILE = os.path.join(DB_DIR, "virtual_portfolio.json")
else:
    DB_DIR = "."
    DB_FILE = "virtual_portfolio.json"

# ==========================================
# МОДУЛЬ РОБОТИ З ДАНИМИ
# ==========================================
def load_data():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                db = json.load(f)
                if "active_trades" not in db:
                    db["active_trades"] = {}
                if "history" not in db:
                    db["history"] = []
                if "balance_usdt" not in db:
                    db["balance_usdt"] = 100.0
                return db
        except Exception:
            pass
    return {"balance_usdt": 100.0, "active_trades": {}, "history": []}

def save_data(data):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)

        csv_path = os.path.join(DB_DIR, "trades_history.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")

            writer.writerow(["ЗАГАЛЬНА СТАТИСТИКА"])
            writer.writerow(["Поточний баланс (Вільний + в угодах)", f"{data.get('balance_usdt', 100.0):.2f}"])
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

def format_price(price):
    if price is None: return "0.0"
    if price < 1.0: return f"{price:.8f}".rstrip('0').rstrip('.') if price > 0 else "0.0"
    return f"{price:.2f}"

# ==========================================
# ОДИН ЦИКЛ СКАНУВАННЯ
# ==========================================
def run_scanner_cycle():
    data = load_data()

    try:
        exchange = ccxt.whitebit({'enableRateLimit': True})
    except Exception as e:
        print(f"❌ Не вдалося ініціалізувати CCXT: {e}")
        return

    print(f"\n⚡ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Скан аномалій (15m)...")
    print(f"💰 Загальний капітал рахунку: {data['balance_usdt']:.2f} USDT")
    print("-" * 50)

    for pair in SCAN_MARKETS:
        # Вільний баланс — це те, що фізично є на балансі прямо зараз
        free_balance = data["balance_usdt"]
        time.sleep(1) 

        try:
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98:
                print(f"  ⚠️ [{pair}] Недостатньо свічок для аналізу")
                continue

            # Об'єм за 24 повні години
            past_volumes = [float(candle[5]) for candle in candles[:-2]]
            avg_volume_24h = sum(past_volumes) / len(past_volumes)

            # Рахуємо середній розмір свічки (High - Low) за добу для фільтрації імпульсів (ATR)
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
            print(f"  ⚠️ Помилка API для {pair}: {e}")
            continue

        # --------------------------------------------------------
        # КРОК 1: МЕНЕДЖМЕНТ ВЖЕ ВІДКРИТИХ ПОЗИЦІЙ
        # --------------------------------------------------------
        if pair in data["active_trades"]:
            trade = data["active_trades"][pair]
            direction = trade.get("direction", "LONG")
            p_in = trade['buy_price']
            invested = trade["invested_amount"]

            print(f"  ⏳ Контроль {direction} {pair}. Вхід: {format_price(p_in)} | Поточна: {format_price(current_price)}")

            closed = False
            exit_p = current_price
            reason = ""

            if direction == "LONG":
                if market["current_low"] <= trade["stop_loss"]:
                    exit_p = min(trade["stop_loss"], current_price)
                    closed = True
                    reason = "STOP_LOSS 🔴"
                elif market["current_high"] >= trade["take_profit"]:
                    exit_p = max(trade["take_profit"], current_price)
                    closed = True
                    reason = "TAKE_PROFIT 🟢"

            elif direction == "SHORT":
                if market["current_high"] >= trade["stop_loss"]:
                    exit_p = max(trade["stop_loss"], current_price)
                    closed = True
                    reason = "STOP_LOSS 🔴"
                elif market["current_low"] <= trade["take_profit"]:
                    exit_p = min(trade["take_profit"], current_price)
                    closed = True
                    reason = "TAKE_PROFIT 🟢"

            if closed:
                # Універсальна чиста математика PnL
                if direction == "LONG":
                    pnl_pct = (exit_p - p_in) / p_in
                else:
                    pnl_pct = (p_in - exit_p) / p_in

                pnl = invested * pnl_pct
                
                # Повертаємо заморожену інвестицію + чистий результат
                data["balance_usdt"] += (invested + pnl)
                
                trade["status"] = reason
                trade["exit_price"] = exit_p
                trade["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                trade["pnl"] = pnl

                data["history"].append(trade)
                del data["active_trades"][pair]
                print(f"  🏁 Позиція {pair} закрита! Результат: {pnl:+.2f} USDT. Баланс: {data['balance_usdt']:.2f}")
            else:
                p_change = ((current_price - p_in) / p_in) * 100 if direction == "LONG" else ((p_in - current_price) / p_in) * 100
                print(f"  💸 Поточний результат: {p_change:+.2f}% (Коридор: {format_price(trade['stop_loss'])} - {format_price(trade['take_profit'])})")

        # --------------------------------------------------------
        # КРОК 2: ПОШУК ТОЧОК ВХОДУ (ЛОНГ АБО ШОРТ)
        # --------------------------------------------------------
        else:
            current_volume = market["volume"]
            avg_volume = market["avg_volume_24h"]
            volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
            is_green_candle = market["close_price"] > market["open_price"]
            
            # Захист: якщо сигнальна свічка вже виросла/впала більше ніж на 3 середніх ATR — вхід пропускається
            overextended = market["confirmed_spread"] > (market["avg_atr"] * 3.0)

            print(f"  📊 [{pair}] Об'єм: {current_volume:.1f} | Базовий: {avg_volume:.1f}")

            if volume_spike:
                if overextended:
                    print(f"  🙅‍♂️ Сигнал пропущено: свічка занадто розтягнута (Різик входу на хаях).")
                elif free_balance >= INVEST_PER_TRADE:
                    ratio = current_volume / avg_volume

                    # Фізично забираємо гроші з балансу під угоду
                    data["balance_usdt"] -= INVEST_PER_TRADE

                    if is_green_candle:
                        print(f"  🔥 СИГНАЛ (LONG) НА {pair}! Об'єм х{ratio:.1f}")
                        data["active_trades"][pair] = {
                            "pair": pair,
                            "direction": "LONG",
                            "buy_price": current_price,
                            "invested_amount": INVEST_PER_TRADE,
                            "take_profit": current_price * (1 + TAKE_PROFIT_PCT),
                            "stop_loss": current_price * (1 - STOP_LOSS_PCT),
                            "status": "OPEN",
                            "open_time": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                    else:
                        print(f"  🔥 СИГНАЛ (SHORT) НА {pair}! Об'єм х{ratio:.1f}")
                        data["active_trades"][pair] = {
                            "pair": pair,
                            "direction": "SHORT",
                            "buy_price": current_price,
                            "invested_amount": INVEST_PER_TRADE,
                            "take_profit": current_price * (1 - TAKE_PROFIT_PCT),
                            "stop_loss": current_price * (1 + STOP_LOSS_PCT),
                            "status": "OPEN",
                            "open_time": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                    print(f"  🚀 Угоду відкрито по {format_price(current_price)} USDT. Інвестицію заморожено.")
                else:
                    print(f"  🙅‍♂️ Вільні USDT закінчилися (Вільний баланс: {free_balance:.2f} USDT).")
            else:
                print(f"  💤 Аномальних сплесків не виявлено.")
        print("-" * 30)

    save_data(data)

# ==========================================
# ГОЛОВНИЙ БЕЗКІНЕЧНИЙ ЦИКЛ
# ==========================================
if __name__ == "__main__":
    print("🤖 Автономний Бот-Снайпер 15m (CCXT) запущений!")
    while True:
        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.second == 5:
            try:
                run_scanner_cycle()
            except Exception as e:
                print(f"⚠️ Помилка в циклі: {e}")
            time.sleep(60)
        time.sleep(0.5)
