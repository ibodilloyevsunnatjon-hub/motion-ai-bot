import os
import time
import base64
import threading
import requests
from flask import Flask

app = Flask(__name__)
session = requests.Session()

# Render -> Environment Variables
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


def money(value):
    return f"{int(value):,}".replace(",", " ")


def parse_packs():
    packs = {}
    for item in PACK_PRICES.split(","):
        try:
            credits, price = item.strip().split(":")
            credits, price = int(credits), int(price)
            if credits > 0 and price > 0:
                packs[credits] = price
        except (ValueError, TypeError):
            print("PACK_PRICES xatosi:", item)
    return packs


def sb_headers():
    headers = {
        "apikey": SB_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    if SB_KEY:
        headers["Authorization"] = f"Bearer {SB_KEY}"
    return headers


def send_message(chat_id, text, markup=None, html=False):
    if not BOT_TOKEN:
        print("XATO: BOT_TOKEN sozlanmagan.")
        return False

    payload = {"chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    if html:
        payload["parse_mode"] = "HTML"

    try:
        response = session.post(
            BOT_URL + "sendMessage",
            json=payload,
            timeout=20
        )
        if not response.ok:
            print("sendMessage xatosi:",
                  response.status_code, response.text[:400])
            return False
        return True
    except Exception as error:
        print("Xabar yuborish xatosi:", repr(error))
        return False


def get_credits(user_id):
    if not SB_URL or not SB_KEY:
        print("Supabase sozlanmagan.")
        return None

    try:
        response = session.get(
            USERS_URL,
            headers=sb_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "select": "user_id,credits"
            },
            timeout=15
        )

        if not response.ok:
            print("Supabase users o'qish xatosi:",
                  response.status_code, response.text[:400])
            return None

        rows = response.json()
        if rows:
            return int(rows[0].get("credits", 0))

        # Yangi foydalanuvchiga 3 ta boshlang'ich kredit
        response = session.post(
            USERS_URL,
            headers=sb_headers(),
            json={"user_id": user_id, "credits": 3},
            timeout=15
        )

        if not response.ok:
            print("Foydalanuvchi yaratish xatosi:",
                  response.status_code, response.text[:400])
            return None

        rows = response.json()
        return int(rows[0].get("credits", 3)) if rows else 3

    except Exception as error:
        print("get_credits xatosi:", repr(error))
        return None


def set_credits(user_id, value):
    try:
        response = session.patch(
            USERS_URL,
            headers=sb_headers(),
            params={"user_id": f"eq.{user_id}"},
            json={"credits": int(value)},
            timeout=15
        )
        if not response.ok:
            print("Kredit yangilash xatosi:",
                  response.status_code, response.text[:400])
            return False
        return True
    except Exception as error:
        print("set_credits xatosi:", repr(error))
        return False


def create_payment(user_id, credits, amount):
    try:
        response = session.post(
            PAYMENTS_URL,
            headers=sb_headers(),
            json={
                "user_id": int(user_id),
                "credits": int(credits),
                "amount": int(amount),
                "status": "waiting"
            },
            timeout=15
        )

        if not response.ok:
            print("To'lov yaratish xatosi:",
                  response.status_code, response.text[:500])
            return None

        rows = response.json()
        return rows[0] if rows else None

    except Exception as error:
        print("create_payment xatosi:", repr(error))
        return None


def get_waiting_payment(user_id):
    try:
        response = session.get(
            PAYMENTS_URL,
            headers=sb_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "status": "eq.waiting",
                "select": "*",
                "order": "id.desc",
                "limit": "1"
            },
            timeout=15
        )

        if not response.ok:
            print("To'lov qidirish xatosi:",
                  response.status_code, response.text[:400])
            return None

        rows = response.json()
        return rows[0] if rows else None

    except Exception as error:
        print("get_waiting_payment xatosi:", repr(error))
        return None


def update_payment(payment_id, fields, only_status):
    try:
        response = session.patch(
            PAYMENTS_URL,
            headers=sb_headers(),
            params={
                "id": f"eq.{payment_id}",
                "status": f"eq.{only_status}",
                "select": "*"
            },
            json=fields,
            timeout=15
        )

        if not response.ok:
            print("To'lov yangilash xatosi:",
                  response.status_code, response.text[:400])
            return None

        rows = response.json()
        return rows[0] if rows else None

    except Exception as error:
        print("update_payment xatosi:", repr(error))
        return None


