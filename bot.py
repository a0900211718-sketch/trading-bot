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

# 先模擬，確認都對再改 False
DRY_RUN = False

# 開倉數量：你現在先用小單測試
TOTAL_SIZE = Decimal("0.01")

# 價格與數量精度，ETHUSDT 一般可用這樣；若 Bitget 回報精度錯，再調整
PRICE_DECIMALS = Decimal("0.01")
SIZE_DECIMALS = Decimal("0.0001")

TP_LEVELS = [
    (Decimal("0.005"), Decimal("0.165")),  # TP1 +0.5%
    (Decimal("0.010"), Decimal("0.165")),  # TP2 +1%
    (Decimal("0.020"), Decimal("0.165")),  # TP3 +2%
    (Decimal("0.030"), Decimal("0.165")),  # TP4 +3%
    (Decimal("0.040"), Decimal("0.165")),  # TP5 +4%
    (Decimal("0.050"), Decimal("0.175")),  # TP6 +5%
]

SL_PCT = Decimal("0.10")  # 10%


def q_price(x: Decimal) -> str:
    return str(x.quantize(PRICE_DECIMALS, rounding=ROUND_HALF_UP))


def q_size(x: Decimal) -> str:
    return str(x.quantize(SIZE_DECIMALS, rounding=ROUND_DOWN))


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
    print("Bitget回應:", res.text)

    try:
        return res.json()
    except Exception:
        return {"raw": res.text}


def bitget_get(path, params=None):
    params = params or {}
    query = ""
    if params:
        query = "?" + "&".join([f"{k}={v}" for k, v in params.items()])

    full_path = path + query

    if DRY_RUN:
        print("DRY_RUN GET", full_path)
        return {"dry_run": True, "data": []}

    res = requests.get(
        BASE_URL + full_path,
        headers=headers("GET", full_path, ""),
        timeout=10,
    )
    print("Bitget GET回應:", res.text)

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
        # 模擬價格，正式下單時會抓交易所價格
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
    """
    取消一般委託。
    注意：TP/SL 計畫單有時要另外取消 pending trigger order。
    這版先放一般取消；如果你之後發現舊 TP/SL 還在，我再幫你補全取消計畫單。
    """
    path = "/api/v2/mix/order/cancel-all-orders"
    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
    }
    return bitget_post(path, payload)


def close_position(hold_side):
    """
    反手前先市價平倉。
    hold_side: long 或 short
    """
    path = "/api/v2/mix/order/close-positions"
    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "holdSide": hold_side,
    }
    return bitget_post(path, payload)


def open_market(direction):
    """
    direction: long 或 short
    """
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


def place_tp(direction, entry_price: Decimal, index: int, pct: Decimal, ratio: Decimal):
    """
    用 Bitget TP/SL plan order 掛分批 TP。
    Bitget v2 endpoint: /api/v2/mix/order/place-tpsl-order
    planType profit_plan = 止盈，loss_plan = 止損。
    """
    path = "/api/v2/mix/order/place-tpsl-order"

    hold_side = "long" if direction == "long" else "short"

    if direction == "long":
        trigger_price = entry_price * (Decimal("1") + pct)
    else:
        trigger_price = entry_price * (Decimal("1") - pct)

    size = TOTAL_SIZE * ratio

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
    """
    direction = long 或 short
    """
    results = []

    # 1. 取消舊單
    results.append({"cancel_all_orders": cancel_all_orders()})

    # 2. 反手：先平反向倉
    if direction == "long":
        results.append({"close_short": close_position("short")})
    else:
        results.append({"close_long": close_position("long")})

    # 3. 開新倉
    open_result = open_market(direction)
    results.append({"open": open_result})

    # 4. 取得價格作為 TP/SL 參考
    # 實盤最準要抓成交均價；這版先用最新價。下一版可改成查成交均價。
    entry_price = get_last_price()
    results.append({"entry_price_used": str(entry_price)})

    # 5. 掛 6 張 TP
    for i, (pct, ratio) in enumerate(TP_LEVELS, start=1):
        results.append({
            f"tp{i}": place_tp(direction, entry_price, i, pct, ratio)
        })

    # 6. 掛 SL
    results.append({"sl": place_sl(direction, entry_price)})

    return results


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
        return jsonify({"ok": False, "error": "action must be buy or sell"}), 400

    return jsonify({"ok": True, "dry_run": DRY_RUN, "result": result})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
 
