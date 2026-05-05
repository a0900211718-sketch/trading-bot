from flask import Flask, request, jsonify
import os
import time
import hmac
import hashlib
import base64
import json
import requests

app = Flask(__name__)

API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
PASSPHRASE = os.getenv("BITGET_PASSPHRASE")

BASE_URL = "https://api.bitget.com"

SYMBOL = "ETHUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_MODE = "crossed"
MARGIN_COIN = "USDT"

DRY_RUN = True  # 先模擬，不真下單


def sign(timestamp, method, path, body=""):
    message = timestamp + method.upper() + path + body
    mac = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def bitget_headers(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign(timestamp, method, path, body),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }


def place_market_order(side):
    path = "/api/v2/mix/order/place-order"

    payload = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginMode": MARGIN_MODE,
        "marginCoin": MARGIN_COIN,
        "size": "0.001",
        "side": side,
        "orderType": "market"
    }

    body = json.dumps(payload, separators=(",", ":"))

    if DRY_RUN:
        print("模擬下單:", payload)
        return {"dry_run": True, "payload": payload}

    res = requests.post(
        BASE_URL + path,
        headers=bitget_headers("POST", path, body),
        data=body
    )

    print("Bitget回應:", res.text)
    return res.json()


@app.route("/", methods=["GET"])
def home():
    return "bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("收到 TradingView:", data)

    action = data.get("action")

    if action == "buy":
        result = place_market_order("buy")
    elif action == "sell":
        result = place_market_order("sell")
    else:
        return jsonify({"error": "action must be buy or sell"}), 400

    return jsonify({"ok": True, "result": result})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