@app.route("/")
def home():
    return "Motion AI server ishlayapti!"


@app.route("/health")
def health():
    return {
        "ok": True,
        "bot_token_configured": bool(BOT_TOKEN),
        "cloudflare_configured": bool(CF_ACCOUNT_ID and CF_API_TOKEN),
        "supabase_configured": bool(SB_URL and SB_KEY),
        "payment_card_configured": bool(ADMIN_CARD),
        "admin_configured": bool(ADMIN_ID)
    }


def show_packs(chat_id):
    packs = parse_packs()

    if not ADMIN_CARD or not ADMIN_ID or not packs:
        send_message(
            chat_id,
            "To'lov tizimi hali sozlanmagan. Admin sozlamalarni tekshirsin.",
            keyboard
        )
        print(
            "Paket sozlamalari:",
            "karta =", bool(ADMIN_CARD),
            "admin =", bool(ADMIN_ID),
            "paketlar =", bool(packs)
        )
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

    if price is None:
        send_message(chat_id, "Bunday paket topilmadi.", keyboard)
        return

    if not ADMIN_CARD or not ADMIN_ID:
        send_message(chat_id, "Karta yoki admin sozlanmagan.", keyboard)
        return

    if not SB_URL or not SB_KEY:
        send_message(chat_id, "Baza sozlanmagan. Admin sozlamalarni tekshirsin.", keyboard)
        return

    send_message(chat_id, "⏳ To'lov so'rovi yaratilmoqda...")

    payment = create_payment(user_id, credits, price)

    if not payment:
        send_message(
            chat_id,
            "To'lov so'rovini bazaga yozib bo'lmadi. "
            "Admin Supabase jadvalini va Render loglarini tekshirsin.",
            keyboard
        )
        return

    send_message(
        chat_id,
        f"✅ Tanlandi: <b>{credits} kredit</b> — "
        f"<b>{money(price)} so'm</b>\n\n"
        f"1) Shu summani quyidagi kartaga o'tkazing:\n"
        f"<code>{ADMIN_CARD}</code>\n"
        f"Karta egasi: {CARD_OWNER or 'Karta egasi'}\n\n"
        f"2) To'lov chekini rasm yoki fayl qilib shu chatga yuboring.\n\n"
        f"Chek tekshirilgach kreditlar qo'shiladi.\n"
        f"To'lov raqami: #{payment.get('id', '?')}",
        keyboard,
        html=True
    )


def handle_receipt(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    user_id = user.get("id")

    if not user_id:
        return

    photos = msg.get("photo")
    document = msg.get("document")

    if not photos and not document:
        return

    payment = get_waiting_payment(user_id)

    if not payment:
        send_message(
            chat_id,
            "Avval 💳 Kredit sotib olish tugmasini bosib, paket tanlang.",
            keyboard
        )
        return

    if photos:
        file_id = photos[-1]["file_id"]
        method = "sendPhoto"
        media_field = "photo"
    else:
        file_id = document["file_id"]
        method = "sendDocument"
        media_field = "document"

    updated = update_payment(
        payment["id"],
        {"status": "pending", "file_id": file_id},
        "waiting"
    )

    if not updated:
        send_message(
            chat_id,
            "Chekni saqlashda xato. Qayta urinib ko'ring.",
            keyboard
        )
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
            {
                "text": "✅ Tasdiqlash",
                "callback_data": f"ok:{payment['id']}"
            },
            {
                "text": "❌ Rad etish",
                "callback_data": f"no:{payment['id']}"
            }
        ]]
    }

    try:
        response = session.post(
            BOT_URL + method,
            json={
                "chat_id": ADMIN_ID,
                media_field: file_id,
                "caption": caption,
                "reply_markup": markup
            },
            timeout=25
        )

        if not response.ok:
            print("Chekni adminga yuborish xatosi:",
                  response.status_code, response.text[:400])
            send_message(
                chat_id,
                "Chek bazaga saqlandi, ammo adminga yuborilmadi. "
                "Admin sozlamalarini tekshirsin.",
                keyboard
            )
            return

    except Exception as error:
        print("Chek yuborish xatosi:", repr(error))
        send_message(chat_id, "Chekni adminga yuborishda xato.", keyboard)
        return

    send_message(
        chat_id,
        "📨 Chek adminga yuborildi. Tasdiqlanishini kuting.",
        keyboard
    )


