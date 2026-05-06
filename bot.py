from flask import Flask, request, jsonify
import os, time, hmac, hashlib, base64, json, uuid, requests
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

app = Flask(__name__)

API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
PASSPHRASE = os.getenv("BITGET_PASSPHRASE")

BASE_URL = "https://api.bitget.com"

SYMBOL = "ETHUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_MODE = "crossed"
MARGIN_COIN = "USDT"

DRY_RUN = False

TOTAL_SIZE = Decimal("0.02")
TP_SIZE = Decimal("0.01")

SIZE_STEP = Decimal("0.001")
PRICE_STEP = Decimal("0.01")

TP_PCT = Decimal("0.01")
SL_PCT = Decimal("0.10")


def q_price(x):
    return str(x.quantize(PRICE_STEP, rounding=ROUND_HALF_UP))


def q_size(x):
    x = (x // SIZE_STEP) * SIZE_STEP
    return str(x.quantize(SIZE_STEP, rounding=ROUND_DOWN))


def sign(timestamp, method, path, body=""):
    msg = timestamp + method.upper() + path + body
    mac = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def headers(method, path, body=""):
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign(ts, method, path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }


def bitget_post(path, payload):
    body = json.dumps(payload, separators=(",", ":"))

    if DRY_RUN:
        return {"code": "00000", "dry_run": True, "payload": payload}

    r = requests.post(
        BASE_URL + path,
        headers=headers("POST", path, body),
        data=body,
        timeout=10,
    )

    print("Bitget:", r.text)

    try:
        return r.json()
    except Exception:
        return {"code": "RAW", "raw": r.text}


def ok(res):
    return isinstance(res, dict) and res.get("code") == "00000"


def get_price():
    if DRY_RUN:
        return Decimal("2400")

    path = "/api/v2/mix/market/ticker"
    query = f"?symbol={SYMBOL}&productType={PRODUCT_TYPE}"
    full = path + query

    r = requests.get(
        BASE_URL + full,
        headers=headers("GET", full, ""),
        timeout=10,
    )

    data = r.json()
    ticker = data.get("data", [{}])[0]
    price = ticker.get("lastPr") or ticker.get("last") or ticker.get("markPrice")
    return Decimal(str(price))


def cancel_all_orders():
    return bitget_post("/api/v2/mix/order/cancel-all-orders", {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
    })


def close_position(hold_side):
    return bitget_post("/api/v2/mix/order/close-positions", {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "holdSide": hold_side,
    })


def open_market(direction):
    side = "buy" if direction == "long" else "sell"

    return bitget_post("/api/v2/mix/order/place-order", {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginMode": MARGIN_MODE,
        "marginCoin": MARGIN_COIN,
        "size": q_size(TOTAL_SIZE),
        "side": side,
        "tradeSide": "open",
        "orderType": "market",
        "clientOid": "open_" + uuid.uuid4().hex[:20],
    })


def place_tp(direction, entry):
    hold_side = "long" if direction == "long" else "short"

    price = entry * (Decimal("1") + TP_PCT) if direction == "long" else entry * (Decimal("1") - TP_PCT)

    return bitget_post("/api/v2/mix/order/place-tpsl-order", {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "planType": "profit_plan",
        "triggerPrice": q_price(price),
        "triggerType": "mark_price",
        "executePrice": "0",
        "holdSide": hold_side,
        "size": q_size(TP_SIZE),
        "clientOid": "tp1_" + uuid.uuid4().hex[:20],
    })


def place_sl(direction, entry):
    hold_side = "long" if direction == "long" else "short"

    price = entry * (Decimal("1") - SL_PCT) if direction == "long" else entry * (Decimal("1") + SL_PCT)

    return bitget_post("/api/v2/mix/order/place-tpsl-order", {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "planType": "loss_plan",
        "triggerPrice": q_price(price),
        "triggerType": "mark_price",
        "executePrice": "0",
        "holdSide": hold_side,
        "size": q_size(TOTAL_SIZE),
        "clientOid": "sl_" + uuid.uuid4().hex[:20],
    })


def run_strategy(direction):
    result = []

    target = direction
    opposite = "short" if target == "long" else "long"

    result.append({"cancel_all_orders": cancel_all_orders()})
    result.append({f"close_{opposite}": close_position(opposite)})

    time.sleep(0.5)

    open_res = open_market(target)
    result.append({"open": open_res})

    if not ok(open_res):
        return {"ok": False, "error": "open_failed", "result": result}

    entry = get_price()
    result.append({"entry_price_used": str(entry)})

    tp_res = place_tp(target, entry)
    sl_res = place_sl(target, entry)

    result.append({"tp1": tp_res})
    result.append({"sl": sl_res})

    if not ok(tp_res) or not ok(sl_res):
        emergency = close_position(target)
        result.append({"emergency_close": emergency})
        return {
            "ok": False,
            "error": "tp_or_sl_failed_emergency_closed",
            "direction": target,
            "result": result,
        }

    return {
        "ok": True,
        "direction": target,
        "total_size": str(TOTAL_SIZE),
        "tp_size": str(TP_SIZE),
        "tp_pct": str(TP_PCT),
        "sl_pct": str(SL_PCT),
        "result": result,
    }


@app.route("/", methods=["GET"])
def home():
    return "OK", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print("收到:", data)

    action = data.get("action")

    if action == "buy":
        res = run_strategy("long")
    elif action == "sell":
        res = run_strategy("short")
    else:
        return jsonify({"ok": False, "error": "action must be buy or sell"}), 400

    return jsonify({
        "ok": res.get("ok"),
        "dry_run": DRY_RUN,
        "result": res,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
