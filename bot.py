from flask import Flask, request, jsonify
import os
import time
import hmac
import hashlib
import base64
import json
import uuid
import requests
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

app = Flask(__name__)

API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
PASSPHRASE = os.getenv("BITGET_PASSPHRASE")

BASE_URL = "https://api.bitget.com"

# ===== 基本設定 =====
SYMBOL = "ETHUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_MODE = "crossed"
MARGIN_COIN = "USDT"

# 安全開關：先測試用 True，確認沒問題再改 False
DRY_RUN = True

# 總開倉數量
# 0.1 ETH 才比較能拆 6 個 TP
TOTAL_SIZE = Decimal("0.1")

# ETHUSDT 數量步進，先用 0.01
SIZE_STEP = Decimal("0.01")

# ETHUSDT 價格精度
PRICE_STEP = Decimal("0.01")

TP_LEVELS = [
    (Decimal("0.005"), Decimal("0.165")),  # TP1 0.5%
    (Decimal("0.010"), Decimal("0.165")),  # TP2 1%
    (Decimal("0.020"), Decimal("0.165")),  # TP3 2%
    (Decimal("0.030"), Decimal("0.165")),  # TP4 3%
    (Decimal("0.040"), Decimal("0.165")),  # TP5 4%
    (Decimal("0.050"), Decimal("0.175")),  # TP6 5%
]

SL_PCT = Decimal("0.10")  # 10%


def q_price(x: Decimal) -> str:
    return str(x.quantize(PRICE_STEP, rounding=ROUND_HALF_UP))


def normalize_size(size: Decimal) -> Decimal:
    return (size // SIZE_STEP) * SIZE_STEP


def q_size(size: Decimal) -> str:
    return str(normalize_size(size).quantize(SIZE_STEP, rounding=ROUND_DOWN))


def calc_tp_sizes():
    """
    前5個TP向下取到0.01，最後一個吃剩餘數量。
    避免所有TP加總小於總倉位。
    """
    sizes = []
    used = Decimal("0")

    for i, (_, ratio) in enumerate(TP_LEVELS, start=1):
        if i < len(TP_LEVELS):
            s = normalize_size(TOTAL_SIZE * ratio)
            sizes.append(s)
            used += s
        else:
            last = normalize_size(TOTAL_SIZE - used)
            sizes.append(last)

    return sizes


def precheck_sizes():
    tp_sizes = calc_tp_sizes()

    if TOTAL_SIZE < SIZE_STEP:
        return False, f"TOTAL_SIZE 太小，至少要 {SIZE_STEP}"

    for i, s in enumerate(tp_sizes, start=1):
        if s < SIZE_STEP:
            return False, f"TP{i} 數量太小：{s}，至少要 {SIZE_STEP}"

    if sum(tp_sizes) > TOTAL_SIZE:
        return False, "TP 加總大於總倉位"

    return True, {
        "total_size": str(TOTAL_SIZE),
        "tp_sizes": [str(x) for x in tp_sizes],
        "tp_sum": str(sum(tp_sizes)),
    }


def sign(timestamp, method, path, body=""):
    message = timestamp + method.upper() + path + body
    mac = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def headers(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign(timestamp, method, path, body),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }


def bitget_post(path, payload):
    body = json.dumps(payload, separators=(",", ":"))

    if DRY_RUN:
        print("DRY_RUN POST", path, payload)
        return {"dry_run": True, "path": path, "payload": payload}

    res = requests.post(
        BASE_URL + path,
        headers=headers("POST", path, body),
        data=body,
        timeout=10,
    )

    print("Bitget 回應:", res.text)

    try:
        return res.json()
    except Exception:
        return {"raw": res.text}


def get_last_price() -> Decimal:
    path = "/api/v2/mix/market/ticker"
    params = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
    }

    if DRY_RUN:
        return Decimal("2400")

    query = "?" + "&".join([f"{k}={v}" for k, v in params.items()])
    full_path = path + query

    res = requests.get(
        BASE_URL + full_path,
        headers=headers("GET", full_path, ""),
        timeout=10,
    )

    data = res.json()
    print("行情回應:", data)

    ticker = data.get("data", [{}])[0]
    price = ticker.get("lastPr") or ticker.get("last") or ticker.get("markPrice")

    return Decimal(str(price))