def handle_callback(cb):
    # Telegram tugmasi yonidagi yuklanish belgisini to'xtatish
    try:
        response = session.post(
            BOT_URL + "answerCallbackQuery",
            json={"callback_query_id": cb["id"]},
            timeout=8
        )
        if not response.ok:
            print("answerCallbackQuery xatosi:",
                  response.status_code, response.text[:300])
    except Exception as error:
        print("Callback javob xatosi:", repr(error))

    data = cb.get("data", "")
    from_id = cb.get("from", {}).get("id")
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id", from_id)

    try:
        if data.startswith("buy:"):
            credits = int(data.split(":", 1)[1])
            print("Paket tanlandi:", credits, "user:", from_id)
            handle_buy(chat_id, from_id, credits)
            return

        if data.startswith(("ok:", "no:")):
            if from_id != ADMIN_ID:
                send_message(chat_id, "Bu amal faqat admin uchun.")
                return

            action, pid_text = data.split(":", 1)
            pid = int(pid_text)
            approve = action == "ok"
            new_status = "approved" if approve else "rejected"

            payment = update_payment(
                pid,
                {"status": new_status},
                "pending"
            )

            if not payment:
                send_message(
                    ADMIN_ID,
                    f"#{pid}: to'lov topilmadi yoki avval ko'rib chiqilgan."
                )
                return

            try:
                session.post(
                    BOT_URL + "editMessageReplyMarkup",
                    json={
                        "chat_id": chat_id,
                        "message_id": message.get("message_id"),
                        "reply_markup": {"inline_keyboard": []}
                    },
                    timeout=10
                )
            except Exception as error:
                print("Tugma olib tashlash xatosi:", repr(error))

            target = int(payment["user_id"])

            if approve:
                balance = get_credits(target)

                if balance is None:
                    send_message(
                        ADMIN_ID,
                        f"#{pid} to'lovi belgilandi, ammo balansni "
                        "o'qib bo'lmadi. Bazani tekshiring."
                    )
                    return

                new_balance = balance + int(payment["credits"])

                if not set_credits(target, new_balance):
                    send_message(
                        ADMIN_ID,
                        f"#{pid}: kreditlarni yozib bo'lmadi. Bazani tekshiring."
                    )
                    return

                send_message(
                    target,
                    f"✅ To'lov tasdiqlandi! "
                    f"+{payment['credits']} kredit.\n"
                    f"Hozirgi balans: {new_balance}",
                    keyboard
                )
                send_message(ADMIN_ID, f"✅ #{pid} tasdiqlandi.")

            else:
                send_message(
                    target,
                    "❌ To'lovingiz tasdiqlanmadi. Admin bilan bog'laning.",
                    keyboard
                )
                send_message(ADMIN_ID, f"❌ #{pid} rad etildi.")

    except Exception as error:
        print("Callback ishlash xatosi:", repr(error))
        send_message(
            chat_id,
            "Xatolik yuz berdi. Keyinroq qayta urinib ko'ring.",
            keyboard
        )


def create_image(prompt):
    try:
        response = session.post(
            CF_URL,
            headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            json={"prompt": prompt},
            timeout=120
        )

        if not response.ok:
            print("Cloudflare xatosi:",
                  response.status_code, response.text[:500])
            return None

        data = response.json()
        image = (data.get("result") or {}).get("image")

        if not image:
            print("Cloudflare javobida rasm topilmadi:",
                  str(data)[:500])
            return None

        return base64.b64decode(image)

    except Exception as error:
        print("create_image xatosi:", repr(error))
        return None


