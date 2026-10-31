import os, re, time, base64, logging, threading, shutil, subprocess, tempfile
import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("MotionAI")
app = Flask(__name__)


def env(name, default=""):
    return os.getenv(name, default).strip()


BOT_TOKEN = env("BOT_TOKEN")
CF_ACCOUNT_ID = env("CF_ACCOUNT_ID")
CF_API_TOKEN = env("CF_API_TOKEN")
SB_URL = env("SB_URL").rstrip("/")
SB_KEY = env("SB_KEY")
ADMIN_ID = env("ADMIN_ID")
ADMIN_CARD = env("ADMIN_CARD")
CARD_OWNER = env("CARD_OWNER")
PACK_PRICES = env("PACK_PRICES", "10:10000,30:25000,100:70000")

LLM_MODELS = ["@cf/google/gemma-4-26b-a4b-it", "@cf/google/gemma-3-12b-it"]
TRANSLATE_MODEL = "@cf/meta/m2m100-1.2b"
IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
VIDEO_MODEL = "pruna/p-video"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
SB_API = SB_URL if SB_URL.endswith("/rest/v1") else SB_URL + "/rest/v1"
CF_RUN = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run"
USER_STATE = {}
POLL_LOCK = threading.Lock()


def load_packs():
    packs = []
    for item in PACK_PRICES.split(","):
        try:
            c, a = item.strip().split(":", 1)
            if int(c) > 0 and int(a) > 0:
                packs.append((int(c), int(a)))
        except ValueError:
            log.warning("Noto'g'ri paket: %s", item)
    return packs or [(10, 10000), (30, 25000), (100, 70000)]


PACKS = load_packs()

# ===================== TELEGRAM =====================


def tg_call(method, payload=None, files=None, timeout=40):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN sozlanmagan")
    kwargs = {"data": payload or {}, "files": files} if files else {"json": payload or {}}
    r = requests.post(f"{TG}/{method}", timeout=timeout, **kwargs)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram JSON xatosi: HTTP {r.status_code}")
    if not r.ok or not data.get("ok"):
        raise RuntimeError(f"Telegram {method} xatosi: HTTP {r.status_code}: {str(data)[:400]}")
    return data.get("result")


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": str(text)}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", payload)


def send_media(method, field, chat_id, data, name, mime, caption="", timeout=90):
    return tg_call(method, {"chat_id": chat_id, "caption": caption},
                   files={field: (name, data, mime)}, timeout=timeout)


def send_image_bytes(chat_id, data, caption=""):
    return send_media("sendPhoto", "photo", chat_id, data, "motion-ai.jpg", "image/jpeg", caption)


def send_video_bytes(chat_id, data, caption=""):
    return send_media("sendVideo", "video", chat_id, data, "motion-ai.mp4", "video/mp4", caption, 180)


def answer_callback(callback_id):
    try:
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=15)
    except Exception:
        log.exception("Callback javob xatosi")


def get_telegram_file_bytes(file_id, max_size=None):
    info = tg_call("getFile", {"file_id": file_id}, timeout=30)
    if not info or not info.get("file_path"):
        raise RuntimeError("Telegram fayl manzilini bermadi")
    size = info.get("file_size", 0)
    if max_size and size and size > max_size:
        raise RuntimeError("Fayl hajmi juda katta.")
    r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}", timeout=90)
    if not r.ok:
        raise RuntimeError(f"Fayl yuklanmadi: HTTP {r.status_code}")
    if max_size and len(r.content) > max_size:
        raise RuntimeError("Yuklangan fayl hajmi juda katta.")
    return r.content


# ===================== SUPABASE =====================


