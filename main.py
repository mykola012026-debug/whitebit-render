import ccxt
import json
import os
import time
from datetime import datetime

# ==========================================
# НАЛАШТУВАННЯ СКАНЕРА
# ==========================================
SCAN_MARKETS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "FET/USDT", 
    "ONDO/USDT", "NEAR/USDT", "SUI/USDT", "PEPE/USDT"
]

TAKE_PROFIT_PCT = 0.03      # Ціль: +3%
STOP_LOSS_PCT = 0.015       # Захист: -1.5%
VOLUME_MULTIPLIER = 2.5     # Коефіцієнт аномального об'єму
INVEST_PER_TRADE = 10.0     # Об'єм однієї угоди

# Якщо на Render підключено Persistent Disk з Mount Path: /data
# файл буде зберігатися надійно. Якщо ні — створиться локально.
if os.path.exists("/data"):
    DB_FILE = "/data/virtual_portfolio.json"
else:
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
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

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
    current_frozen = len(data["active_trades"]) * INVEST_PER_TRADE
    print(f"💰 Баланс: {data['balance_usdt']:.2f} USDT | В угодах: {current_frozen:.2f} USDT")
    print("-" * 50)

    for pair in SCAN_MARKETS:
        free_balance = data["balance_usdt"] - (len(data["active_trades"]) * INVEST_PER_TRADE)
        time.sleep(1) # Захист від Rate Limit

        try:
            # 98 свічок 15-хвилинного таймфрейму (~24 години історії + запас)
            candles = exchange.fetch_ohlcv(pair, timeframe='15m', limit=98)
            if not candles or len(candles) < 98:
                print(f"  ⚠️ [{pair}] Недостатньо свічок для аналізу")
                continue

            # Середній об'єм за 24 повні закриті години (відсікаємо поточну -1 та закриту сигнальну -2)
            past_volumes = [float(candle[5]) for candle in candles[:-2]]
            avg_volume_24h = sum(past_volumes) / len(past_volumes)

            confirmed_candle = candles[-2]
            current_candle = candles[-1]
            current_price = float(current_candle[4])

            market = {
                "open_price": float(confirmed_candle[1]),
                "close_price": float(confirmed_candle[4]),
                "volume": float(confirmed_candle[5]),
                "avg_volume_24h": avg_volume_24h,
                "current_low": float(current_candle[3]),
                "current_high": float(current_candle[2])
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
            print(f"  ⏳ Контроль {direction} позиції {pair}. Вхід: {trade['buy_price']} | Поточна: {current_price}")

            closed = False
            pnl = 0.0
            exit_p = current_price
            reason = ""

            if direction == "LONG":
                if market["current_low"] <= trade["stop_loss"]:
                    exit_p = min(trade["stop_loss"], current_price)
                    pnl = -trade["invested_amount"] * ((trade["buy_price"] - exit_p) / trade["buy_price"])
                    closed = True
                    reason = "STOP_LOSS 🔴"
                elif market["current_high"] >= trade["take_profit"]:
                    exit_p = max(trade["take_profit"], current_price)
                    pnl = trade["invested_amount"] * ((exit_p - trade["buy_price"]) / trade["buy_price"])
                    closed = True
                    reason = "TAKE_PROFIT 🟢"

            elif direction == "SHORT":
                if market["current_high"] >= trade["stop_loss"]:
                    exit_p = max(trade["stop_loss"], current_price)
                    pnl = -trade["invested_amount"] * ((exit_p - trade["buy_price"]) / trade["buy_price"])
                    closed = True
                    reason = "STOP_LOSS 🔴"
                elif market["current_low"] <= trade["take_profit"]:
                    exit_p = min(trade["take_profit"], current_price)
                    pnl = trade["invested_amount"] * ((trade["buy_price"] - exit_p) / trade["buy_price"])
                    closed = True
                    reason = "TAKE_PROFIT 🟢"

            if closed:
                data["balance_usdt"] += pnl
                trade["status"] = reason
                trade["exit_price"] = exit_p
                trade["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                trade["pnl"] = pnl

                data["history"].append(trade)
                del data["active_trades"][pair]
                print(f"  🏁 Позиція {pair} закрита по {reason}! Результат: {pnl:+.2f} USDT. Баланс: {data['balance_usdt']:.2f}")
            else:
                if direction == "LONG":
                    p_change = ((current_price - trade["buy_price"]) / trade["buy_price"]) * 100
                else:
                    p_change = ((trade["buy_price"] - current_price) / trade["buy_price"]) * 100
                print(f"  💸 [{pair}] Поточний результат: {p_change:+.2f}% (Коридор: {trade['stop_loss']:.2f} - {trade['take_profit']:.2f})")

        # --------------------------------------------------------
        # КРОК 2: ПОШУК ТОЧОК ВХОДУ (ЛОНГ АБО ШОРТ)
        # --------------------------------------------------------
        else:
            current_volume = market["volume"]
            avg_volume = market["avg_volume_24h"]
            volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
            is_green_candle = market["close_price"] > market["open_price"]

            print(f"  📊 [{pair}] Об'єм (закритий): {current_volume:.1f} | Середній 24г: {avg_volume:.1f}")

            if volume_spike:
                if free_balance >= INVEST_PER_TRADE:
                    ratio = current_volume / avg_volume

                    if is_green_candle:
                        print(f"  🔥 СНАЙПЕРСЬКИЙ СИГНАЛ (LONG) НА {pair}! Об'єм вище в {ratio:.1f} разів!")
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
                        print(f"  🚀 Віртуально КУПЛЕНО (LONG) {pair} по {current_price} USDT.")

                    else:
                        print(f"  🔥 СНАЙПЕРСЬКИЙ СИГНАЛ (SHORT) НА {pair}! Об'єм вище в {ratio:.1f} разів!")
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
                        print(f"  🚀 Віртуально ПРОДАНО (SHORT) {pair} по {current_price} USDT.")
                else:
                    print(f"  🙅‍♂️ Сигнал по {pair} є, але вільні USDT закінчилися.")
            else:
                print(f"  💤 [{pair}] Аномальних сплесків не виявлено.")
        print("-" * 30)

    save_data(data)

# ==========================================
# ГОЛОВНИЙ БЕЗКІНЕЧНИЙ ЦИКЛ (ДЕМОН)
# ==========================================
if __name__ == "__main__":
    print("🤖 Автономний Бот-Снайпер 15m (CCXT) запущений на сервері!")

    while True:
        now = datetime.now()

        # Перевіряємо закриття 15-хвилинки (00, 15, 30, 45 хвилин) на 5-й секунді
        if now.minute in [0, 15, 30, 45] and now.second == 5:
            try:
                run_scanner_cycle()
            except Exception as e:
                print(f"⚠️ Помилка в циклі: {e}")
            time.sleep(60) # Спимо хвилину, щоб уникнути повторного спрацювання

        time.sleep(0.5) # Пауза, щоб не вантажити процесор сервера
