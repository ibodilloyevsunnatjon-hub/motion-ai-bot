import os
import re
import time
import base64
import logging
import threading
import requests
import shutil
import subprocess
import tempfile

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
).strip()

PROMPT_MODEL = "@cf/meta/llama-3.2-3b-instruct"

VIDEO_MODEL = "pruna/p-video"
VIDEO_DURATION = 5
VIDEO_RESOLUTION = "720p"
VIDEO_FPS = 24
VIDEO_DRAFT = False

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024

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
POLLING_LOCK = threading.Lock()


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
            log.warning("Noto'g'ri kredit paketi: %s", item)

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
# TELEGRAM FAYL YUKLASH
# =========================

def get_telegram_file_bytes(file_id, max_size=None):
    file_info = tg_call(
        "getFile",
        {"file_id": file_id},
        timeout=30
    )

    if not file_info or not file_info.get("file_path"):
        raise RuntimeError("Telegram fayl manzilini bermadi")

    file_size = file_info.get("file_size", 0)

    if max_size and file_size and file_size > max_size:
        raise RuntimeError(
            "Fayl hajmi ruxsat etilgan chegaradan katta."
        )

    file_url = (
        f"https://api.telegram.org/file/bot"
        f"{BOT_TOKEN}/{file_info['file_path']}"
    )

    response = requests.get(file_url, timeout=90)

    if not response.ok:
        raise RuntimeError(
            f"Fayl yuklanmadi: HTTP {response.status_code}"
        )

    if max_size and len(response.content) > max_size:
        raise RuntimeError("Yuklangan fayl hajmi juda katta.")

    return response.content


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

    # Eski JWT formatidagi Supabase kalitlari uchun.
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


def consume_credit(user_id):
    result = sb_rpc(
        "consume_motion_credit",
        {"p_user_id": int(user_id)}
    )

    remaining = rpc_integer(result)

    if remaining < 0:
        return None

    return remaining


def refund_credit(user_id):
    return sb_rpc(
        "refund_motion_credit",
        {"p_user_id": int(user_id)}
    )


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
            {"text": "🎬 Matndan video"}
        ],
        [
            {"text": "📸 Rasmdan video"},
            {"text": "🎞️ Videodan rasm"}
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
        reply_markup={"inline_keyboard": buttons}
    )


# =========================
# CLOUDFLARE API
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


def cloudflare_endpoint(model):
    return (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{model}"
    )


# =========================
# DUTORNI ANIQLASH
# =========================

DUTOR_PATTERN = re.compile(
    r"(?<![a-zA-Z])dutor(?:ning|ni|ga|dan|lar|im|i)?(?![a-zA-Z])",
    re.IGNORECASE
)

DUTAR_ENGLISH = (
    "traditional Uzbek dutar, a two-stringed, "
    "long-necked plucked string musical instrument"
)


def contains_dutor(text):
    text = text.lower()
    return bool(
        DUTOR_PATTERN.search(text)
        or re.search(r"(?<![a-zA-Z])dutar(?![a-zA-Z])", text)
    )


def is_simple_dutor_request(text):
    """
    Faqat 'Dutor', 'Dutor rasmi', 'Dutor surati'
    kabi sodda so'rovlarni tekshiradi.
    Bunday holatda tarjimon model ishlatilmaydi.
    """
    words = re.findall(
        r"[a-zA-Zа-яА-ЯўқғҳЎҚҒҲ']+",
        text.lower()
    )

    allowed_words = {
        "dutor",
        "dutar",
        "dutorning",
        "dutorni",
        "dutorim",
        "rasm",
        "rasmi",
        "rasmini",
        "surat",
        "surati",
        "suratini",
        "tasvir",
        "tasviri",
        "chiz",
        "chizing"
    }

    return (
        bool(words)
        and contains_dutor(text)
        and all(word in allowed_words for word in words)
    )


# =========================
# O'ZBEKCHA MATNNI ICHKI TARJIMA QILISH
# =========================

