import os
import time
import base64
import threading
import requests
from flask import Flask

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

BOT_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
CF_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    "/ai/run/@cf/black-forest-labs/flux-1-schnell"
)

credits = {}
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


@app.route("/")
def home():
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

    if user_id not in credits:
        credits[user_id] = 3

    if text == "/start":
        send_message(
            chat_id,
            "🎨 Motion AI ga xush kelibsiz!\n"
            "Sizda 3 ta sinov krediti bor.",
            keyboard
        )

    elif text == "⭐ Kreditlarim":
        send_message(
            chat_id,
            f"⭐ Qolgan kredit: {credits[user_id]}",
            keyboard
        )

    elif text == "🖼️ Rasm yaratish":
        if credits[user_id] > 0:
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
                "Sinov kreditlari tugadi. Xarid tizimi hali ulanmagan."
            )

    elif text == "ℹ️ Yordam":
        send_message(
            chat_id,
            "Rasm yaratish tugmasini bosing va tavsif yuboring.",
            keyboard
        )

    elif user_id in waiting:
        waiting.discard(user_id)
        send_message(chat_id, "⏳ Rasm yaratilmoqda...")

        if not CF_ACCOUNT_ID or not CF_API_TOKEN:
            send_message(chat_id, "Cloudflare serverda sozlanmagan.")
            return

        try:
            image = create_image(text)
            if image:
                result = send_photo(chat_id, image)
                if result.ok:
                    credits[user_id] -= 1
                    send_message(
                        chat_id,
                        f"✅ Tayyor! Qolgan kredit: {credits[user_id]}",
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
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
