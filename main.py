import os
import time
import json
import base64
import logging
import threading
import requests

from flask import Flask, jsonify

# =========================
# LOG VA ASOSIY SOZLAMALAR
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("MotionAI")
app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
SB_URL = os.getenv("SB_URL", "").strip().rstrip("/")
SB_KEY = os.getenv("SB_KEY", "").strip()

ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
ADMIN_CARD = os.getenv("ADMIN_CARD", "").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "").strip()

PACK_PRICES = os.getenv(
    "PACK_PRICES",
    "10:10000,30:25000,100:70000"
)

TELEGRAM_URL = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    if BOT_TOKEN else ""
)

if SB_URL and not SB_URL.endswith("/rest/v1"):
    SB_API = SB_URL + "/rest/v1"
else:
    SB_API = SB_URL

# Foydalanuvchi kutayotgan amal
USER_STATE = {}

# Kredit paketlari
def load_packs():
    result = []

    for item in PACK_PRICES.split(","):
        try:
            credits, amount = item.strip().split(":", 1)
            credits = int(credits)
            amount = int(amount)

            if credits > 0 and amount > 0:
                result.append((credits, amount))

        except (ValueError, TypeError):
            log.warning("Noto'g'ri paket sozlamasi: %s", item)

    return result or [
        (10, 10000),
        (30, 25000),
        (100, 70000)
    ]


PACKS = load_packs()


# =========================
# TELEGRAM API
# =========================

def tg_call(method, payload=None, files=None, timeout=40):
    if not BOT_TOKEN:
        raise RuntimeError("Render'da BOT_TOKEN kiritilmagan")

    url = f"{TELEGRAM_URL}/{method}"

    if files:
        response = requests.post(
            url,
            data=payload or {},
            files=files,
            timeout=timeout
        )
    else:
        response = requests.post(
            url,
            json=payload or {},
            timeout=timeout
        )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Telegram javobi JSON emas: HTTP {response.status_code}"
        )

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram {method} xatosi: "
            f"HTTP {response.status_code}; "
            f"{str(data)[:500]}"
        )

    return data.get("result")


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": str(text)
    }

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return tg_call("sendMessage", payload)


def send_image_bytes(chat_id, image_bytes, caption=""):
    payload = {
        "chat_id": chat_id,
        "caption": caption
    }

    files = {
        "photo": (
            "motion-ai.jpg",
            image_bytes,
            "image/jpeg"
        )
    }

    return tg_call(
        "sendPhoto",
        payload,
        files=files,
        timeout=60
    )


def answer_callback(callback_id):
    # Telegram tugmasidagi aylanishni to'xtatish
    try:
        tg_call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id},
            timeout=15
        )
    except Exception:
        log.exception("Callback javobini yuborib bo'lmadi")


# =========================
# SUPABASE API
# =========================