def sb_request(method, path, params=None, body=None, prefer=None):
    if not SB_URL or not SB_KEY:
        raise RuntimeError("SB_URL yoki SB_KEY sozlanmagan")
    headers = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if SB_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SB_KEY}"
    if prefer:
        headers["Prefer"] = prefer
    r = requests.request(method, f"{SB_API}/{path.lstrip('/')}", params=params,
                         json=body, headers=headers, timeout=25)
    if not r.ok:
        raise RuntimeError(f"Supabase HTTP {r.status_code}: {r.text[:400]}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


def sb_rpc(name, args):
    return sb_request("POST", f"rpc/{name}", body=args)


def rpc_integer(value):
    if isinstance(value, list):
        value = value[0] if value else -1
    if isinstance(value, dict):
        if len(value) != 1:
            raise RuntimeError(f"RPC natijasi noto'g'ri: {value}")
        value = next(iter(value.values()))
    return int(value)


def ensure_user(user_id):
    params = {"select": "user_id,credits", "user_id": f"eq.{int(user_id)}", "limit": "1"}
    rows = sb_request("GET", "users", params=params)
    if not rows:
        try:
            sb_request("POST", "users", body={"user_id": int(user_id), "credits": 3},
                       prefer="return=representation")
        except Exception:
            pass
        rows = sb_request("GET", "users", params=params)
    if not rows:
        raise RuntimeError("Foydalanuvchi bazadan topilmadi")
    return rows[0]


def get_balance(user_id):
    return int(ensure_user(user_id).get("credits") or 0)


def consume_credit(user_id):
    remaining = rpc_integer(sb_rpc("consume_motion_credit", {"p_user_id": int(user_id)}))
    return None if remaining < 0 else remaining


def refund_credit(user_id):
    return sb_rpc("refund_motion_credit", {"p_user_id": int(user_id)})


def create_payment(user_id, credits, amount):
    rows = sb_request("POST", "payments", prefer="return=representation",
                      body={"user_id": int(user_id), "credits": int(credits),
                            "amount": int(amount), "status": "waiting"})
    if not isinstance(rows, list) or not rows or not rows[0].get("id"):
        raise RuntimeError("To'lov bazaga yozilmadi")
    return str(rows[0]["id"])


def find_waiting_payment(user_id):
    rows = sb_request("GET", "payments", params={
        "select": "id,user_id,credits,amount,status,created_at",
        "user_id": f"eq.{int(user_id)}", "status": "eq.waiting",
        "order": "created_at.desc", "limit": "1"})
    return rows[0] if rows else None


# ===================== MENYU =====================

MAIN_MENU = {
    "keyboard": [
        [{"text": "🖼️ Rasm yaratish"}, {"text": "🎬 Matndan video"}],
        [{"text": "📸 Rasmdan video"}, {"text": "🎞️ Videodan rasm"}],
        [{"text": "💳 Kredit sotib olish"}, {"text": "💰 Balans"}],
        [{"text": "ℹ️ Yordam"}],
    ],
    "resize_keyboard": True,
}


def show_packs(chat_id):
    buttons = [[{"text": f"{c} ta kredit — {a:,} so'm".replace(",", " "),
                 "callback_data": f"buy:{c}"}] for c, a in PACKS]
    send_message(chat_id, "Kerakli kredit paketini tanlang:",
                 reply_markup={"inline_keyboard": buttons})


# ===================== CLOUDFLARE =====================


def cf_headers():
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise RuntimeError("CF_ACCOUNT_ID yoki CF_API_TOKEN sozlanmagan")
    return {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}


# ===================== DUTOR =====================

DUTOR_PATTERN = re.compile(
    r"(?<![a-zA-Z])dutor(?:ning|ni|ga|dan|lar|im|i)?(?![a-zA-Z])", re.IGNORECASE)
DUTAR_ENGLISH = ("traditional Uzbek dutar, a two-stringed, long-necked "
                 "plucked string musical instrument")


def contains_dutor(text):
    text = text.lower()
    return bool(DUTOR_PATTERN.search(text) or re.search(r"(?<![a-zA-Z])dutar(?![a-zA-Z])", text))


def is_simple_dutor_request(text):
    words = re.findall(r"[a-zA-Zа-яА-ЯўқғҳЎҚҒҲ']+", text.lower())
    allowed = {"dutor", "dutar", "dutorning", "dutorni", "dutorim", "rasm", "rasmi",
               "rasmini", "surat", "surati", "suratini", "tasvir", "tasviri", "chiz", "chizing"}
    return bool(words) and contains_dutor(text) and all(w in allowed for w in words)


def simple_dutor_prompt(media_type):
    if media_type == "video":
        return (f"A {DUTAR_ENGLISH}. Show the complete dutar clearly as the main subject. "
                "Do not show a person, soldier, officer, uniform, or unrelated object.")
    return (f"A clear, realistic image of one {DUTAR_ENGLISH}. Show the complete musical "
            "instrument as the main subject. It is an instrument, not a person. "
            "Do not show a human, soldier, general, uniform, or unrelated object.")


# ===================== LUG'AT =====================
# Yangi so'z qo'shish: tegishli ro'yxatga  "o'zbekcha": "english",  qatorini qo'shing.

UZ_NOUNS = {
    # hayvonlar
    "mushuk": "cat", "mushukcha": "kitten", "it": "dog", "kuchuk": "puppy", "ot": "horse",
    "sigir": "cow", "buzoq": "calf", "ho'kiz": "ox", "buqa": "bull", "qo'y": "sheep",
    "qo'zi": "lamb", "echki": "goat", "tovuq": "chicken", "jo'ja": "chick",
    "xo'roz": "rooster", "o'rdak": "duck", "g'oz": "goose", "kurka": "turkey",
    "kaptar": "pigeon", "quyon": "rabbit", "bo'ri": "wolf", "tulki": "fox", "ayiq": "bear",
    "sher": "lion", "yo'lbars": "tiger", "qoplon": "leopard", "fil": "elephant",
    "maymun": "monkey", "tuya": "camel", "eshak": "donkey", "cho'chqa": "pig",
    "baliq": "fish", "akula": "shark", "kit": "whale", "delfin": "dolphin", "qush": "bird",
    "burgut": "eagle", "lochin": "falcon", "boyqush": "owl", "bulbul": "nightingale",
    "to'ti": "parrot", "tovus": "peacock", "oqqush": "swan", "laylak": "stork",
    "turna": "crane", "ilon": "snake", "toshbaqa": "turtle", "qurbaqa": "frog",
    "timsoh": "crocodile", "kapalak": "butterfly", "ari": "bee", "chumoli": "ant",
    "o'rgimchak": "spider", "kiyik": "deer", "jirafa": "giraffe", "zebra": "zebra",
    "sichqon": "mouse", "olmaxon": "squirrel", "yarasa": "bat", "dinozavr": "dinosaur",
    "ajdaho": "dragon",
    # meva, sabzavot, ovqat
    "olma": "apple", "nok": "pear", "anor": "pomegranate", "uzum": "grapes",
    "o'rik": "apricot", "shaftoli": "peach", "gilos": "cherry", "olxo'ri": "plum",
    "behi": "quince", "anjir": "fig", "xurmo": "persimmon", "qovun": "melon",
    "tarvuz": "watermelon", "banan": "banana", "apelsin": "orange", "limon": "lemon",
    "qulupnay": "strawberry", "yong'oq": "walnut", "bodom": "almond",
    "non": "traditional Uzbek bread", "choy": "tea",
    "palov": "plov, traditional Uzbek rice pilaf", "somsa": "samsa, a baked Uzbek pastry",
    "lag'mon": "lagman noodles", "manti": "manti dumplings", "shashlik": "shashlik kebab",
    "go'sht": "meat", "tuxum": "egg", "sut": "milk", "qatiq": "yogurt", "asal": "honey",
    "kartoshka": "potato", "pomidor": "tomato", "bodring": "cucumber", "sabzi": "carrot",
    "piyoz": "onion", "qovoq": "pumpkin", "guruch": "rice", "bug'doy": "wheat",
    "makkajo'xori": "corn", "paxta": "cotton", "tort": "cake", "muzqaymoq": "ice cream",
    "shokolad": "chocolate", "kofe": "coffee", "suv": "water", "shirinlik": "sweets",
    # tabiat
    "daraxt": "tree", "gul": "flower", "o't": "grass", "barg": "leaf", "tog'": "mountain",
    "tepalik": "hill", "vodiy": "valley", "daryo": "river", "ko'l": "lake", "dengiz": "sea",
    "okean": "ocean", "sohil": "shore", "qirg'oq": "shore", "orol": "island",
    "cho'l": "desert", "o'rmon": "forest", "dala": "field", "bog'": "garden",
    "sharshara": "waterfall", "buloq": "spring", "g'or": "cave", "qoya": "cliff",
    "tosh": "stone", "qum": "sand", "yer": "ground", "osmon": "sky", "quyosh": "sun",
    "oy": "moon", "yulduz": "star", "bulut": "cloud", "yomg'ir": "rain", "qor": "snow",
    "shamol": "wind", "bo'ron": "storm", "chaqmoq": "lightning", "kamalak": "rainbow",
    "tuman": "fog", "muz": "ice", "olov": "fire", "alanga": "flame",
    "paxtazor": "cotton field", "uzumzor": "vineyard", "guldasta": "bouquet",
    # joy va binolar
    "uy": "house", "hovli": "courtyard", "qasr": "palace", "qal'a": "fortress",
    "masjid": "mosque", "madrasa": "madrasa", "minora": "minaret", "gumbaz": "dome",
    "ko'prik": "bridge", "yo'l": "road", "ko'cha": "street", "shahar": "city",
    "qishloq": "village", "maydon": "square", "bozor": "bazaar",
    "choyxona": "traditional Uzbek teahouse", "maktab": "school", "kasalxona": "hospital",
    "stadion": "stadium", "fabrika": "factory", "zavod": "factory",
    "issiqxona": "greenhouse", "ferma": "farm", "kutubxona": "library",
    "muzey": "museum", "teatr": "theater", "restoran": "restaurant", "do'kon": "shop",
    "aeroport": "airport", "mahalla": "neighborhood",
    # transport
    "mashina": "car", "avtomobil": "car", "avtobus": "bus", "velosiped": "bicycle",
    "mototsikl": "motorcycle", "samolyot": "airplane", "vertolyot": "helicopter",
    "kema": "ship", "qayiq": "boat", "poyezd": "train", "traktor": "tractor",
    "kombayn": "combine harvester", "raketa": "rocket", "dron": "drone", "arava": "cart",
    # buyum va kiyim
    "kitob": "book", "daftar": "notebook", "qalam": "pencil", "stol": "table",
    "stul": "chair", "karavot": "bed", "divan": "sofa", "deraza": "window",
    "eshik": "door", "devor": "wall", "tom": "roof", "telefon": "phone",
    "kompyuter": "computer", "noutbuk": "laptop", "televizor": "television",
    "kamera": "camera", "chiroq": "lamp", "sham": "candle", "oyna": "mirror",
    "piyola": "tea bowl", "choynak": "teapot", "kosa": "bowl", "likop": "plate",
    "qoshiq": "spoon", "pichoq": "knife", "qozon": "cauldron", "tandir": "tandoor oven",
    "gilam": "carpet", "suzani": "suzani embroidered textile",
    "do'ppi": "doppi, a traditional Uzbek skullcap",
    "chopon": "chapan, a traditional Uzbek robe", "ro'mol": "headscarf",
    "ko'ylak": "dress", "shim": "trousers", "kurtka": "jacket", "poyabzal": "shoes",
    "etik": "boots", "shapka": "hat", "qalpoq": "cap", "ko'zoyna": "glasses",
    "sumka": "bag", "uzuk": "ring", "marjon": "necklace", "toj": "crown",
    "sovg'a": "gift", "to'p": "ball", "bayroq": "flag",
    # musiqa asboblari
    "dutor": "dutar, a traditional two-stringed Uzbek lute",
    "doira": "doira, an Uzbek frame drum", "nay": "nay flute",
    "karnay": "karnay, a long Uzbek horn", "rubob": "rubab, a traditional lute",
    "tanbur": "tanbur, a long-necked lute", "surnay": "surnay, a traditional wind instrument",
    "gitara": "guitar", "pianino": "piano", "skripka": "violin", "baraban": "drum",
    # odamlar
    "odam": "person", "erkak": "man", "ayol": "woman", "bola": "child", "qiz": "girl",
    "yigit": "young man", "o'g'il": "boy", "chol": "old man", "kampir": "old woman",
    "ona": "mother", "ota": "father", "buvi": "grandmother", "bobo": "grandfather",
    "do'st": "friend", "oila": "family", "o'qituvchi": "teacher", "talaba": "student",
    "shifokor": "doctor", "dehqon": "farmer", "cho'pon": "shepherd",
    "bog'bon": "gardener", "haydovchi": "driver", "oshpaz": "chef", "usta": "craftsman",
    "temirchi": "blacksmith", "kulol": "potter", "duradgor": "carpenter",
    "askar": "soldier", "qirol": "king", "malika": "queen", "shahzoda": "prince",
    "bahodir": "hero", "pahlavon": "wrestler", "sportchi": "athlete",
    "xonanda": "singer", "raqqosa": "female dancer", "rassom": "painter",
    "musiqachi": "musician",
}

UZ_ADJECTIVES = {
    "oq": "white", "qora": "black", "qizil": "red", "yashil": "green", "sariq": "yellow",
    "ko'k": "blue", "zangori": "light blue", "havorang": "sky blue", "jigarrang": "brown",
    "kulrang": "gray", "pushti": "pink", "binafsha": "purple", "to'q": "dark",
    "oltin": "golden", "kumush": "silver", "katta": "big", "kichik": "small",
    "kichkina": "tiny", "ulkan": "huge", "baland": "tall", "past": "low", "uzun": "long",
    "qisqa": "short", "keng": "wide", "tor": "narrow", "yosh": "young", "qari": "old",
    "keksa": "elderly", "eski": "old", "yangi": "new", "chiroyli": "beautiful",
    "go'zal": "beautiful", "yorqin": "bright", "qorong'u": "dark", "iliq": "warm",
    "issiq": "hot", "sovuq": "cold", "tez": "fast", "sekin": "slow", "baxtli": "happy",
    "xafa": "sad", "kulgili": "funny", "kuchli": "strong", "mehribon": "kind",
    "jasur": "brave", "qadimiy": "ancient", "zamonaviy": "modern", "shaffof": "transparent",
    "yumshoq": "soft", "qattiq": "hard", "toza": "clean", "mazali": "delicious",
    "shirin": "sweet", "achchiq": "spicy", "to'la": "full", "bo'sh": "empty",
    "sehrli": "magical", "dahshatli": "scary",
}

UZ_SETTINGS = {
    "tong": "dawn", "ertalab": "in the morning", "kunduz": "during the day",
    "kechqurun": "in the evening", "kechasi": "at night", "tun": "night",
    "bahor": "spring season", "yoz": "summer", "kuz": "autumn", "qish": "winter",
    "ustida": "on top of", "ostida": "under", "yonida": "next to", "oldida": "in front of",
    "orqasida": "behind", "ichida": "inside", "tashqarida": "outside",
    "o'rtasida": "in the middle of", "bo'yida": "by the shore of", "yaqinida": "near",
    "uzoqda": "far away", "orasida": "among", "tepasida": "above",
    "realistik": "realistic", "haqiqiy": "real", "multfilm": "cartoon", "anime": "anime",
    "akvarel": "watercolor", "kinematik": "cinematic", "fantastik": "fantasy",
    "yaqindan": "close-up", "uzoqdan": "from far away", "tepadan": "from above",
    "ikki": "two", "uch": "three", "to'rt": "four", "besh": "five", "olti": "six",
    "yetti": "seven", "sakkiz": "eight", "to'qqiz": "nine", "o'n": "ten", "ko'p": "many",
    "ko'plab": "many", "yolg'iz": "alone", "ikkita": "two", "uchta": "three",
    "to'rtta": "four", "beshta": "five", "samarqand": "Samarkand", "buxoro": "Bukhara",
    "xiva": "Khiva", "toshkent": "Tashkent", "o'zbekiston": "Uzbekistan",
    "registon": "the Registan in Samarkand",
}

UZ_VERB_STEMS = {
    "yugur": "running", "sakra": "jumping", "o'yna": "playing", "raqs": "dancing",
    "kuyla": "singing", "uxla": "sleeping", "o'tir": "sitting", "turgan": "standing",
    "uchay": "flying", "uchib": "flying", "suzay": "swimming", "suzib": "swimming",
    "yonay": "burning", "yonib": "burning", "oqay": "flowing", "oqib": "flowing",
    "yog'ay": "falling as rain or snow", "botay": "setting (sun)", "chiqay": "rising",
    "kelay": "coming", "ketay": "going away", "yuray": "walking", "yurib": "walking",
    "pishiray": "cooking", "teray": "picking", "hayday": "driving", "chizay": "drawing",
    "yozay": "writing", "o'qiy": "reading", "kulib": "smiling", "kulay": "laughing",
    "yig'lay": "crying", "quchoq": "hugging", "ishlay": "working", "tomosha": "watching",
    "kuzat": "watching", "gullag": "blooming", "gullab": "blooming", "pishgan": "ripe",
}

NEGATION_WORDS = {"emas", "yo'q", "yoq", "bo'lmasin", "bolmasin", "qilmang",
                  "ko'rsatmang", "qo'shmang", "kerakmas"}
TRIVIAL_WORDS = {"bir", "va", "bilan", "bu", "shu", "u", "men", "hamda"}
CONNECTORS = {"va", "bilan", "hamda"}
UZ_SUFFIXES = sorted(
    ("larning", "larni", "lariga", "laridan", "larida", "larga", "lardan", "larda",
     "lari", "lar", "ning", "gacha", "imiz", "ingiz", "dagi", "dan", "ini", "ga", "ka",
     "qa", "da", "ni", "si", "im", "ing", "li", "i"),
    key=len, reverse=True)

GLOSSARY = {}
for _table, _kind in ((UZ_NOUNS, "n"), (UZ_ADJECTIVES, "a"), (UZ_SETTINGS, "s")):
    for _key, _value in _table.items():
        GLOSSARY[_key] = (_value, _kind)


def _build_stripped_index():
    seen = {}
    for key, entry in GLOSSARY.items():
        stripped = key.replace("'", "")
        if stripped == key:
            continue
        if stripped in GLOSSARY or (stripped in seen and seen[stripped] != entry):
            seen[stripped] = None
        else:
            seen.setdefault(stripped, entry)
    return {k: v for k, v in seen.items() if v is not None}


STRIPPED_INDEX = _build_stripped_index()


def tokenize(text):
    words = re.findall(r"[a-z']+", text.lower())
    return [w.strip("'") for w in words if w.strip("'")]


def lookup_word(word, allow_suffix=True):
    word = word.strip("'")
    if not word or word.endswith("siz"):
        return None
    entry = GLOSSARY.get(word) or STRIPPED_INDEX.get(word)
    if entry or not allow_suffix:
        return entry
    for suffix in UZ_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            stem = word[:-len(suffix)]
            entry = GLOSSARY.get(stem) or STRIPPED_INDEX.get(stem)
            if entry:
                return entry
    return None


def lookup_verb(word):
    for stem, english in UZ_VERB_STEMS.items():
        if word.startswith(stem):
            return english
    return None


def build_hints(text):
    hints, seen = [], set()
    for word in tokenize(text):
        if word in TRIVIAL_WORDS:
            continue
        entry = lookup_word(word)
        english = entry[0] if entry else lookup_verb(word)
        if english and english not in seen:
            seen.add(english)
            hints.append(f"{word} = {english}")
    return hints[:40]


def required_terms(text):
    words = tokenize(text)
    if any(w in NEGATION_WORDS for w in words):
        return []
    result = []
    for word in words:
        entry = lookup_word(word)
        if entry and entry[1] == "n" and entry[0] not in result:
            result.append(entry[0])
    return result[:6]


def enforce_terms(prompt, required):
    lowered = prompt.lower()
    missing = []
    for phrase in required:
        head = phrase.split(",")[0].strip()
        main = head.split()[-1].lower()
        probe = main[:-1] if len(main) > 4 and main.endswith("s") else main
        if probe not in lowered:
            missing.append(head)
    if not missing:
        return prompt
    return prompt.rstrip(". ") + ". Must clearly show: " + ", ".join(missing) + "."


def dictionary_prompt(text, media_type):
    words = tokenize(text)
    if not words or len(words) > 8:
        return None
    chunks, chunk = [], []
    for word in words:
        if word in CONNECTORS:
            if not chunk:
                return None
            chunks.append(chunk)
            chunk = []
            continue
        entry = lookup_word(word, allow_suffix=False)
        if not entry or entry[1] not in ("a", "n"):
            return None
        chunk.append(entry)
    if not chunk:
        return None
    chunks.append(chunk)
    phrases = []
    for chunk in chunks:
        *adjectives, noun = chunk
        if noun[1] != "n" or any(a[1] != "a" for a in adjectives):
            return None
        phrase = " ".join([a[0] for a in adjectives] + [noun[0]])
        phrases.append(("an " if phrase[0] in "aeiou" else "a ") + phrase)
    subject = " and ".join(phrases)
    if media_type == "video":
        return f"A realistic video of {subject}, natural smooth movement."
    return f"A realistic, high-quality photo of {subject}."


# ===================== TARJIMA =====================

CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
    "ч": "ch", "ш": "sh", "ъ": "'", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}
