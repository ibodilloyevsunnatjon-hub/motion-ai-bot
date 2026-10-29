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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
ADMIN_CARD = os.environ.get("ADMIN_CARD", "").strip()
CARD_OWNER = os.environ.get("CARD_OWNER", "").strip()
PACK_PRICES = os.environ.get(
    "PACK_PRICES", "10:10000,30:25000,100:70000"
).strip()

BOT_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
CF_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    "/ai/run/@cf/black-forest-labs/flux-1-schnell"
)
USERS_URL = f"{SB_URL}/rest/v1/users"
PAYMENTS_URL = f"{SB_URL}/rest/v1/payments"

waiting = set()
offset = 0

keyboard = {
    "keyboard": [
        [{"text": "🖼️ Rasm yaratish"}],
        [{"text": "⭐ Kreditlarim"}, {"text": "💳 Kredit sotib olish"}],
        [{"text": "ℹ️ Yordam"}]
    ],
    "resize_keyboard": True
}


def parse_packs():
    packs = {}
    for item in PACK_PRICES.split(","):
        try:
            credits, price = item.strip().split(":")
            packs[int(credits)] = int(price)
        except Exception:
            continue
    return packs


def money(value):
    return f"{value:,}".replace(",", " ")


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


def create_payment(user_id, credits, amount):
    r = requests.post(
        PAYMENTS_URL,
        headers={**sb_headers(), "Prefer": "return=representation"},
        json={"user_id": user_id, "credits": credits, "amount": amount},
        timeout=20
    )
    if not r.ok:
        print("To'lov yaratish xatosi:", r.status_code, r.text[:300])
        return None
    data = r.json()
    return data[0] if data else None


def get_waiting_payment(user_id):
    r = requests.get(
        PAYMENTS_URL,
        headers=sb_headers(),
        params={
            "user_id": f"eq.{user_id}",
            "status": "eq.waiting",
            "order": "id.desc",
            "limit": "1"
        },
        timeout=20
    )
    if not r.ok:
        print("To'lov o'qish xatosi:", r.status_code, r.text[:300])
        return None
    rows = r.json()
    return rows[0] if rows else None


def update_payment(payment_id, fields, only_status):
    r = requests.patch(
        PAYMENTS_URL,
        headers={**sb_headers(), "Prefer": "return=representation"},
        params={"id": f"eq.{payment_id}", "status": f"eq.{only_status}"},
        json=fields,
        timeout=20
    )
    if not r.ok:
        print("To'lov yangilash xatosi:", r.status_code, r.text[:300])
        return None
    rows = r.json()
    return rows[0] if rows else None


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


def send_message(chat_id, text, markup=None, html=False):
    data = {"chat_id": chat_id, "text": text}
    if markup:
        data["reply_markup"] = markup
    if html:
        data["parse_mode"] = "HTML"
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


def show_packs(chat_id):
    packs = parse_packs()
    if not ADMIN_CARD or not ADMIN_ID or not packs:
        send_message(chat_id, "To'lov tizimi hali sozlanmagan.")
        return

    buttons = []
    for credits, price in sorted(packs.items()):
        buttons.append([{
            "text": f"{credits} kredit — {money(price)} so'm",
            "callback_data": f"buy:{credits}"
        }])

    send_message(
        chat_id,
        "💳 Paketni tanlang (1 kredit = 1 ta rasm):",
        {"inline_keyboard": buttons}
    )


def handle_buy(chat_id, user_id, credits):
    packs = parse_packs()
    price = packs.get(credits)
    if not price:
        send_message(chat_id, "Bunday paket topilmadi.")
        return

    payment = create_payment(user_id, credits, price)
    if not payment:
        send_message(chat_id, "Xatolik yuz berdi. Keyinroq urinib ko‘ring.")
        return

    send_message(
        chat_id,
        f"✅ Tanlandi: <b>{credits} kredit</b> — "
        f"<b>{money(price)} so'm</b>\n\n"
        f"1) Shu summani quyidagi kartaga o'tkazing:\n"
        f"<code>{ADMIN_CARD}</code>\n"
        f"Karta egasi: {CARD_OWNER}\n\n"
        f"2) To'lov chekini (rasm yoki skrinshot) shu yerga yuboring.\n\n"
        f"Chek tekshirilgach kreditlar qo'shiladi.",
        None,
        html=True
    )