def sb_request(
    method,
    path,
    params=None,
    body=None,
    prefer=None
):
    if not SB_URL or not SB_KEY:
        raise RuntimeError(
            "Render'da SB_URL yoki SB_KEY kiritilmagan"
        )

    headers = {
        "apikey": SB_KEY,
        "Content-Type": "application/json"
    }

    # Eski JWT ko'rinishidagi kalitlar uchun.
    # sb_secret_ kalitini Bearer sifatida yubormaymiz.
    if SB_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SB_KEY}"

    if prefer:
        headers["Prefer"] = prefer

    url = f"{SB_API}/{path.lstrip('/')}"

    response = requests.request(
        method=method,
        url=url,
        params=params,
        json=body,
        headers=headers,
        timeout=25
    )

    if not response.ok:
        raise RuntimeError(
            f"Supabase HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    if not response.content:
        return None

    try:
        return response.json()
    except ValueError:
        return response.text


def sb_rpc(function_name, arguments):
    return sb_request(
        "POST",
        f"rpc/{function_name}",
        body=arguments
    )


def rpc_integer(value):
    if isinstance(value, list):
        value = value[0] if value else -1

    if isinstance(value, dict):
        if len(value) == 1:
            value = next(iter(value.values()))
        else:
            raise RuntimeError(
                f"RPC natijasi kutilgan shaklda emas: {value}"
            )

    return int(value)


def ensure_user(user_id):
    user_id = int(user_id)

    rows = sb_request(
        "GET",
        "users",
        params={
            "select": "user_id,credits",
            "user_id": f"eq.{user_id}",
            "limit": "1"
        }
    )

    if rows:
        return rows[0]

    # Yangi foydalanuvchiga 3 ta boshlang'ich kredit
    try:
        sb_request(
            "POST",
            "users",
            body={
                "user_id": user_id,
                "credits": 3
            },
            prefer="return=representation"
        )
    except Exception:
        # Bir vaqtning o'zida qayta yaratilgan bo'lishi mumkin
        rows = sb_request(
            "GET",
            "users",
            params={
                "select": "user_id,credits",
                "user_id": f"eq.{user_id}",
                "limit": "1"
            }
        )

        if not rows:
            raise

    rows = sb_request(
        "GET",
        "users",
        params={
            "select": "user_id,credits",
            "user_id": f"eq.{user_id}",
            "limit": "1"
        }
    )

    if not rows:
        raise RuntimeError("Foydalanuvchi bazadan topilmadi")

    return rows[0]


def get_balance(user_id):
    user = ensure_user(user_id)
    return int(user.get("credits") or 0)


def create_payment(user_id, credits, amount):
    rows = sb_request(
        "POST",
        "payments",
        body={
            "user_id": int(user_id),
            "credits": int(credits),
            "amount": int(amount),
            "status": "waiting"
        },
        prefer="return=representation"
    )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("To'lov yozuvi bazada yaratilmagan")

    payment_id = rows[0].get("id")

    if not payment_id:
        raise RuntimeError(
            "To'lov yozuvi yaratildi, lekin id qaytmadi"
        )

    return str(payment_id)


def find_waiting_payment(user_id):
    rows = sb_request(
        "GET",
        "payments",
        params={
            "select": "id,user_id,credits,amount,status,created_at",
            "user_id": f"eq.{int(user_id)}",
            "status": "eq.waiting",
            "order": "created_at.desc",
            "limit": "1"
        }
    )

    return rows[0] if rows else None


# =========================
# TELEGRAM MENYU
# =========================

MAIN_MENU = {
    "keyboard": [
        [
            {"text": "🖼️ Rasm yaratish"},
            {"text": "🎬 Video yaratish"}
        ],
        [
            {"text": "💳 Kredit sotib olish"},
            {"text": "💰 Balans"}
        ],
        [
            {"text": "ℹ️ Yordam"}
        ]
    ],
    "resize_keyboard": True
}


def show_packs(chat_id):
    buttons = []

    for credits, amount in PACKS:
        price_text = f"{amount:,}".replace(",", " ")

        buttons.append([
            {
                "text": (
                    f"{credits} ta kredit — "
                    f"{price_text} so'm"
                ),
                "callback_data": f"buy:{credits}"
            }
        ])

    send_message(
        chat_id,
        "Kerakli kredit paketini tanlang:",
        reply_markup={
            "inline_keyboard": buttons
        }
    )


# =========================
# RASM GENERATSIYASI
# =========================

def create_image(prompt):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise RuntimeError(
            "CF_ACCOUNT_ID yoki CF_API_TOKEN kiritilmagan"
        )

    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )

    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        json={
            "prompt": prompt,
            "steps": 4
        },
        timeout=180
    )

    if not response.ok:
        raise RuntimeError(
            f"Cloudflare HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    # API rasmni to'g'ridan-to'g'ri baytlar ko'rinishida qaytarsa
    if content_type.startswith("image/"):
        return response.content

    try:
        data = response.json()
    except ValueError:
        if response.content:
            return response.content
        raise RuntimeError("Cloudflare bo'sh javob qaytardi")

    if data.get("success") is False:
        raise RuntimeError(
            f"Cloudflare xatosi: {str(data.get('errors'))[:400]}"
        )

    result = data.get("result", data)

    if isinstance(result, dict):
        image = result.get("image")
    elif isinstance(result, str):
        image = result
    else:
        image = None

    if not image:
        raise RuntimeError(
            "Cloudflare javobida rasm ma'lumoti yo'q"
        )

    if image.startswith("data:") and "," in image:
        image = image.split(",", 1)[1]

    try:
        return base64.b64decode(image)
    except Exception as exc:
        raise RuntimeError(
            f"Rasm ma'lumotini ochib bo'lmadi: {exc}"
        )


def generate_and_send(chat_id, user_id, prompt):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        send_message(
            chat_id,
            "Rasm yaratish sozlanmagan. "
            "Render'dagi Cloudflare kalitlarini tekshirish kerak."
        )
        return

    if len(prompt) > 2048:
        send_message(
            chat_id,
            "Prompt juda uzun. 2048 belgidan qisqaroq yozing."
        )
        return

    # Avval bitta kreditni atomik tarzda ishlatamiz
    charged = False

    try:
        result = sb_rpc(
            "consume_motion_credit",
            {"p_user_id": int(user_id)}
        )

        remaining = rpc_integer(result)

        if remaining < 0:
            send_message(
                chat_id,
                "Kreditingiz qolmagan. "
                "Kredit sotib olish tugmasidan foydalaning."
            )
            return

        charged = True

        send_message(chat_id, "⏳ Rasm yaratilmoqda. Kuting...")

        image_bytes = create_image(prompt)

        send_image_bytes(
            chat_id,
            image_bytes,
            caption="✅ Motion AI orqali yaratildi."
        )

        log.info(
            "Rasm yaratildi: user_id=%s, qolgan_kredit=%s",
            user_id,
            remaining
        )

    except Exception:
        log.exception(
            "Rasm yaratish xatosi: user_id=%s",
            user_id
        )

        # Rasm yaratilmasa, ishlatilgan kreditni qaytarishga urinamiz
        if charged:
            try:
                sb_rpc(
                    "refund_motion_credit",
                    {"p_user_id": int(user_id)}
                )
            except Exception:
                log.exception("Kreditni qaytarish xatosi")

        try:
            send_message(
                chat_id,
                "Rasm yaratilmadi. Kreditni qaytarishga urindik. "
                "Render → Logs sahifasidagi yangi xatoni tekshirish kerak."
            )
        except Exception:
            log.exception("Xato xabarini yuborib bo'lmadi")


# =========================
# CHEKNI QABUL QILISH
# =========================

def handle_receipt(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = int(user["id"])

    if not ADMIN_ID:
        send_message(
            chat_id,
            "Admin sozlanmagan. To'lovni tekshirib bo'lmaydi."
        )
        return

    if not message.get("photo"):
        send_message(
            chat_id,
            "Iltimos, to'lov chekini rasm sifatida yuboring."
        )
        return

    try:
        payment = find_waiting_payment(user_id)

        if not payment:
            send_message(
                chat_id,
                "Sizda chek kutayotgan to'lov so'rovi yo'q. "
                "Avval kredit paketini tanlang."
            )
            return

        photo_file_id = message["photo"][-1]["file_id"]

        updated = sb_request(
            "PATCH",
            "payments",
            params={
                "id": f"eq.{payment['id']}",
                "status": "eq.waiting"
            },
            body={
                "status": "pending",
                "file_id": photo_file_id
            },
            prefer="return=representation"
        )

        if not updated:
            send_message(
                chat_id,
                "Chek qabul qilinmadi. "
                "To'lov holatini qayta tekshiring."
            )
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Tasdiqlash",
                        "callback_data": (
                            f"approve:{payment['id']}"
                        )
                    },
                    {
                        "text": "❌ Rad etish",
                        "callback_data": (
                            f"reject:{payment['id']}"
                        )
                    }
                ]
            ]
        }

        name = user.get("first_name", "Noma'lum")
        username = user.get("username", "username yo'q")

        caption = (
            "Yangi to'lov cheki\n"
            f"Foydalanuvchi: {name}\n"
            f"Username: @{username}\n"
            f"ID: {user_id}\n"
            f"Kredit: {payment['credits']}\n"
            f"Summa: {payment['amount']} so'm\n"
            f"To'lov ID: {payment['id']}"
        )

        try:
            tg_call(
                "sendPhoto",
                {
                    "chat_id": int(ADMIN_ID),
                    "photo": photo_file_id,
                    "caption": caption,
                    "reply_markup": keyboard
                }
            )
        except Exception:
            log.exception("Admin uchun chek yuborilmadi")

            send_message(
                int(ADMIN_ID),
                caption,
                reply_markup=keyboard
            )

        send_message(
            chat_id,
            "✅ Chek qabul qilindi. "
            "Admin to'lovni tekshirganidan keyin kredit beriladi."
        )

    except Exception:
        log.exception("Chekni qabul qilish xatosi")

        send_message(
            chat_id,
            "Chekni qabul qilishda xatolik yuz berdi. "
            "Iltimos, keyinroq qayta urinib ko'ring."
        )


