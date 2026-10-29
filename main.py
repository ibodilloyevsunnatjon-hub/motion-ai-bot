import os
import time
import base64
import logging
import threading
import requests

from flask import Flask, jsonify

# =========================
# SOZLAMALAR
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

SB_API = (
    SB_URL
    if SB_URL.endswith("/rest/v1")
    else SB_URL + "/rest/v1"
)

USER_STATE = {}


# =========================
# KREDIT PAKETLARI
# =========================

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
            log.warning("Noto'g'ri paket: %s", item)

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
        raise RuntimeError("BOT_TOKEN sozlanmagan")

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
            f"Telegram JSON xatosi: HTTP {response.status_code}"
        )

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram {method} xatosi: "
            f"HTTP {response.status_code}: {str(data)[:500]}"
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
    return tg_call(
        "sendPhoto",
        {
            "chat_id": chat_id,
            "caption": caption
        },
        files={
            "photo": (
                "motion-ai.jpg",
                image_bytes,
                "image/jpeg"
            )
        },
        timeout=90
    )


def send_video_bytes(chat_id, video_bytes, caption=""):
    return tg_call(
        "sendVideo",
        {
            "chat_id": chat_id,
            "caption": caption
        },
        files={
            "video": (
                "motion-ai.mp4",
                video_bytes,
                "video/mp4"
            )
        },
        timeout=180
    )


def answer_callback(callback_id):
    try:
        tg_call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id},
            timeout=15
        )
    except Exception:
        log.exception("Callback javob xatosi")


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
        raise RuntimeError("SB_URL yoki SB_KEY sozlanmagan")

    headers = {
        "apikey": SB_KEY,
        "Content-Type": "application/json"
    }

    if SB_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SB_KEY}"

    if prefer:
        headers["Prefer"] = prefer

    response = requests.request(
        method,
        f"{SB_API}/{path.lstrip('/')}",
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
                f"RPC natijasi noto'g'ri: {value}"
            )

    return int(value)


def ensure_user(user_id):
    user_id = int(user_id)

    params = {
        "select": "user_id,credits",
        "user_id": f"eq.{user_id}",
        "limit": "1"
    }

    rows = sb_request("GET", "users", params=params)

    if rows:
        return rows[0]

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
        rows = sb_request("GET", "users", params=params)

        if not rows:
            raise

    rows = sb_request("GET", "users", params=params)

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
        raise RuntimeError("To'lov bazaga yozilmadi")

    payment_id = rows[0].get("id")

    if not payment_id:
        raise RuntimeError("To'lov ID qaytmadi")

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
# MENYU
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
                    f"{credits} ta kredit — {price_text} so'm"
                ),
                "callback_data": f"buy:{credits}"
            }
        ])

    send_message(
        chat_id,
        "Kerakli kredit paketini tanlang:",
        reply_markup={"inline_keyboard": buttons}
    )


# =========================
# CLOUDFLARE UMUMIY SOZLAMA
# =========================

def cloudflare_headers():
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise RuntimeError(
            "CF_ACCOUNT_ID yoki CF_API_TOKEN sozlanmagan"
        )

    return {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }


# =========================
# RASM YARATISH
# =========================

