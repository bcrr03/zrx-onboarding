import os
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CONTROL_CHAT_ID = os.environ.get("CONTROL_CHAT_ID")
PENDING_ACCOUNTS_PATH = os.environ.get("PENDING_ACCOUNTS_PATH", "pending_accounts.json")


def load_pending():
    if not os.path.exists(PENDING_ACCOUNTS_PATH):
        return {}
    try:
        with open(PENDING_ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pending(data):
    with open(PENDING_ACCOUNTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def notify_admin(entry_id: str, entry: dict):
    """Sends a Telegram message to the control group with the new signup details."""
    if not BOT_TOKEN or not CONTROL_CHAT_ID:
        return
    text = (
        f"🆕 NIEUWE ACCOUNT AANVRAAG\n\n"
        f"ID: {entry_id}\n"
        f"Naam: {entry['name']}\n"
        f"Telegram: {entry['telegram']}\n"
        f"E-mail: {entry['email']}\n\n"
        f"MT5 login: {entry['mt5_login']}\n"
        f"Server: {entry['mt5_server']}\n"
        f"Lot/leg: {entry['lot_per_leg']}\n"
        f"Funded mode: {'JA' if entry['funded_mode'] else 'NEE'}\n\n"
        f"⚠️ Wachtwoord staat veilig in pending_accounts.json — niet in dit bericht.\n\n"
        f"Voeg toe met:\n/approveaccount {entry_id}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CONTROL_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print("notify_admin error:", e)


@app.route("/submit-account", methods=["POST"])
def submit_account():
    data = request.json or {}

    required = ["name", "telegram", "email", "mt5_login", "mt5_password", "mt5_server", "lot_per_leg"]
    missing = [f for f in required if not data.get(f) and data.get(f) != 0]
    if missing:
        return jsonify({"ok": False, "error": f"Ontbrekende velden: {', '.join(missing)}"}), 400

    try:
        mt5_login = int(data["mt5_login"])
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "MT5 login moet een getal zijn"}), 400

    try:
        lot_per_leg = float(data["lot_per_leg"])
        if lot_per_leg <= 0 or lot_per_leg > 100:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Ongeldige lotsize"}), 400

    entry_id = f"req_{int(time.time())}_{mt5_login}"

    entry = {
        "name": data["name"].strip(),
        "telegram": data["telegram"].strip(),
        "email": data["email"].strip(),
        "mt5_login": mt5_login,
        "mt5_password": data["mt5_password"],  # stored, never sent back in messages
        "mt5_server": data["mt5_server"].strip(),
        "lot_per_leg": lot_per_leg,
        "funded_mode": bool(data.get("funded_mode", False)),
        "submitted_at": time.time(),
        "status": "pending",
    }

    pending = load_pending()
    pending[entry_id] = entry
    save_pending(pending)

    notify_admin(entry_id, entry)

    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory("static", "onboarding.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