def make_english_prompt(user_text, media_type):
    if not user_text or not user_text.strip():
        raise RuntimeError("Prompt matni bo'sh.")

    original_text = user_text.strip()
    has_dutor = contains_dutor(original_text)

    # Eng muhim tuzatish:
    # "Dutor rasmi" bevosita to'g'ri inglizcha promptga aylanadi.
    # Bunda Llama modelining dutorni general deb tushunishiga
    # imkon berilmaydi.
    if has_dutor and is_simple_dutor_request(original_text):
        if media_type == "video":
            return (
                "A traditional Uzbek dutar, a two-stringed, "
                "long-necked plucked string musical instrument. "
                "Show the complete dutar clearly. "
                "The dutar is the main subject. "
                "Do not show a person, soldier, military officer, "
                "general, uniform, or unrelated object."
            )

        return (
            "A clear, realistic image of one traditional Uzbek dutar, "
            "a two-stringed, long-necked plucked string musical instrument. "
            "Show the complete musical instrument clearly as the main "
            "subject. The dutar is a musical instrument, not a person. "
            "Do not show a human, soldier, military officer, general, "
            "military uniform, or unrelated object."
        )

    if media_type == "image":
        task = (
            "Translate the user's request into a clear English image "
            "generation prompt. Preserve the exact requested subject, "
            "objects, actions, colors, location and composition. "
        )
    else:
        task = (
            "Translate the user's request into a clear English video "
            "generation prompt. Preserve the exact requested subject, "
            "objects, actions, movement and scene. "
            "The video duration is five seconds. "
        )

    # Agar foydalanuvchi dutor deb yozgan bo'lsa,
    # tarjimonning o'zbekcha so'zni noto'g'ri tushunish ehtimolini kamaytiramiz.
    if has_dutor:
        safe_user_text = DUTOR_PATTERN.sub(
            f"{DUTAR_ENGLISH} (called dutor in Uzbek)",
            original_text
        )

        safe_user_text = re.sub(
            r"(?<![a-zA-Z])dutar(?![a-zA-Z])",
            DUTAR_ENGLISH,
            safe_user_text,
            flags=re.IGNORECASE
        )

        subject_rule = (
            " IMPORTANT SUBJECT RULE: The word dutor means the "
            "traditional Uzbek musical instrument called a dutar. "
            "It is NOT a person, NOT a military general, NOT an officer, "
            "and NOT a soldier. The requested main subject must remain "
            "the dutar musical instrument. Do not replace it with a person "
            "or another object. "
        )
    else:
        safe_user_text = original_text
        subject_rule = ""

    system_prompt = (
        "You translate Uzbek requests into English prompts for image "
        "and video generation. "
        "Do not invent a new subject. Do not replace the user's subject. "
        "Do not add people, animals, buildings, vehicles, uniforms, "
        "weapons, scenery, actions, or objects unless the user requested "
        "them. Keep the user's meaning and details. "
        "Do not add cinematic effects or extra decorations unless asked. "
        + subject_rule
        + task
        + "Return only the final English prompt. "
        "Do not include explanations, labels or quotation marks."
    )

    response = requests.post(
        cloudflare_endpoint(PROMPT_MODEL),
        headers=cloudflare_headers(),
        json={
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": safe_user_text
                }
            ],
            "max_tokens": 400,
            "temperature": 0.1,
            "top_p": 0.2
        },
        timeout=120
    )

    if not response.ok:
        raise RuntimeError(
            f"Prompt AI HTTP {response.status_code}: "
            f"{response.text[:600]}"
        )

    data = response.json()

    if data.get("success") is False:
        raise RuntimeError(
            f"Prompt AI xatosi: {str(data.get('errors'))[:400]}"
        )

    result = data.get("result", data)

    if isinstance(result, dict):
        english_prompt = result.get("response")
    else:
        english_prompt = None

    if not english_prompt or not str(english_prompt).strip():
        raise RuntimeError("AI inglizcha prompt qaytarmadi.")

    english_prompt = (
        str(english_prompt)
        .strip()
        .strip('"')
        .strip("'")
    )

    # Dutor so'ralgan bo'lsa-yu, tarjimada dutar so'zi yo'qolsa,
    # noto'g'ri rasm yaratmaslik uchun xatoga to'xtatamiz.
    if has_dutor:
        if not re.search(
            r"\bdutar\b|\btraditional Uzbek dutar\b",
            english_prompt,
            re.IGNORECASE
        ):
            raise RuntimeError(
                "Tarjima dutor ma'nosini saqlamadi. "
                "Noto'g'ri rasm yaratilmadi."
            )

        english_prompt = (
            f"MAIN SUBJECT: {DUTAR_ENGLISH}. "
            "It is a musical instrument, not a human. "
            "Do not replace it with a person, general, officer, "
            "soldier or military uniform. "
            + english_prompt
        )

    return english_prompt