def send_photo(chat_id, image_bytes):
    try:
        response = session.post(
            BOT_URL + "sendPhoto",
            data={"chat_id": chat_id},
            files={
                "photo": ("motion_ai.jpg", image_bytes, "image/jpeg")
            },
            timeout=60
        )

        if not response.ok:
            print("sendPhoto xatosi:",
                  response.status_code, response.text[:400])

        return response

    except Exception as error:
        print("Rasm yuborish xatosi:", repr(error))
        return None


def handle_message(msg):
    if not msg:
        return

    chat_id = msg.get("chat", {}).get("id")
    user = msg.get("from", {})
    user_id = user.get("id")

    if not chat_id or not user_id:
        return

    if msg.get("photo") or msg.get("document"):
        handle_receipt(msg)
        return

    text = msg.get("text", "").strip()

    if not text:
        return

    if text == "/start":
        balance = get_credits(user_id)

        if balance is None:
            send_message(
                chat_id,
                "Baza bilan ulanishda xatolik. Admin sozlamalarni tekshirsin.",
                keyboard
            )
            return

        send_message(
            chat_id,
            "🎨 Motion AI ga xush kelibsiz!\n"
            f"Sizda {balance} ta kredit bor.",
            keyboard
        )
        return

    balance = get_credits(user_id)

    if balance is None:
        send_message(
            chat_id,
            "Baza bilan ulanishda xatolik. Keyinroq urinib ko'ring.",
            keyboard
        )
        return

    if text == "⭐ Kreditlarim":
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
                "Rasm qanday bo'lishini yozing "
                "(inglizcha yozsangiz natija yaxshiroq bo'lishi mumkin):",
                keyboard
            )
        else:
            send_message(
                chat_id,
                "Kreditlaringiz tugadi. 💳 Kredit sotib olish tugmasini bosing.",
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

        if not CF_ACCOUNT_ID or not CF_API_TOKEN:
            send_message(
                chat_id,
                "Cloudflare serverda sozlanmagan.",
                keyboard
            )
            return

        send_message(chat_id, "⏳ Rasm yaratilmoqda. Kuting...")

        image = create_image(text)

        if not image:
            send_message(
                chat_id,
                "Rasm yaratilmadi. Cloudflare sozlamalari va limitlarini tekshiring.",
                keyboard
            )
            return

        result = send_photo(chat_id, image)

        if result is not None and result.ok:
            if set_credits(user_id, balance - 1):
                send_message(
                    chat_id,
                    f"✅ Tayyor! Qolgan kredit: {balance - 1}",
                    keyboard
                )
            else:
                send_message(
                    chat_id,
                    "Rasm yuborildi, lekin kredit balansini yangilashda xato bo'ldi. Adminga xabar bering.",
                    keyboard
                )
        else:
            send_message(
                chat_id,
                "Telegram rasmni yubora olmadi. Keyinroq qayta urinib ko'ring.",
                keyboard
            )

    else:
        send_message(
            chat_id,
            "Menyudan tanlang yoki /start yuboring.",
            keyboard
        )


def telegram_loop():
    global offset

    if not BOT_TOKEN:
        print("XATO: BOT_TOKEN sozlanmagan!")
        return

    if not SB_URL or not SB_KEY:
        print("OGOHLANTIRISH: SB_URL yoki SB_KEY sozlanmagan!")

    try:
        response = session.get(
            BOT_URL + "deleteWebhook",
            timeout=15
        )
        print("deleteWebhook:", response.status_code, response.text[:200])
    except Exception as error:
        print("Webhook xatosi:", repr(error))

    print("Motion AI bot ishga tushdi!")

    while True:
        try:
            response = session.get(
                BOT_URL + "getUpdates",
                params={"offset": offset, "timeout": 25},
                timeout=35
            )

            if not response.ok:
                print("getUpdates HTTP xatosi:",
                      response.status_code, response.text[:300])
                time.sleep(5)
                continue

            result = response.json()

            if not result.get("ok"):
                print("Telegram xatosi:", str(result)[:400])
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                try:
                    if "callback_query" in update:
                        handle_callback(update["callback_query"])
                    elif "message" in update:
                        handle_message(update["message"])
                except Exception as error:
                    print("Update ishlash xatosi:", repr(error))

        except Exception as error:
            print("Telegram ulanish xatosi:", repr(error))
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(
        target=telegram_loop,
        daemon=True
    ).start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