def handle_receipt(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    user_id = user["id"]

    payment = get_waiting_payment(user_id)
    if not payment:
        send_message(
            chat_id,
            "Avval 💳 Kredit sotib olish tugmasi orqali paketni tanlang."
        )
        return

    photos = msg.get("photo")
    if photos:
        file_id = photos[-1]["file_id"]
        method = "sendPhoto"
        field = "photo"
    else:
        file_id = msg["document"]["file_id"]
        method = "sendDocument"
        field = "document"

    updated = update_payment(
        payment["id"], {"status": "pending", "file_id": file_id}, "waiting"
    )
    if not updated:
        send_message(chat_id, "Xatolik yuz berdi. Keyinroq urinib ko‘ring.")
        return

    username = user.get("username")
    who = f"@{username}" if username else str(user_id)
    caption = (
        f"💳 Yangi to'lov #{payment['id']}\n"
        f"Foydalanuvchi: {who} (ID {user_id})\n"
        f"Paket: {payment['credits']} kredit\n"
        f"Summa: {money(payment['amount'])} so'm"
    )
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Tasdiqlash", "callback_data": f"ok:{payment['id']}"},
            {"text": "❌ Rad etish", "callback_data": f"no:{payment['id']}"}
        ]]
    }
    requests.post(
        BOT_URL + method,
        json={
            "chat_id": ADMIN_ID,
            field: file_id,
            "caption": caption,
            "reply_markup": markup
        },
        timeout=30
    )
    send_message(
        chat_id,
        "📨 Chek adminga yuborildi. Tasdiqlanishini kuting.",
        keyboard
    )


def handle_callback(cb):
    requests.post(
        BOT_URL + "answerCallbackQuery",
        json={"callback_query_id": cb["id"]},
        timeout=15
    )

    data = cb.get("data", "")
    from_id = cb["from"]["id"]
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id", from_id)

    if data.startswith("buy:"):
        try:
            credits = int(data.split(":")[1])
        except ValueError:
            return
        handle_buy(chat_id, from_id, credits)
        return

    if data.startswith(("ok:", "no:")):
        if from_id != ADMIN_ID:
            return

        action, pid = data.split(":")
        approve = action == "ok"
        new_status = "approved" if approve else "rejected"
        payment = update_payment(int(pid), {"status": new_status}, "pending")

        requests.post(
            BOT_URL + "editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "reply_markup": {"inline_keyboard": []}
            },
            timeout=15
        )

        if not payment:
            send_message(
                ADMIN_ID, f"#{pid}: bu to'lov allaqachon ko'rib chiqilgan."
            )
            return

        target = payment["user_id"]
        if approve:
            balance = get_credits(target) or 0
            new_balance = balance + payment["credits"]
            set_credits(target, new_balance)
            send_message(
                target,
                f"✅ To'lov tasdiqlandi! +{payment['credits']} kredit.\n"
                f"Hozirgi balans: {new_balance}",
                keyboard
            )
            send_message(ADMIN_ID, f"✅ #{pid} tasdiqlandi.")
        else:
            send_message(
                target,
                "❌ To'lovingiz tasdiqlanmadi. Muammo bo'lsa, admin bilan "
                "bog'laning.",
                keyboard
            )
            send_message(ADMIN_ID, f"❌ #{pid} rad etildi.")


def handle_message(msg):
    if not msg:
        return

    if msg.get("photo") or msg.get("document"):
        handle_receipt(msg)
        return

    if not msg.get("text"):
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

    elif text == "💳 Kredit sotib olish":
        show_packs(chat_id)

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
                "Kreditlaringiz tugadi. 💳 Kredit sotib olish tugmasini "
                "bosing.",
                keyboard
            )

    elif text == "ℹ️ Yordam":
        send_message(
            chat_id,
            "Rasm yaratish tugmasini bosing va tavsif yuboring. "
            "Kredit tugasa, 💳 Kredit sotib olish orqali to'ldiring.",
            keyboard
        )

    elif user_id in waiting:
        waiting.discard(user_id)

        if balance <= 0:
            send_message(chat_id, "Kreditlaringiz tugadi.", keyboard)
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
                try:
                    if "callback_query" in update:
                        handle_callback(update["callback_query"])
                    else:
                        handle_message(update.get("message", {}))
                except Exception as error:
                    print("Xabar xatosi:", repr(error))

        except Exception as error:
            print("Telegram ulanish xatosi:", repr(error))
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=telegram_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
