import os
import json
import time
import threading
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CONTROL_CHAT_ID = os.environ.get("CONTROL_CHAT_ID")
PENDING_ACCOUNTS_PATH = os.environ.get("PENDING_ACCOUNTS_PATH", "pending_accounts.json")
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "change-me")  # shared secret between Render and VPS

_lock = threading.Lock()


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


def tg_send(text: str, reply_markup=None):
    if not BOT_TOKEN or not CONTROL_CHAT_ID:
        return
    payload = {"chat_id": CONTROL_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print("tg_send error:", e)


def tg_answer_callback(callback_query_id: str, text: str = ""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def tg_edit_message(chat_id, message_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def notify_admin(entry_id: str, entry: dict):
    """Sends a Telegram message with a tappable APPROVE/REJECT button — no typing needed."""
    text = (
        f"🆕 NIEUWE ACCOUNT AANVRAAG\n\n"
        f"Naam: {entry['name']}\n"
        f"Telegram: {entry['telegram']}\n"
        f"E-mail: {entry['email']}\n\n"
        f"MT5 login: {entry['mt5_login']}\n"
        f"Server: {entry['mt5_server']}\n"
        f"Lot/leg: {entry['lot_per_leg']}\n"
        f"Funded mode: {'JA' if entry['funded_mode'] else 'NEE'}\n\n"
        f"⚠️ Wachtwoord staat veilig opgeslagen — niet in dit bericht."
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Goedkeuren", "callback_data": f"approve:{entry_id}"},
            {"text": "❌ Afwijzen", "callback_data": f"reject:{entry_id}"},
        ]]
    }
    tg_send(text, keyboard)


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
        "mt5_password": data["mt5_password"],
        "mt5_server": data["mt5_server"].strip(),
        "lot_per_leg": lot_per_leg,
        "funded_mode": bool(data.get("funded_mode", False)),
        "submitted_at": time.time(),
        "status": "pending",  # pending -> approved -> synced   (or rejected)
    }

    with _lock:
        pending = load_pending()
        pending[entry_id] = entry
        save_pending(pending)

    notify_admin(entry_id, entry)
    return jsonify({"ok": True})


# ── Sync API used by the VPS bot to pick up approved accounts ──
def _check_sync_auth():
    token = request.headers.get("X-Sync-Token", "")
    return token == SYNC_TOKEN


@app.route("/approved-accounts", methods=["GET"])
def approved_accounts():
    if not _check_sync_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    with _lock:
        pending = load_pending()
    result = {k: v for k, v in pending.items() if v.get("status") == "approved"}
    return jsonify({"ok": True, "accounts": result})


@app.route("/mark-synced/<entry_id>", methods=["POST"])
def mark_synced(entry_id):
    if not _check_sync_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    with _lock:
        pending = load_pending()
        if entry_id in pending:
            pending[entry_id]["status"] = "synced"
            save_pending(pending)
    return jsonify({"ok": True})


# ── Telegram callback (button tap) polling — runs in background thread ──
def callback_poll_loop():
    last_update_id = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"timeout": 25, "offset": last_update_id + 1, "allowed_updates": '["callback_query"]'},
                timeout=35,
            ).json()

            if not r.get("ok"):
                time.sleep(2)
                continue

            for upd in r.get("result", []):
                last_update_id = max(last_update_id, int(upd.get("update_id", 0)))
                cq = upd.get("callback_query")
                if not cq:
                    continue

                data = cq.get("data", "")
                cq_id = cq.get("id")
                msg = cq.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                message_id = msg.get("message_id")

                if ":" not in data:
                    continue
                action, entry_id = data.split(":", 1)

                with _lock:
                    pending = load_pending()
                    entry = pending.get(entry_id)

                    if not entry:
                        tg_answer_callback(cq_id, "Niet gevonden.")
                        continue

                    if action == "approve":
                        entry["status"] = "approved"
                        pending[entry_id] = entry
                        save_pending(pending)
                        tg_answer_callback(cq_id, "Goedgekeurd!")
                        tg_edit_message(chat_id, message_id, f"✅ GOEDGEKEURD\n\nMT5 login: {entry['mt5_login']}\nWordt automatisch gesynchroniseerd naar de bot...")
                    elif action == "reject":
                        entry["status"] = "rejected"
                        pending[entry_id] = entry
                        save_pending(pending)
                        tg_answer_callback(cq_id, "Afgewezen.")
                        tg_edit_message(chat_id, message_id, f"❌ AFGEWEZEN\n\nMT5 login: {entry['mt5_login']}")

        except Exception as e:
            print("callback_poll_loop error:", e)
            time.sleep(3)


@app.route("/")
def index():
    return send_from_directory("static", "onboarding.html")


if __name__ == "__main__":
    if BOT_TOKEN:
        t = threading.Thread(target=callback_poll_loop, daemon=True)
        t.start()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