# =========================
# ADMIN TO'LOVNI BOSHQARISHI
# =========================

def is_admin(user_id):
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def handle_admin_payment(callback, action, payment_id):
    user_id = callback.get("from", {}).get("id")

    if not is_admin(user_id):
        send_message(
            user_id,
            "Bu amal faqat admin uchun."
        )
        return

    chat = callback.get("message", {}).get("chat", {})
    admin_chat_id = chat.get("id", user_id)

    if action == "approve":
        result = sb_rpc(
            "approve_motion_payment",
            {"p_payment_id": payment_id}
        )

        if not isinstance(result, dict) or not result.get("ok"):
            reason = (
                result.get("message", "Noma'lum holat")
                if isinstance(result, dict)
                else str(result)
            )

            send_message(
                admin_chat_id,
                f"To'lov tasdiqlanmadi: {reason}"
            )
            return

        buyer_id = int(result["user_id"])
        credits = int(result["credits"])
        balance = int(result["balance"])

        send_message(
            buyer_id,
            f"✅ To'lov tasdiqlandi!\n"
            f"Qo'shilgan kredit: {credits}\n"
            f"Jami balans: {balance}"
        )

        send_message(
            admin_chat_id,
            f"✅ To'lov tasdiqlandi. "
            f"Foydalanuvchi: {buyer_id}; "
            f"qo'shilgan kredit: {credits}."
        )

    elif action == "reject":
        updated = sb_request(
            "PATCH",
            "payments",
            params={
                "id": f"eq.{payment_id}",
                "status": "eq.pending"
            },
            body={"status": "rejected"},
            prefer="return=representation"
        )

        if not updated:
            send_message(
                admin_chat_id,
                "Bu to'lov topilmadi yoki oldin ko'rib chiqilgan."
            )
            return

        buyer_id = int(updated[0]["user_id"])

        send_message(
            buyer_id,
            "❌ To'lov tasdiqlanmadi. "
            "Admin bilan bog'lanib, chekni tekshiring."
        )

        send_message(admin_chat_id, "To'lov rad etildi.")