ENGLISH_WORDS = {"the", "a", "an", "of", "with", "and", "in", "on", "at", "is", "are",
                 "to", "for", "from", "by"}


def cyrillic_to_latin(text):
    out = []
    for ch in text:
        lat = CYR_TO_LAT.get(ch.lower())
        out.append(ch if lat is None else (lat.capitalize() if ch.isupper() else lat))
    return "".join(out)


def normalize_apostrophes(text):
    for ch in ("‘", "’", "ʻ", "ʼ", "`", "´"):
        text = text.replace(ch, "'")
    return text


def looks_english(text):
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if len(words) < 2:
        return False
    hits = sum(1 for w in words if w in ENGLISH_WORDS)
    return hits >= 2 or hits / len(words) >= 0.25


def detect_source_lang(text):
    if re.search(r"[ўқғҳЎҚҒҲ]", text):
        return "uz", cyrillic_to_latin(text)
    if re.search(r"[а-яА-ЯёЁ]", text):
        return "ru", text
    return "uz", text


def clean_prompt(text, limit=700):
    text = " ".join(str(text or "").split())
    text = re.sub(r"^(english|prompt|translation)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip('"').strip("'").strip("`").strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    return text


def extract_llm_text(data):
    result = data.get("result", data)
    text = None
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = result.get("response")
        if not text:
            choices = result.get("choices") or []
            if choices:
                first = choices[0] or {}
                text = (first.get("message") or {}).get("content") or first.get("text")
    if isinstance(text, list):
        text = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
    if not text or not str(text).strip():
        raise RuntimeError(f"Modeldan matn qaytmadi: {str(data)[:300]}")
    return str(text)


def call_chat_model(model, system_prompt, user_message):
    r = requests.post(
        f"{CF_RUN}/{model}", headers=cf_headers(), timeout=90,
        json={"messages": [{"role": "system", "content": system_prompt},
                           {"role": "user", "content": user_message}],
              "max_tokens": 400, "temperature": 0.1})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("success") is False:
        raise RuntimeError(f"Xato: {str(data.get('errors'))[:300]}")
    return extract_llm_text(data)


def build_system_prompt(media_type):
    if media_type == "video":
        task = ("The result is a prompt for a 5-second AI video. Describe the scene, "
                "the main subject and its motion, and any camera movement the user mentioned.")
    else:
        task = "The result is a prompt for an AI image of one still scene."
    return (
        "You are a professional Uzbek-to-English translator who writes prompts for AI "
        "image and video generators. The user's text is in Uzbek (or sometimes Russian). "
        "Translate it faithfully into one fluent English prompt.\n\nRULES:\n"
        "1. Keep EVERY subject, object, animal, person, color, number, place, time of day "
        "and action from the user's text. Never drop, replace or invent a subject.\n"
        "2. Do not add people, animals, text, logos or objects the user did not mention.\n"
        "3. Vocabulary hints are verified translations of Uzbek words found in the text. "
        "Always follow them exactly.\n"
        "4. Uzbek puts the verb at the end and adjectives before nouns. Write natural "
        "English word order.\n"
        "5. Keep style words the user wrote (realistic, cartoon, cinematic, watercolor, "
        "close-up and so on).\n"
        f"6. {task}\n"
        "7. Output ONLY the English prompt as one paragraph of at most 100 words. No "
        "quotes, labels, notes or explanations.\n\nEXAMPLES:\n"
        "Uzbek: Quyosh botayotgan paytda dengiz bo'yida turgan oq ot\n"
        "English: A white horse standing on the seashore at sunset.\n\n"
        "Uzbek: Katta qora mushuk deraza oldida o'tiribdi\n"
        "English: A big black cat sitting in front of a window.\n\n"
        "Uzbek: Bahorda tog'li qishloqda gullagan olma daraxtlari orasida bolalar "
        "o'ynayapti\n"
        "English: Children playing among blossoming apple trees in a mountain village "
        "in spring.\n")


def translate_m2m(text, source_lang):
    r = requests.post(f"{CF_RUN}/{TRANSLATE_MODEL}", headers=cf_headers(), timeout=60,
                      json={"text": text, "source_lang": source_lang, "target_lang": "en"})
    if not r.ok:
        raise RuntimeError(f"Tarjima HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("success") is False:
        raise RuntimeError(f"Tarjima xatosi: {str(data.get('errors'))[:300]}")
    result = data.get("result", data)
    translated = result.get("translated_text") if isinstance(result, dict) else None
    if not translated or not str(translated).strip() or str(translated).startswith("ERROR"):
        raise RuntimeError("Tarjima bo'sh yoki xato qaytdi")
    return str(translated).strip()


def translate_with_fallbacks(source_text, source_lang, hints, media_type):
    system_prompt = build_system_prompt(media_type)
    parts = []
    if hints:
        parts.append("Vocabulary hints:\n" + "\n".join(f"- {h}" for h in hints))
    parts.append("Uzbek text:\n" + source_text)
    user_message = "\n\n".join(parts)

    for model in LLM_MODELS:
        try:
            cleaned = clean_prompt(call_chat_model(model, system_prompt, user_message))
            if len(cleaned) >= 3:
                return cleaned, model
        except Exception as error:
            log.warning("LLM tarjima ishlamadi (%s): %s", model, str(error)[:300])
    try:
        return clean_prompt(translate_m2m(source_text, source_lang)), "m2m100"
    except Exception:
        log.exception("M2M tarjima ham ishlamadi")
        raise RuntimeError("Hech bir tarjima usuli ishlamadi")


def make_english_prompt(user_text, media_type):
    if not user_text or not user_text.strip():
        raise RuntimeError("Prompt matni bo'sh.")
    original = normalize_apostrophes(user_text.strip())
    has_dutor = contains_dutor(original)

    if has_dutor and is_simple_dutor_request(original):
        return simple_dutor_prompt(media_type)

    lang, source_text = detect_source_lang(original)
    method = "oddiy"

    if looks_english(original):
        prompt, method = clean_prompt(original, 900), "inglizcha"
    else:
        if lang == "uz" and not has_dutor:
            ready = dictionary_prompt(source_text, media_type)
            if ready:
                log.info("Lug'at prompti (%s): %r -> %r", media_type, original[:100], ready)
                return ready
        hints = build_hints(source_text) if lang == "uz" else []
        required = required_terms(source_text) if lang == "uz" else []
        prompt, method = translate_with_fallbacks(source_text, lang, hints, media_type)
        prompt = enforce_terms(prompt, required)

    if has_dutor:
        prompt = (f"MAIN SUBJECT: {DUTAR_ENGLISH}. It is a musical instrument, "
                  "not a human. " + prompt)
    prompt = clean_prompt(prompt, 900)
    log.info("Prompt tarjimasi (%s, %s): %r -> %r", media_type, method,
             original[:150], prompt[:300])
    return prompt


# ===================== RASM =====================


def create_image(prompt):
    r = requests.post(f"{CF_RUN}/{IMAGE_MODEL}",
                      headers={**cf_headers(), "Accept": "application/json"},
                      json={"prompt": prompt, "steps": 4}, timeout=180)
    if not r.ok:
        raise RuntimeError(f"Cloudflare image HTTP {r.status_code}: {r.text[:400]}")
    if r.headers.get("Content-Type", "").lower().startswith("image/"):
        return r.content
    data = r.json()
    if data.get("success") is False:
        raise RuntimeError(f"Cloudflare rasm xatosi: {str(data.get('errors'))[:300]}")
    result = data.get("result", data)
    image = result.get("image") if isinstance(result, dict) else (
        result if isinstance(result, str) else None)
    if not image:
        raise RuntimeError("Javobda rasm ma'lumoti yo'q")
    if image.startswith("data:") and "," in image:
        image = image.split(",", 1)[1]
    return base64.b64decode(image)


def fail_and_refund(chat_id, user_id, charged, what):
    if charged:
        try:
            refund_credit(user_id)
        except Exception:
            log.exception("Kredit qaytmadi")
    send_message(chat_id, f"❌ {what} yaratilmadi. Kreditni qaytarishga urindik. "
                          "Render Logs sahifasini tekshiring.")


def generate_and_send(chat_id, user_id, text):
    if len(text) > 2000:
        send_message(chat_id, "Matn 2000 belgidan qisqa bo'lsin.")
        return
    charged = False
    try:
        remaining = consume_credit(user_id)
        if remaining is None:
            send_message(chat_id, "Kreditingiz qolmagan. Kredit sotib oling.")
            return
        charged = True
        send_message(chat_id, "🖼️ Rasm yaratilmoqda. Kuting...")
        prompt = make_english_prompt(text, "image")
        send_image_bytes(chat_id, create_image(prompt),
                         f"✅ Motion AI orqali yaratildi.\n🔤 Prompt: {prompt[:300]}")
        log.info("Rasm yaratildi: user_id=%s, qolgan=%s", user_id, remaining)
    except Exception:
        log.exception("Rasm yaratish xatosi")
        fail_and_refund(chat_id, user_id, charged, "Rasm")


# ===================== VIDEO =====================


def create_video(prompt, image_data_uri=None):
    video_input = {"prompt": prompt, "duration": 5, "resolution": "720p",
                   "aspect_ratio": "16:9", "fps": 24, "draft": False,
                   "save_audio": True, "prompt_upsampling": False,
                   "disable_safety_filter": False}
    if image_data_uri:
        video_input["image"] = image_data_uri
    r = requests.post(CF_RUN, headers=cf_headers(), timeout=300,
                      json={"model": VIDEO_MODEL, "input": video_input})
    if not r.ok:
        raise RuntimeError(f"Cloudflare video HTTP {r.status_code}: {r.text[:600]}")
    if r.headers.get("Content-Type", "").lower().startswith("video/"):
        return r.content
    data = r.json()
    if data.get("success") is False:
        raise RuntimeError(f"Cloudflare video xatosi: {str(data.get('errors'))[:400]}")
    if str(data.get("state", "")).lower() in ("failed", "error", "cancelled"):
        raise RuntimeError(f"Video holati xato: {str(data)[:400]}")
    result = data.get("result", data)
    url = (result.get("video") or result.get("url")) if isinstance(result, dict) else None
    if not url or not str(url).startswith("https://"):
        raise RuntimeError(f"Video manzili topilmadi: {str(data)[:400]}")
    v = requests.get(url, timeout=180)
    if not v.ok or not v.content:
        raise RuntimeError(f"Videoni yuklab bo'lmadi: HTTP {v.status_code}")
    return v.content


def generate_video_and_send(chat_id, user_id, text, image_data_uri=None):
    if len(text) > 2000:
        send_message(chat_id, "Matn 2000 belgidan qisqa bo'lsin.")
        return
    charged = False
    try:
        remaining = consume_credit(user_id)
        if remaining is None:
            send_message(chat_id, "Kreditingiz qolmagan. Kredit sotib oling.")
            return
        charged = True
        send_message(chat_id, "🎬 5 soniyalik HD video yaratilmoqda. Kuting...")
        prompt = make_english_prompt(text, "video")
        video = create_video(prompt, image_data_uri)
        send_video_bytes(chat_id, video,
                         f"✅ Motion AI video yaratdi!\nHD 720p, 5 soniya\n🔤 Prompt: {prompt[:300]}")
        log.info("Video yaratildi: user_id=%s, qolgan=%s", user_id, remaining)
    except Exception:
        log.exception("Video yaratish xatosi")
        fail_and_refund(chat_id, user_id, charged, "Video")


def handle_image_video_photo(message):
    chat_id = message["chat"]["id"]
    user_id = int(message["from"]["id"])
    try:
        data = get_telegram_file_bytes(message["photo"][-1]["file_id"], MAX_IMAGE_BYTES)
        uri = "data:image/jpeg;base64," + base64.b64encode(data).decode("utf-8")
        USER_STATE[user_id] = {"mode": "image_video_prompt", "image_data_uri": uri}
        send_message(chat_id,
                     "✅ Rasm qabul qilindi!\n\nEndi shu rasm asosida qanday video "
                     "yaratishni xohlayotganingizni yozing.\n\nMasalan: Kamera sekin "
                     "yaqinlashadi, daraxt barglari shamolda tebranadi.\n"
                     "/cancel — bekor qilish.")
    except Exception:
        log.exception("Rasmni qabul qilish xatosi")
        send_message(chat_id, "❌ Rasmni yuklab bo'lmadi. Boshqa rasm yuboring.")


# ===================== VIDEODAN RASM =====================


def extract_video_frame(video_bytes):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise RuntimeError("FFmpeg topilmadi. requirements.txt ga imageio-ffmpeg qo'shing.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "input.mp4"), os.path.join(tmp, "frame.jpg")
        with open(src, "wb") as f:
            f.write(video_bytes)
        res = subprocess.run([ffmpeg, "-y", "-ss", "00:00:01", "-i", src, "-frames:v", "1",
                              "-q:v", "2", dst], capture_output=True, text=True, timeout=90)
        if res.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("FFmpeg xatosi: " + res.stderr[-600:])
        with open(dst, "rb") as f:
            image = f.read()
        if not image:
            raise RuntimeError("Olingan rasm bo'sh.")
        return image


def handle_video_upload(message):
    chat_id = message["chat"]["id"]
    user_id = int(message["from"]["id"])
    video = message.get("video") or message.get("document") or {}
    if not video.get("file_id"):
        send_message(chat_id, "Video faylini topib bo'lmadi.")
        return
    if video.get("file_size", 0) > MAX_VIDEO_BYTES:
        send_message(chat_id, "Video hajmi 20 MB dan kichik bo'lishi kerak.")
        return
    charged = False
    try:
        send_message(chat_id, "🎞️ Videodan rasm ajratilmoqda...")
        image = extract_video_frame(get_telegram_file_bytes(video["file_id"], MAX_VIDEO_BYTES))
        remaining = consume_credit(user_id)
        if remaining is None:
            send_message(chat_id, "Kreditingiz qolmagan. Kredit sotib oling.")
            return
        charged = True
        send_image_bytes(chat_id, image, "✅ Videodan rasm olindi!")
        log.info("Videodan rasm olindi: user_id=%s, qolgan=%s", user_id, remaining)
    except Exception:
        log.exception("Videodan rasm olish xatosi")
        fail_and_refund(chat_id, user_id, charged, "Videodan rasm")


# ===================== TO'LOV =====================


def handle_receipt(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = int(user["id"])
    if not ADMIN_ID:
        send_message(chat_id, "Admin sozlanmagan.")
        return
    if not message.get("photo"):
        send_message(chat_id, "To'lov chekini rasm sifatida yuboring.")
        return
    try:
        payment = find_waiting_payment(user_id)
        if not payment:
            send_message(chat_id, "Chek kutayotgan to'lov topilmadi. "
                                  "Avval kredit paketini tanlang.")
            return
        file_id = message["photo"][-1]["file_id"]
        updated = sb_request("PATCH", "payments", prefer="return=representation",
                             params={"id": f"eq.{payment['id']}", "status": "eq.waiting"},
                             body={"status": "pending", "file_id": file_id})
        if not updated:
            send_message(chat_id, "Chek qabul qilinmadi. To'lov holatini tekshiring.")
            return
        markup = {"inline_keyboard": [[
            {"text": "✅ Tasdiqlash", "callback_data": f"approve:{payment['id']}"},
            {"text": "❌ Rad etish", "callback_data": f"reject:{payment['id']}"}]]}
        caption = (f"Yangi to'lov cheki\nIsm: {user.get('first_name', '-')}\n"
                   f"Username: @{user.get('username', '-')}\nID: {user_id}\n"
                   f"Kredit: {payment['credits']}\nSumma: {payment['amount']} so'm\n"
                   f"To'lov ID: {payment['id']}")
        try:
            tg_call("sendPhoto", {"chat_id": int(ADMIN_ID), "photo": file_id,
                                  "caption": caption, "reply_markup": markup})
        except Exception:
            log.exception("Admin uchun chek yuborilmadi")
            send_message(int(ADMIN_ID), caption, reply_markup=markup)
        send_message(chat_id, "✅ Chek qabul qilindi. Admin tekshirganidan keyin kredit beriladi.")
    except Exception:
        log.exception("Chekni qabul qilish xatosi")
        send_message(chat_id, "Chekni qabul qilishda xatolik yuz berdi.")


def handle_admin_payment(callback, action, payment_id):
    user_id = callback.get("from", {}).get("id")
    if not ADMIN_ID or str(user_id) != ADMIN_ID:
        send_message(user_id, "Bu amal faqat admin uchun.")
        return
    admin_chat = callback.get("message", {}).get("chat", {}).get("id", user_id)

    if action == "approve":
        result = sb_rpc("approve_motion_payment", {"p_payment_id": payment_id})
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("message", "Noma'lum holat") if isinstance(result, dict) else str(result)
            send_message(admin_chat, f"To'lov tasdiqlanmadi: {reason}")
            return
        buyer, credits, balance = int(result["user_id"]), int(result["credits"]), int(result["balance"])
        send_message(buyer, f"✅ To'lov tasdiqlandi!\nQo'shilgan kredit: {credits}\n"
                            f"Jami balans: {balance}")
        send_message(admin_chat, f"✅ To'lov tasdiqlandi. ID: {buyer}; kredit: {credits}.")
    else:
        updated = sb_request("PATCH", "payments", prefer="return=representation",
                             params={"id": f"eq.{payment_id}", "status": "eq.pending"},
                             body={"status": "rejected"})
        if not updated:
            send_message(admin_chat, "To'lov topilmadi yoki avval ko'rib chiqilgan.")
            return
        send_message(int(updated[0]["user_id"]), "❌ To'lov tasdiqlanmadi. Admin bilan bog'laning.")
        send_message(admin_chat, "To'lov rad etildi.")


def process_callback(callback):
    data = callback.get("data", "")
    user_id = callback.get("from", {}).get("id")
    chat_id = callback.get("message", {}).get("chat", {}).get("id", user_id)
    if callback.get("id"):
        answer_callback(callback["id"])
    try:
        if data.startswith("buy:"):
            credits = int(data.split(":", 1)[1])
            prices = dict(PACKS)
            if credits not in prices:
                send_message(chat_id, "Paket topilmadi.")
                return
            if not ADMIN_CARD or not CARD_OWNER:
                send_message(chat_id, "Admin kartasi yoki karta egasi sozlanmagan.")
                return
            pid = create_payment(user_id, credits, prices[credits])
            send_message(chat_id,
                         "💳 To'lov so'rovi yaratildi.\n\n"
                         f"Paket: {credits} ta kredit\n"
                         f"Summa: {prices[credits]:,} so'm\n".replace(",", " ")
                         + f"Karta: {ADMIN_CARD}\nKarta egasi: {CARD_OWNER}\n\n"
                           "Pul o'tkazgach, chekni rasm sifatida yuboring.\n"
                           f"So'rov ID: {pid}")
        elif data.startswith("approve:"):
            handle_admin_payment(callback, "approve", data.split(":", 1)[1])
        elif data.startswith("reject:"):
            handle_admin_payment(callback, "reject", data.split(":", 1)[1])
    except Exception:
        log.exception("Tugma bajarilmadi: %s", data)
        try:
            send_message(chat_id, "Amal bajarilmadi. Render Logs'ni tekshiring.")
        except Exception:
            log.exception("Xato xabari yuborilmadi")


# ===================== XABARLAR =====================

MENU_PROMPTS = {
    "🖼️ Rasm yaratish": (
        "image_prompt",
        "🖼️ Qanday rasm xohlayotganingizni yozing. Uzun va batafsil yozishingiz "
        "mumkin (2000 belgigacha), o'zbekcha ham bo'ladi.\n\n"
        "Masalan: Quyosh botayotgan paytda dengiz bo'yida turgan oq ot.\n"
        "/cancel — bekor qilish."),
    "🎬 Matndan video": (
        "video_prompt",
        "🎬 Qanday video xohlayotganingizni yozing. Uzun va batafsil yozishingiz "
        "mumkin, o'zbekcha ham bo'ladi.\n\n"
        "Masalan: Oq ot qorli tog'da yugursin, kamera uni orqasidan kuzatsin.\n"
        "Video: 5 soniya, HD 720p.\n/cancel — bekor qilish."),
    "📸 Rasmdan video": (
        "image_video_wait_image",
        "📸 Video yaratish uchun rasm yuboring. Keyin qanday harakat bo'lishini "
        "yozasiz.\nVideo: 5 soniya, HD 720p.\n/cancel — bekor qilish."),
    "🎞️ Videodan rasm": (
        "video_to_image_wait_video",
        "🎞️ Videodan rasm olish uchun MP4 video yuboring (20 MB gacha). "
        "Taxminan 1-soniyadagi kadr olinadi.\n/cancel — bekor qilish."),
}

HELP_TEXT = (
    "ℹ️ Motion AI yordam\n\n"
    "🖼️ Rasm yaratish — rasm haqida yozing.\n"
    "🎬 Matndan video — video tavsifini yozing.\n"
    "📸 Rasmdan video — rasm yuboring, keyin harakatni yozing.\n"
    "🎞️ Videodan rasm — video yuboring.\n"
    "💳 Kredit sotib olish — paketni tanlang.\n"
    "💰 Balans — qolgan kreditlar.\n\n"
    "Har bir amal 1 kredit sarflaydi. O'zbekcha yozishingiz mumkin.\n"
    "/cancel — joriy amalni bekor qilish.")


def process_message(message):
    if not message or not message.get("chat"):
        return
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    if user_id is None:
        return
    user_id = int(user_id)
    text = (message.get("text") or "").strip()
    state = USER_STATE.get(user_id)

    if text == "/start" or text.startswith("/start "):
        USER_STATE.pop(user_id, None)
        try:
            ensure_user(user_id)
            welcome = "🤖 Motion AI botiga xush kelibsiz!\n\nKerakli bo'limni tanlang."
        except Exception:
            log.exception("Foydalanuvchi bazasi xatosi")
            welcome = "Bazaga ulanishda xatolik yuz berdi. Admin loglarni tekshirishi kerak."
        send_message(chat_id, welcome, reply_markup=MAIN_MENU)
        return

    if text == "/cancel":
        USER_STATE.pop(user_id, None)
        send_message(chat_id, "Amal bekor qilindi.", reply_markup=MAIN_MENU)
        return

    if text in MENU_PROMPTS:
        new_state, reply = MENU_PROMPTS[text]
        USER_STATE[user_id] = new_state
        send_message(chat_id, reply)
        return

    if text == "💳 Kredit sotib olish":
        try:
            show_packs(chat_id)
        except Exception:
            log.exception("Kredit paketlari xatosi")
            send_message(chat_id, "Paketlarni ko'rsatib bo'lmadi.")
        return

    if text in ("💰 Balans", "/balance"):
        try:
            send_message(chat_id, f"💰 Joriy balans: {get_balance(user_id)} ta kredit")
        except Exception:
            log.exception("Balans xatosi")
            send_message(chat_id, "Balansni tekshirib bo'lmadi.")
        return

    if text == "ℹ️ Yordam":
        send_message(chat_id, HELP_TEXT)
        return

    if message.get("photo"):
        if state == "image_video_wait_image":
            handle_image_video_photo(message)
        else:
            handle_receipt(message)
        return

    if message.get("video"):
        if state == "video_to_image_wait_video":
            handle_video_upload(message)
        else:
            send_message(chat_id, "Videodan rasm olish uchun avval 🎞️ Videodan rasm tugmasini bosing.")
        return

    if message.get("document"):
        mime = message["document"].get("mime_type", "")
        if state == "video_to_image_wait_video" and mime.startswith("video/"):
            handle_video_upload(message)
        else:
            send_message(chat_id, "Bu fayl qo'llab-quvvatlanmaydi. Videoni MP4 formatida yuboring.")
        return

    if not text:
        return

    if state == "image_prompt":
        USER_STATE.pop(user_id, None)
        generate_and_send(chat_id, user_id, text)
    elif state == "video_prompt":
        USER_STATE.pop(user_id, None)
        generate_video_and_send(chat_id, user_id, text)
    elif isinstance(state, dict) and state.get("mode") == "image_video_prompt":
        uri = state.get("image_data_uri")
        USER_STATE.pop(user_id, None)
        if not uri:
            send_message(chat_id, "Rasm ma'lumoti topilmadi. Qaytadan boshlang.")
        else:
            generate_video_and_send(chat_id, user_id, text, image_data_uri=uri)
    elif state in ("video_to_image_wait_video", "image_video_wait_image"):
        send_message(chat_id, "Kerakli faylni yuboring yoki /cancel bosing.")
    else:
        send_message(chat_id, "Menyudagi tugmalardan birini tanlang.", reply_markup=MAIN_MENU)


def process_update(update):
    if "message" in update:
        process_message(update["message"])
    elif "callback_query" in update:
        process_callback(update["callback_query"])


# ===================== POLLING =====================


def polling_loop():
    if not POLL_LOCK.acquire(blocking=False):
        log.warning("Polling allaqachon ishlayapti.")
        return
    log.info("Telegram polling boshlandi")
    try:
        tg_call("deleteWebhook", {"drop_pending_updates": False}, timeout=20)
    except Exception:
        log.exception("Webhook tekshiruvi xatosi")
    offset = None
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            for update in tg_call("getUpdates", payload, timeout=35) or []:
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception:
                    log.exception("Update qayta ishlanmadi: %s", update.get("update_id"))
        except Exception:
            log.exception("Telegram polling xatosi")
            time.sleep(3)


# ===================== SERVER =====================


@app.get("/")
def home():
    try:
        sb_request("GET", "users", params={"select": "user_id", "limit": "1"})
    except Exception:
        log.warning("Supabase keepalive so'rovi bajarilmadi")
    return jsonify({"service": "Motion AI", "status": "running"})


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "telegram_configured": bool(BOT_TOKEN),
        "cloudflare_configured": bool(CF_ACCOUNT_ID and CF_API_TOKEN),
        "supabase_configured": bool(SB_URL and SB_KEY),
    })


if BOT_TOKEN:
    threading.Thread(target=polling_loop, daemon=True, name="telegram-polling").start()
else:
    log.error("BOT_TOKEN sozlanmagan")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False) 
    