# =========================
# RASM YARATISH
# =========================

def create_image(prompt):
    endpoint = cloudflare_endpoint(
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

    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    if content_type.startswith("image/"):
        return response.content

    data = response.json()

    if data.get("success") is False:
        raise RuntimeError(
            f"Cloudflare rasm xatosi: "
            f"{str(data.get('errors'))[:400]}"
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
        raise RuntimeError(
            f"Rasm ma'lumoti ochilmadi: {exc}"
        )


def generate_and_send(chat_id, user_id, user_text):
    if len(user_text) > 2048:
        send_message(chat_id, "Matn 2048 belgidan qisqa bo'lsin.")
        return

    charged = False

    try:
        remaining = consume_credit(user_id)

        if remaining is None:
            send_message(
                chat_id,
                "Kreditingiz qolmagan. Kredit sotib oling."
            )
            return

        charged = True

        send_message(
            chat_id,
            "🖼️ Rasm yaratilmoqda. Kuting..."
        )

        english_prompt = make_english_prompt(
            user_text,
            "image"
        )

        image_bytes = create_image(english_prompt)

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

    except Exception as error:
        log.exception("Rasm yaratish xatosi")

        if charged:
            try:
                refund_credit(user_id)
            except Exception:
                log.exception("Rasm krediti qaytmadi")

        send_message(
            chat_id,
            "❌ Rasm yaratilmadi. Kreditni qaytarishga "
            "urindik. Render Logs sahifasini tekshiring."
        )


# =========================
# VIDEO YARATISH
# =========================

def create_video(prompt, image_data_uri=None):
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run"
    )

    video_input = {
        "prompt": prompt,
        "duration": VIDEO_DURATION,
        "resolution": VIDEO_RESOLUTION,
        "aspect_ratio": "16:9",
        "fps": VIDEO_FPS,
        "draft": VIDEO_DRAFT,
        "save_audio": True,

        # O'CHIRILDI:
        # Model promptni o'zicha kengaytirmaydi.
        "prompt_upsampling": False,

        "disable_safety_filter": False
    }

    if image_data_uri:
        video_input["image"] = image_data_uri

    response = requests.post(
        endpoint,
        headers=cloudflare_headers(),
        json={
            "model": VIDEO_MODEL,
            "input": video_input
        },
        timeout=300
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

    data = response.json()

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


def generate_video_and_send(
    chat_id,
    user_id,
    user_text,
    image_data_uri=None
):
    if len(user_text) > 2000:
        send_message(chat_id, "Matn 2000 belgidan qisqa bo'lsin.")
        return

    charged = False

    try:
        remaining = consume_credit(user_id)

        if remaining is None:
            send_message(
                chat_id,
                "Kreditingiz qolmagan. Kredit sotib oling."
            )
            return

        charged = True

        send_message(
            chat_id,
            "🎬 5 soniyalik HD video yaratilmoqda. Kuting..."
        )

        english_prompt = make_english_prompt(
            user_text,
            "video"
        )

        video_bytes = create_video(
            english_prompt,
            image_data_uri=image_data_uri
        )

        send_video_bytes(
            chat_id,
            video_bytes,
            "✅ Motion AI video yaratdi!\n"
            "Sifat: HD 720p\n"
            "Davomiyligi: 5 soniya"
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
                refund_credit(user_id)
            except Exception:
                log.exception("Video krediti qaytmadi")

        send_message(
            chat_id,
            "❌ Video yaratilmadi. Kreditni qaytarishga "
            "urindik. Render → Logs sahifasidagi xatoni tekshiring."
        )


# =========================
# RASMDAN VIDEO
# =========================

def handle_image_video_photo(message):
    chat_id = message["chat"]["id"]
    user_id = int(message["from"]["id"])

    try:
        photo = message["photo"][-1]

        photo_bytes = get_telegram_file_bytes(
            photo["file_id"],
            max_size=MAX_IMAGE_BYTES
        )

        image_data_uri = (
            "data:image/jpeg;base64,"
            + base64.b64encode(photo_bytes).decode("utf-8")
        )

        USER_STATE[user_id] = {
            "mode": "image_video_prompt",
            "image_data_uri": image_data_uri
        }

        send_message(
            chat_id,
            "✅ Rasm qabul qilindi!\n\n"
            "Endi shu rasm asosida qanday video "
            "yaratishni xohlayotganingizni yozing.\n\n"
            "Masalan: Kamera sekin yaqinlashadi, "
            "daraxt barglari shamolda tebranadi.\n\n"
            "Video: 5 soniya, HD 720p.\n"
            "/cancel — bekor qilish."
        )

    except Exception:
        log.exception("Rasmni qabul qilish xatosi")
        send_message(
            chat_id,
            "❌ Rasmni yuklab bo'lmadi. Boshqa rasm yuboring."
        )


# =========================
# VIDEODAN RASM OLISH
# =========================

def extract_video_frame(video_bytes):
    ffmpeg_path = shutil.which("ffmpeg")

    if not ffmpeg_path:
        try:
            import imageio_ffmpeg
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise RuntimeError(
                "FFmpeg topilmadi. requirements.txt fayliga "
                "imageio-ffmpeg qo'shing."
            )

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = os.path.join(temp_dir, "input.mp4")
        output_path = os.path.join(temp_dir, "frame.jpg")

        with open(input_path, "wb") as file:
            file.write(video_bytes)

        command = [
            ffmpeg_path,
            "-y",
            "-ss", "00:00:01",
            "-i", input_path,
            "-frames:v", "1",
            "-q:v", "2",
            output_path
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90
        )

        if result.returncode != 0:
            raise RuntimeError(
                "FFmpeg xatosi: " + result.stderr[-700:]
            )

        if not os.path.exists(output_path):
            raise RuntimeError("Video ichidan rasm olinmadi.")

        with open(output_path, "rb") as file:
            image_bytes = file.read()

        if not image_bytes:
            raise RuntimeError("Olingan rasm bo'sh.")

        return image_bytes


def extract_and_send_frame(chat_id, user_id, video_bytes):
    charged = False

    try:
        send_message(
            chat_id,
            "🎞️ Videodan rasm ajratilmoqda..."
        )

        image_bytes = extract_video_frame(video_bytes)

        remaining = consume_credit(user_id)

        if remaining is None:
            send_message(
                chat_id,
                "Kreditingiz qolmagan. Kredit sotib oling."
            )
            return

        charged = True

        send_image_bytes(
            chat_id,
            image_bytes,
            "✅ Videodan rasm olindi!"
        )

        log.info(
            "Videodan rasm olindi: user_id=%s, qolgan=%s",
            user_id,
            remaining
        )

    except Exception:
        log.exception("Videodan rasm olish xatosi")

        if charged:
            try:
                refund_credit(user_id)
            except Exception:
                log.exception("Kredit qaytmadi")

        send_message(
            chat_id,
            "❌ Videodan rasm olinmadi. "
            "Render Logs sahifasidagi xatoni tekshiring."
        )


def handle_video_upload(message):
    chat_id = message["chat"]["id"]
    user_id = int(message["from"]["id"])

    video = message.get("video")

    if not video:
        video = message.get("document", {})

    if not video.get("file_id"):
        send_message(chat_id, "Video faylini topib bo'lmadi.")
        return

    file_size = video.get("file_size", 0)

    if file_size and file_size > MAX_VIDEO_BYTES:
        send_message(
            chat_id,
            "Video hajmi 20 MB dan kichik bo'lishi kerak."
        )
        return

    try:
        video_bytes = get_telegram_file_bytes(
            video["file_id"],
            max_size=MAX_VIDEO_BYTES
        )

        extract_and_send_frame(
            chat_id,
            user_id,
            video_bytes
        )

    except Exception:
        log.exception("Video yuklash xatosi")
        send_message(
            chat_id,
            "Videoni qabul qilishda xatolik yuz berdi."
        )


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
            "✅ Chek qabul qilindi. Admin tekshirganidan "
            "keyin kredit beriladi."
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

        send_message(
            admin_chat_id,
            "To'lov rad etildi."
        )


# =========================
# TUGMALAR
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

            price_text = (
                f"{price_map[credits]:,}".replace(",", " ")
            )

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


# =========================
# XABARLAR
# =========================

def process_message(message):
    if not message or not message.get("chat"):
        return

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")

    if user_id is None:
        return

    user_id = int(user_id)
    text = message.get("text", "").strip()
    state = USER_STATE.get(user_id)

    # START
    if text == "/start" or text.startswith("/start "):
        USER_STATE.pop(user_id, None)

        try:
            ensure_user(user_id)

            welcome = (
                "🤖 Motion AI botiga xush kelibsiz!\n\n"
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

    # CANCEL
    if text == "/cancel":
        USER_STATE.pop(user_id, None)

        send_message(
            chat_id,
            "Amal bekor qilindi.",
            reply_markup=MAIN_MENU
        )
        return

    # RASM YARATISH
    if text == "🖼️ Rasm yaratish":
        USER_STATE[user_id] = "image_prompt"

        send_message(
            chat_id,
            "🖼️ Qanday rasm xohlayotganingizni yozing.\n\n"
            "Masalan: Quyosh botayotgan paytda "
            "dengiz bo'yida turgan oq ot.\n\n"
            "O'zbekcha yozishingiz mumkin.\n"
            "/cancel — bekor qilish."
        )
        return

    # MATNDAN VIDEO
    if text == "🎬 Matndan video":
        USER_STATE[user_id] = "video_prompt"

        send_message(
            chat_id,
            "🎬 Qanday video xohlayotganingizni yozing.\n\n"
            "Masalan: Oq ot qorli tog'da yugursin, "
            "kamera uni orqasidan kuzatsin.\n\n"
            "Davomiyligi: 5 soniya.\n"
            "Sifati: HD 720p.\n\n"
            "O'zbekcha yozishingiz mumkin.\n"
            "/cancel — bekor qilish."
        )
        return

    # RASMDAN VIDEO
    if text == "📸 Rasmdan video":
        USER_STATE[user_id] = "image_video_wait_image"

        send_message(
            chat_id,
            "📸 Video yaratish uchun rasm yuboring.\n\n"
            "Rasm qabul qilingandan keyin qanday "
            "harakat bo'lishini yozasiz.\n\n"
            "Video: 5 soniya, HD 720p.\n"
            "/cancel — bekor qilish."
        )
        return

    # VIDEODAN RASM
    if text == "🎞️ Videodan rasm":
        USER_STATE[user_id] = "video_to_image_wait_video"

        send_message(
            chat_id,
            "🎞️ Videodan rasm olish uchun MP4 video yuboring.\n\n"
            "Video hajmi 20 MB dan kichik bo'lishi kerak.\n"
            "Tizim taxminan 1-soniyadagi kadrni oladi.\n\n"
            "/cancel — bekor qilish."
        )
        return

    # KREDIT SOTIB OLISH
    if text == "💳 Kredit sotib olish":
        try:
            show_packs(chat_id)
        except Exception:
            log.exception("Kredit paketlari xatosi")
            send_message(chat_id, "Paketlarni ko'rsatib bo'lmadi.")
        return

    # BALANS
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

    # YORDAM
    if text == "ℹ️ Yordam":
        send_message(
            chat_id,
            "ℹ️ Motion AI yordam\n\n"
            "🖼️ Rasm yaratish — rasm haqida yozing.\n"
            "🎬 Matndan video — video tavsifini yozing.\n"
            "📸 Rasmdan video — rasm yuboring, keyin "
            "harakatni yozing.\n"
            "🎞️ Videodan rasm — video yuboring.\n"
            "💳 Kredit sotib olish — paketni tanlang.\n"
            "💰 Balans — qolgan kreditlarni ko'ring.\n\n"
            "Har bir AI yaratish amali 1 kredit sarflaydi.\n"
            "O'zbekcha matn yozishingiz mumkin.\n"
            "/cancel — joriy amalni bekor qilish.\n\n"
            "To'lovlar admin tomonidan tekshiriladi."
        )
        return

    # RASM YUBORILDI
    if message.get("photo"):
        if state == "image_video_wait_image":
            handle_image_video_photo(message)
        else:
            handle_receipt(message)
        return

    # VIDEO YUBORILDI
    if message.get("video"):
        if state == "video_to_image_wait_video":
            handle_video_upload(message)
        else:
            send_message(
                chat_id,
                "Videodan rasm olish uchun avval "
                "🎞️ Videodan rasm tugmasini bosing."
            )
        return

    # VIDEO HUJJAT KO'RINISHIDA YUBORILDI
    if message.get("document"):
        document = message.get("document", {})
        mime_type = document.get("mime_type", "")

        if (
            state == "video_to_image_wait_video"
            and mime_type.startswith("video/")
        ):
            handle_video_upload(message)
        else:
            send_message(
                chat_id,
                "Bu fayl qo'llab-quvvatlanmaydi. "
                "Videoni MP4 formatida yuboring."
            )
        return

    if not text:
        return

    # RASM PROMPTI
    if state == "image_prompt":
        USER_STATE.pop(user_id, None)

        generate_and_send(
            chat_id,
            user_id,
            text
        )
        return

    # MATNDAN VIDEO PROMPTI
    if state == "video_prompt":
        USER_STATE.pop(user_id, None)

        generate_video_and_send(
            chat_id,
            user_id,
            text
        )
        return

    # RASMDAN VIDEO PROMPTI
    if (
        isinstance(state, dict)
        and state.get("mode") == "image_video_prompt"
    ):
        image_data_uri = state.get("image_data_uri")
        USER_STATE.pop(user_id, None)

        if not image_data_uri:
            send_message(
                chat_id,
                "Rasm ma'lumoti topilmadi. Qaytadan boshlang."
            )
            return

        generate_video_and_send(
            chat_id,
            user_id,
            text,
            image_data_uri=image_data_uri
        )
        return

    # FAYL KUTILAYOTGAN HOLATLAR
    if state in (
        "video_to_image_wait_video",
        "image_video_wait_image"
    ):
        send_message(
            chat_id,
            "Kerakli faylni yuboring yoki /cancel bosing."
        )
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
    # Bitta polling thread ishlashini nazorat qilish.
    if not POLLING_LOCK.acquire(blocking=False):
        log.warning("Polling allaqachon ishlayapti.")
        return

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
    return jsonify({
        "status": "ok",
        "telegram_configured": bool(BOT_TOKEN),
        "cloudflare_configured": bool(
            CF_ACCOUNT_ID and CF_API_TOKEN
        ),
        "supabase_configured": bool(SB_URL and SB_KEY)
    })


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