# =========================
# TUGMALAR VA XABARLAR
# =========================

def process_callback(callback):
    callback_id = callback.get("id")
    callback_data = callback.get("data", "")
    user = callback.get("from", {})
    user_id = user.get("id")

    chat = callback.get("message", {}).get("chat", {})
    chat_id = chat.get("id", user_id)

    # Har qanday callback'da avval aylanishni to'xtatish
    if callback_id:
        answer_callback(callback_id)

    log.info(
        "Tugma bosildi: user_id=%s, data=%s",
        user_id,
        callback_data
    )

    try:
        if callback_data.startswith("buy:"):
            credits = int(callback_data.split(":", 1)[1])

            price_map = dict(PACKS)

            if credits not in price_map:
                send_message(chat_id, "Bunday paket topilmadi.")
                return

            if not ADMIN_CARD or not CARD_OWNER:
                send_message(
                    chat_id,
                    "Karta sozlamasi to'liq emas. "
                    "Admin kartasi va egasini Render'da kiriting."
                )
                return

            payment_id = create_payment(
                user_id,
                credits,
                price_map[credits]
            )

            price_text = (
                f"{price_map[credits]:,}".replace(",", " ")
            )

            send_message(
                chat_id,
                f"💳 To'lov so'rovi yaratildi.\n\n"
                f"Paket: {credits} ta kredit\n"
                f"Summa: {price_text} so'm\n"
                f"Karta: {ADMIN_CARD}\n"
                f"Karta egasi: {CARD_OWNER}\n\n"
                f"Pul o'tkazganingizdan keyin chekni "
                f"rasm ko'rinishida shu chatga yuboring.\n\n"
                f"So'rov ID: {payment_id}\n"
                f"Holat: chek kutilmoqda."
            )
            return

        if callback_data.startswith("approve:"):
            payment_id = callback_data.split(":", 1)[1]
            handle_admin_payment(
                callback, "approve", payment_id
            )
            return

        if callback_data.startswith("reject:"):
            payment_id = callback_data.split(":", 1)[1]
            handle_admin_payment(
                callback, "reject", payment_id
            )
            return

    except Exception:
        log.exception(
            "Tugmani bajarish xatosi: %s",
            callback_data
        )

        try:
            send_message(
                chat_id,
                "Amal bajarilmadi. Render'dagi "
                "Application Logs sahifasini tekshirish kerak."
            )
        except Exception:
            log.exception("Xato haqida xabar yuborilmadi")


