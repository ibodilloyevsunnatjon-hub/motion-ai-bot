import os
import time
import base64
import threading
import requests
from flask import Flask

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
SB_URL = os.environ.get("SB_URL", "").strip().rstrip("/")
SB_KEY = os.environ.get("SB_KEY", "").strip()

BOT_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
CF_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    "/ai/run/@cf/black-forest-labs/flux-1-schnell"
)
USERS_URL = f"{SB_URL}/rest/v1/users"

waiting = set()
offset = 0

keyboard = {
    "keyboard": [
        [{"text": "🖼️ Rasm yaratish"}],
        [{"text": "⭐ Kreditlarim"}],
        [{"text": "ℹ️ Yordam"}]
    ],
    "resize_keyboard": True
}


def sb_headers():
    headers = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if SB_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SB_KEY}"
    return headers


def get_credits(user_id):
    r = requests.get(
        USERS_URL,
        headers=sb_headers(),
        params={"user_id": f"eq.{user_id}", "select": "credits"},
        timeout=20
    )
    if not r.ok:
        print("Supabase o'qish xatosi:", r.status_code, r.text[:300])
        return None

    rows = r.json()
    if rows:
        return rows[0]["credits"]

    r = requests.post(
        USERS_URL,
        headers={**sb_headers(), "Prefer": "return=representation"},
        json={"user_id": user_id},
        timeout=20
    )
    if not r.ok:
        print("Supabase yozish xatosi:", r.status_code, r.text[:300])
        return None

    data = r.json()
    return data[0]["credits"] if data else 3


def set_credits(user_id, value):
    r = requests.patch(
        USERS_URL,
        headers=sb_headers(),
        params={"user_id": f"eq.{user_id}"},
        json={"credits": value},
        timeout=20
    )
    if not r.ok:
        print("Supabase yangilash xatosi:", r.status_code, r.text[:300])
        return False
    return True


@app.route("/")
def home():
    try:
        requests.get(
            USERS_URL,
            headers=sb_headers(),
            params={"select": "user_id", "limit": "1"},
            timeout=10
        )
    except Exception:
        pass
    return "Motion AI server ishlayapti!"


def send_message(chat_id, text, markup=None):
    data = {"chat_id": chat_id, "text": text}
    if markup:
        data["reply_markup"] = markup
    requests.post(
        BOT_URL + "sendMessage", json=data, timeout=30
    )


def create_image(prompt):
    response = requests.post(
        CF_URL,
        headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
        json={"prompt": prompt},
        timeout=120
    )

    if not response.ok:
        print("Cloudflare xatosi:", response.status_code,
              response.text[:500])
        return None

    data = response.json()
    image = (data.get("result") or {}).get("image")
    if not image:
        print("Javobda rasm topilmadi:", str(data)[:500])
        return None
    return base64.b64decode(image)


def send_photo(chat_id, image_bytes):
    return requests.post(
        BOT_URL + "sendPhoto",
        data={"chat_id": chat_id},
        files={
            "photo": (
                "motion_ai.jpg", image_bytes, "image/jpeg"
            )
        },
        timeout=60
    )


def handle_message(msg):
    if not msg or not msg.get("text"):
        return

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg["text"].strip()

    balance = get_credits(user_id)
    if balance is None:
        send_message(
            chat_id,
            "Baza bilan ulanishda xatolik. Birozdan keyin urinib ko‘ring."
        )
        return

    if text == "/start":
        send_message(
            chat_id,
            "🎨 Motion AI ga xush kelibsiz!\n"
            f"Sizda {balance} ta kredit bor.",
            keyboard
        )

    elif text == "⭐ Kreditlarim":
        send_message(
            chat_id,
            f"⭐ Qolgan kredit: {balance}",
            keyboard
        )

    elif text == "🖼️ Rasm yaratish":
        if balance > 0:
            waiting.add(user_id)
            send_message(
                chat_id,
                "Rasm qanday bo‘lishini yozing "
                "(inglizcha yozsangiz sifati yaxshiroq):",
                keyboard
            )
        else:
            send_message(
                chat_id,
                "Kreditlaringiz tugadi. Xarid tizimi hali ulanmagan."
            )

    elif text == "ℹ️ Yordam":
        send_message(
            chat_id,
            "Rasm yaratish tugmasini bosing va tavsif yuboring.",
            keyboard
        )

    elif user_id in waiting:
        waiting.discard(user_id)

        if balance <= 0:
            send_message(chat_id, "Kreditlaringiz tugadi.")
            return

        send_message(chat_id, "⏳ Rasm yaratilmoqda...")

        if not CF_ACCOUNT_ID or not CF_API_TOKEN:
            send_message(chat_id, "Cloudflare serverda sozlanmagan.")
            return

        try:
            image = create_image(text)
            if image:
                result = send_photo(chat_id, image)
                if result.ok:
                    new_balance = balance - 1
                    set_credits(user_id, new_balance)
                    send_message(
                        chat_id,
                        f"✅ Tayyor! Qolgan kredit: {new_balance}",
                        keyboard
                    )
                else:
                    send_message(chat_id, "Telegram rasmni yubora olmadi.")
            else:
                send_message(
                    chat_id,
                    "Rasm yaratilmadi. Birozdan keyin qayta urinib ko‘ring."
                )
        except Exception as error:
            print("Rasm xatosi:", repr(error))
            send_message(chat_id, "Xatolik yuz berdi. Keyinroq urinib ko‘ring.")

    else:
        send_message(chat_id, "Menyudan tanlang yoki /start yuboring.", keyboard)


def telegram_loop():
    global offset

    if not BOT_TOKEN:
        print("BOT_TOKEN sozlanmagan!")
        return

    if not SB_URL or not SB_KEY:
        print("SB_URL yoki SB_KEY sozlanmagan!")

    try:
        requests.get(BOT_URL + "deleteWebhook", timeout=15)
    except Exception as error:
        print("Webhook xatosi:", repr(error))

    print("Motion AI bot ishga tushdi!")

    while True:
        try:
            result = requests.get(
                BOT_URL + "getUpdates",
                params={"offset": offset, "timeout": 25},
                timeout=35
            ).json()

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                handle_message(update.get("message", {}))

        except Exception as error:
            print("Telegram ulanish xatosi:", repr(error))
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=telegram_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))9
    app.run(host="0.0.0.0", port=port)