def create_image(prompt):
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )

    response = requests.post(
        endpoint,
        headers={
            **cloudflare_headers(),
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
            f"Cloudflare image HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    if response.headers.get("Content-Type", "").lower().startswith(
        "image/"
    ):
        return response.content

    try:
        data = response.json()
    except ValueError:
        if response.content:
            return response.content
        raise RuntimeError("Cloudflare rasm javobi bo'sh")

    if data.get("success") is False:
        raise RuntimeError(
            f"Cloudflare rasm xatosi: {str(data.get('errors'))[:400]}"
        )

    result = data.get("result", data)

    if isinstance(result, dict):
        image = result.get("image")
    elif isinstance(result, str):
        image = result
    else:
        image = None

    if not image:
        raise RuntimeError("Javobda rasm ma'lumoti yo'q")

    if image.startswith("data:") and "," in image:
        image = image.split(",", 1)[1]

    try:
        return base64.b64decode(image)
    except Exception as exc:
        raise RuntimeError(f"Rasm ma'lumoti ochilmadi: {exc}")


def generate_and_send(chat_id, user_id, prompt):
    if len(prompt) > 2048:
        send_message(chat_id, "Prompt 2048 belgidan qisqa bo'lsin.")
        return

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
                "Kreditingiz qolmagan. Kredit sotib oling."
            )
            return

        charged = True

        send_message(chat_id, "⏳ Rasm yaratilmoqda. Kuting...")

        image_bytes = create_image(prompt)

        send_image_bytes(
            chat_id,
            image_bytes,
            "✅ Motion AI orqali yaratildi."
        )

        log.info(
            "Rasm yaratildi: user_id=%s, qolgan=%s",
            user_id,
            remaining
        )

    except Exception:
        log.exception("Rasm yaratish xatosi")

        if charged:
            try:
                sb_rpc(
                    "refund_motion_credit",
                    {"p_user_id": int(user_id)}
                )
            except Exception:
                log.exception("Rasm krediti qaytmadi")

        try:
            send_message(
                chat_id,
                "❌ Rasm yaratilmadi. Kreditni qaytarishga "
                "urindik. Render Logs sahifasini tekshiring."
            )
        except Exception:
            log.exception("Rasm xabari yuborilmadi")


# =========================
# VIDEO YARATISH
# =========================

def create_video(prompt):
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run"
    )

    # Cloudflare Workers AI: P-Video
    response = requests.post(
        endpoint,
        headers=cloudflare_headers(),
        json={
            "model": "pruna/p-video",
            "input": {
                "prompt": prompt,
                "duration": 5,
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "fps": 24,
                "draft": True,
                "save_audio": True,
                "prompt_upsampling": True,
                "disable_safety_filter": False
            }
        },
        timeout=240
    )

    if not response.ok:
        raise RuntimeError(
            f"Cloudflare video HTTP {response.status_code}: "
            f"{response.text[:800]}"
        )

    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    if content_type.startswith("video/"):
        return response.content

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "Video API javobi JSON formatida emas"
        )

    if data.get("success") is False:
        raise RuntimeError(
            f"Cloudflare video xatosi: "
            f"{str(data.get('errors'))[:500]}"
        )

    state = str(data.get("state", "")).lower()

    if state in ("failed", "error", "cancelled"):
        raise RuntimeError(
            f"Video holati: {state}; {str(data)[:500]}"
        )

    result = data.get("result", data)

    if isinstance(result, dict):
        video_url = result.get("video") or result.get("url")
    else:
        video_url = None

    if not video_url:
        raise RuntimeError(
            f"Video manzili topilmadi: {str(data)[:600]}"
        )

    if not str(video_url).startswith("https://"):
        raise RuntimeError("Video manzili HTTPS emas")

    video_response = requests.get(
        video_url,
        timeout=180
    )

    if not video_response.ok:
        raise RuntimeError(
            f"Videoni yuklab bo'lmadi: HTTP "
            f"{video_response.status_code}"
        )

    if not video_response.content:
        raise RuntimeError("Yuklangan video bo'sh")

    return video_response.content


def generate_video_and_send(chat_id, user_id, prompt):
    if len(prompt) > 2000:
        send_message(chat_id, "Prompt 2000 belgidan qisqa bo'lsin.")
        return

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
                "Kreditingiz qolmagan. Kredit sotib oling."
            )
            return

        charged = True

        send_message(
            chat_id,
            "🎬 Video yaratilmoqda. Bir oz kuting..."
        )

        video_bytes = create_video(prompt)

        send_video_bytes(
            chat_id,
            video_bytes,
            "✅ Motion AI video yaratdi!"
        )

        log.info(
            "Video yaratildi: user_id=%s, qolgan=%s",
            user_id,
            remaining
        )

    except Exception:
        log.exception("Video yaratish xatosi")

        if charged:
            try:
                sb_rpc(
                    "refund_motion_credit",
                    {"p_user_id": int(user_id)}
                )
            except Exception:
                log.exception("Video krediti qaytmadi")

        try:
            send_message(
                chat_id,
                "❌ Video yaratilmadi. Kreditni qaytarishga "
                "urindik. Render → Logs sahifasidagi xatoni "
                "tekshiring."
            )
        except Exception:
            log.exception("Video xabari yuborilmadi")