def process_message(message):
    if not message or not message.get("chat"):
        return

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")

    if user_id is None:
        return

    user_id = int(user_id)
    text = message.get("text", "")

    # Chek rasmlari
    if message.get("photo"):
        handle_receipt(message)
        return

    if message.get("document"):
        send_message(
            chat_id,
            "Chekni fayl emas, rasm sifatida yuboring."
        )
        return

    if not text:
        return

    if text == "/start" or text.startswith("/start "):
        try:
            ensure_user(user_id)
            welcome = (
                "🤖 Motion AI botiga xush kelibsiz!\n"
                "Kerakli bo'limni tanlang."
            )
        except Exception:
            log.exception("Foydalanuvchini yaratish xatosi")
            welcome = (
                "Motion AI ishga tushdi, ammo bazaga "
                "ulanishda xatolik bor. Admin loglarni tekshirishi kerak."
            )

        send_message(
            chat_id,
            welcome,
            reply_markup=MAIN_MENU
        )
        return

    if text == "🖼️ Rasm yaratish":
        USER_STATE[user_id] = "image_prompt"

        send_message(
            chat_id,
            "Rasm uchun prompt yozing.\n"
            "Masalan: quyosh botayotgan paytda tog'lar "
            "orasida turgan kichik uy, realistik uslub."
        )
        return

    if text == "🎬 Video yaratish":
        send_message(
            chat_id,
            "🎬 Video yaratish xizmati hali ulanmagan. "
            "Buning uchun alohida video generatsiya API'si kerak."
        )
        return

    if text == "💳 Kredit sotib olish":
        try:
            show_packs(chat_id)
        except Exception:
            log.exception("Kredit paketlarini ko'rsatish xatosi")
            send_message(
                chat_id,
                "Paketlarni ko'rsatib bo'lmadi. "
                "Render loglarini tekshirish kerak."
            )
        return

    if text == "💰 Balans" or text == "/balance":
        try:
            balance = get_balance(user_id)
            send_message(
                chat_id,
                f"💰 Joriy balans: {balance} ta kredit"
            )
        except Exception:
            log.exception("Balansni o'qish xatosi")
            send_message(
                chat_id,
                "Balansni tekshirib bo'lmadi. "
                "Baza sozlamalarini tekshirish kerak."
            )
        return

    if text == "ℹ️ Yordam":
        send_message(
            chat_id,
            "Yordam:\n\n"
            "🖼️ Rasm yaratish — prompt yuboring.\n"
            "💳 Kredit sotib olish — paket tanlang va "
            "to'lov chekini yuboring.\n"
            "💰 Balans — qolgan kreditlar.\n\n"
            "To'lov cheki admin tomonidan qo'lda tekshiriladi."
        )
        return

    if USER_STATE.get(user_id) == "image_prompt":
        USER_STATE.pop(user_id, None)
        generate_and_send(chat_id, user_id, text)
        return

    send_message(
        chat_id,
        "Iltimos, menyudagi tugmalardan birini tanlang.",
        reply_markup=MAIN_MENU
    )


def process_update(update):
    if "message" in update:
        process_message(update["message"])

    elif "callback_query" in update:
        process_callback(update["callback_query"])


# =========================
# TELEGRAM LONG POLLING
# =========================

def polling_loop():
    log.info("Telegram polling boshlandi")

    try:
        tg_call(
            "deleteWebhook",
            {"drop_pending_updates": False},
            timeout=20
        )
        log.info("Webhook holati tekshirildi")
    except Exception:
        log.exception("Webhookni tekshirishda xatolik")

    offset = None

    while True:
        try:
            payload = {
                "timeout": 25,
                "allowed_updates": [
                    "message",
                    "callback_query"
                ]
            }

            if offset is not None:
                payload["offset"] = offset

            updates = tg_call(
                "getUpdates",
                payload,
                timeout=35
            )

            for update in updates or []:
                offset = update["update_id"] + 1

                try:
                    process_update(update)
                except Exception:
                    log.exception(
                        "Update qayta ishlanmadi: %s",
                        update.get("update_id")
                    )

                    # Xabarni jim qoldirmaslikka harakat qilamiz
                    try:
                        if "message" in update:
                            cid = update["message"]["chat"]["id"]
                        else:
                            cid = (
                                update["callback_query"]
                                .get("message", {})
                                .get("chat", {})
                                .get("id")
                            )

                        if cid:
                            send_message(
                                cid,
                                "Xatolik yuz berdi. "
                                "Admin Render loglarini tekshirishi kerak."
                            )
                    except Exception:
                        log.exception("Xato xabarini yuborib bo'lmadi")

        except Exception:
            log.exception("Telegram polling xatosi")
            time.sleep(3)


# =========================
# RENDER HEALTH CHECK
# =========================

@app.get("/")
def home():
    return jsonify({
        "service": "Motion AI",
        "status": "running"
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# Render uchun bitta ishchi jarayon ishlatiladi
if BOT_TOKEN:
    thread = threading.Thread(
        target=polling_loop,
        daemon=True,
        name="telegram-polling"
    )
    thread.start()
else:
    log.error("BOT_TOKEN yo'q. Telegram bot ishga tushmaydi.")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        debug=False
    )