def cancel_all_orders():
    path = "/api/v2/mix/order/cancel-all-orders"
    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
    }
    return bitget_post(path, payload)


def close_position(hold_side):
    path = "/api/v2/mix/order/close-positions"
    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "holdSide": hold_side,
    }
    return bitget_post(path, payload)


def open_market(direction):
    path = "/api/v2/mix/order/place-order"

    side = "buy" if direction == "long" else "sell"

    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginMode": MARGIN_MODE,
        "marginCoin": MARGIN_COIN,
        "size": q_size(TOTAL_SIZE),
        "side": side,
        "orderType": "market",
        "clientOid": "open_" + uuid.uuid4().hex[:20],
    }

    return bitget_post(path, payload)


def place_tp(direction, entry_price: Decimal, index: int, pct: Decimal, size: Decimal):
    path = "/api/v2/mix/order/place-tpsl-order"

    hold_side = "long" if direction == "long" else "short"

    if direction == "long":
        trigger_price = entry_price * (Decimal("1") + pct)
    else:
        trigger_price = entry_price * (Decimal("1") - pct)

    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "planType": "profit_plan",
        "triggerPrice": q_price(trigger_price),
        "triggerType": "mark_price",
        "executePrice": "0",
        "holdSide": hold_side,
        "size": q_size(size),
        "clientOid": f"tp{index}_" + uuid.uuid4().hex[:18],
    }

    return bitget_post(path, payload)


def place_sl(direction, entry_price: Decimal):
    path = "/api/v2/mix/order/place-tpsl-order"

    hold_side = "long" if direction == "long" else "short"

    if direction == "long":
        trigger_price = entry_price * (Decimal("1") - SL_PCT)
    else:
        trigger_price = entry_price * (Decimal("1") + SL_PCT)

    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "planType": "loss_plan",
        "triggerPrice": q_price(trigger_price),
        "triggerType": "mark_price",
        "executePrice": "0",
        "holdSide": hold_side,
        "size": q_size(TOTAL_SIZE),
        "clientOid": "sl_" + uuid.uuid4().hex[:20],
    }

    return bitget_post(path, payload)


def run_strategy(direction):
    ok, check = precheck_sizes()

    if not ok:
        return {
            "ok": False,
            "error": "precheck_failed",
            "detail": check,
        }

    results = []
    tp_sizes = calc_tp_sizes()

    results.append({"precheck": check})

    # 1. 取消舊委託
    results.append({"cancel_all_orders": cancel_all_orders()})

    # 2. 反手前先平反向倉
    if direction == "long":
        results.append({"close_short": close_position("short")})
    else:
        results.append({"close_long": close_position("long")})

    # 3. 開倉
    open_result = open_market(direction)
    results.append({"open": open_result})

    # 4. 用最新價當 TP/SL 參考
    entry_price = get_last_price()
    results.append({"entry_price_used": str(entry_price)})

    # 5. 掛 6 個 TP
    for i, ((pct, _), size) in enumerate(zip(TP_LEVELS, tp_sizes), start=1):
        results.append({
            f"tp{i}": place_tp(direction, entry_price, i, pct, size)
        })

    # 6. 掛 SL
    results.append({"sl": place_sl(direction, entry_price)})

    return {
        "ok": True,
        "direction": direction,
        "result": results,
    }


@app.route("/", methods=["GET"])
def home():
    return "bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print("收到 TradingView:", data)

    action = data.get("action")

    if action == "buy":
        result = run_strategy("long")
    elif action == "sell":
        result = run_strategy("short")
    else:
        return jsonify({
            "ok": False,
            "error": "action must be buy or sell"
        }), 400

    return jsonify({
        "ok": True,
        "dry_run": DRY_RUN,
        "result": result
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