# =========================
# CHEK QABUL QILISH
# =========================

def handle_receipt(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = int(user["id"])

    if not ADMIN_ID:
        send_message(chat_id, "Admin sozlanmagan.")
        return

    if not message.get("photo"):
        send_message(
            chat_id,
            "To'lov chekini rasm sifatida yuboring."
        )
        return

    try:
        payment = find_waiting_payment(user_id)

        if not payment:
            send_message(
                chat_id,
                "Chek kutayotgan to'lov topilmadi. "
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
                "Chek qabul qilinmadi. To'lov holatini tekshiring."
            )
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Tasdiqlash",
                        "callback_data": f"approve:{payment['id']}"
                    },
                    {
                        "text": "❌ Rad etish",
                        "callback_data": f"reject:{payment['id']}"
                    }
                ]
            ]
        }

        caption = (
            "Yangi to'lov cheki\n"
            f"Ism: {user.get('first_name', 'Nomaʼlum')}\n"
            f"Username: @{user.get('username', 'username yoʻq')}\n"
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
            "✅ Chek qabul qilindi. Admin tekshirganidan keyin "
            "kredit beriladi."
        )

    except Exception:
        log.exception("Chekni qabul qilish xatosi")
        send_message(
            chat_id,
            "Chekni qabul qilishda xatolik yuz berdi."
        )


# =========================
# ADMIN TO'LOVLARI
# =========================

def is_admin(user_id):
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def handle_admin_payment(callback, action, payment_id):
    user_id = callback.get("from", {}).get("id")

    if not is_admin(user_id):
        send_message(user_id, "Bu amal faqat admin uchun.")
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
            "✅ To'lov tasdiqlandi!\n"
            f"Qo'shilgan kredit: {credits}\n"
            f"Jami balans: {balance}"
        )

        send_message(
            admin_chat_id,
            f"✅ To'lov tasdiqlandi. ID: {buyer_id}; "
            f"kredit: {credits}."
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
                "To'lov topilmadi yoki avval ko'rib chiqilgan."
            )
            return

        buyer_id = int(updated[0]["user_id"])

        send_message(
            buyer_id,
            "❌ To'lov tasdiqlanmadi. Admin bilan bog'laning."
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

    if callback_id:
        answer_callback(callback_id)

    try:
        if callback_data.startswith("buy:"):
            credits = int(callback_data.split(":", 1)[1])
            price_map = dict(PACKS)

            if credits not in price_map:
                send_message(chat_id, "Paket topilmadi.")
                return

            if not ADMIN_CARD or not CARD_OWNER:
                send_message(
                    chat_id,
                    "Admin kartasi yoki karta egasi sozlanmagan."
                )
                return

            payment_id = create_payment(
                user_id,
                credits,
                price_map[credits]
            )

            price_text = f"{price_map[credits]:,}".replace(",", " ")

            send_message(
                chat_id,
                "💳 To'lov so'rovi yaratildi.\n\n"
                f"Paket: {credits} ta kredit\n"
                f"Summa: {price_text} so'm\n"
                f"Karta: {ADMIN_CARD}\n"
                f"Karta egasi: {CARD_OWNER}\n\n"
                "Pul o'tkazgach, chekni rasm sifatida yuboring.\n"
                f"So'rov ID: {payment_id}"
            )
            return

        if callback_data.startswith("approve:"):
            handle_admin_payment(
                callback,
                "approve",
                callback_data.split(":", 1)[1]
            )
            return

        if callback_data.startswith("reject:"):
            handle_admin_payment(
                callback,
                "reject",
                callback_data.split(":", 1)[1]
            )
            return

    except Exception:
        log.exception("Tugma bajarilmadi: %s", callback_data)

        try:
            send_message(
                chat_id,
                "Amal bajarilmadi. Render Application Logs'ni tekshiring."
            )
        except Exception:
            log.exception("Xato xabari yuborilmadi")


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

    if message.get("photo"):
        handle_receipt(message)
        return

    if message.get("document"):
        send_message(
            chat_id,
            "Chekni hujjat emas, rasm sifatida yuboring."
        )
        return

    if not text:
        return

    if text == "/start" or text.startswith("/start "):
        USER_STATE.pop(user_id, None)

        try:
            ensure_user(user_id)
            welcome = (
                "🤖 Motion AI botiga xush kelibsiz!\n"
                "Kerakli bo'limni tanlang."
            )
        except Exception:
            log.exception("Foydalanuvchi bazasi xatosi")
            welcome = (
                "Bazaga ulanishda xatolik yuz berdi. "
                "Admin loglarni tekshirishi kerak."
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
            "🖼️ Rasm uchun prompt yuboring.\n"
            "Bekor qilish uchun /cancel yozing."
        )
        return

    if text == "🎬 Video yaratish":
        USER_STATE[user_id] = "video_prompt"
        send_message(
            chat_id,
            "🎬 Video uchun prompt yuboring.\n\n"
            "Masalan: A small kitten running through a meadow, "
            "butterflies flying, cinematic camera movement.\n\n"
            "Davomiyligi: 5 soniya; sifat: 720p draft.\n"
            "Bekor qilish uchun /cancel yozing."
        )
        return

    if text == "💳 Kredit sotib olish":
        try:
            show_packs(chat_id)
        except Exception:
            log.exception("Kredit paketlari xatosi")
            send_message(chat_id, "Paketlarni ko'rsatib bo'lmadi.")
        return

    if text == "💰 Balans" or text == "/balance":
        try:
            send_message(
                chat_id,
                f"💰 Joriy balans: {get_balance(user_id)} ta kredit"
            )
        except Exception:
            log.exception("Balans xatosi")
            send_message(chat_id, "Balansni tekshirib bo'lmadi.")
        return

    if text == "ℹ️ Yordam":
        send_message(
            chat_id,
            "ℹ️ Motion AI yordam\n\n"
            "🖼️ Rasm yaratish — rasm promptini yuboring.\n"
            "🎬 Video yaratish — video promptini yuboring.\n"
            "💳 Kredit sotib olish — paketni tanlang.\n"
            "💰 Balans — qolgan kreditlarni ko'ring.\n"
            "/cancel — joriy amalni bekor qilish.\n\n"
            "To'lovlar admin tomonidan tekshiriladi."
        )
        return

    if text == "/cancel":
        USER_STATE.pop(user_id, None)
        send_message(
            chat_id,
            "Amal bekor qilindi.",
            reply_markup=MAIN_MENU
        )
        return

    if USER_STATE.get(user_id) == "image_prompt":
        USER_STATE.pop(user_id, None)
        generate_and_send(chat_id, user_id, text)
        return

    if USER_STATE.get(user_id) == "video_prompt":
        USER_STATE.pop(user_id, None)
        generate_video_and_send(chat_id, user_id, text)
        return

    send_message(
        chat_id,
        "Menyudagi tugmalardan birini tanlang.",
        reply_markup=MAIN_MENU
    )


def process_update(update):
    if "message" in update:
        process_message(update["message"])
    elif "callback_query" in update:
        process_callback(update["callback_query"])


# =========================
# TELEGRAM POLLING
# =========================

def polling_loop():
    log.info("Telegram polling boshlandi")

    try:
        tg_call(
            "deleteWebhook",
            {"drop_pending_updates": False},
            timeout=20
        )
    except Exception:
        log.exception("Webhook tekshiruvi xatosi")

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


# =========================
# ISHGA TUSHIRISH
# =========================

if BOT_TOKEN:
    thread = threading.Thread(
        target=polling_loop,
        daemon=True,
        name="telegram-polling"
    )
    thread.start()
else:
    log.error("BOT_TOKEN sozlanmagan")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        debug=False
    )
