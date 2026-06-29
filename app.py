"""
מרדי - עוזר אישי עברי מתקדם בטלגרם.
קריאת LLM אחת להודעה (שתיים בחיפוש) + רוטציית ספקים חינמיים, תמלול עברי (Groq Whisper),
זיכרון קבוע + תזכורות עם כפתורי דחייה (Supabase), חיפוש אינטרנט, יצירת תמונות (FLUX).
"""
import os
import re
import json
import asyncio
import logging
import secrets
from urllib.parse import quote
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mordi")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER = int(os.environ.get("TELEGRAM_ALLOWED_USERS", "0"))
SUPA_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPA_KEY = os.environ["SUPABASE_KEY"]
HF_TOKEN = os.environ.get("HF_TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Asia/Jerusalem")

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
BUCKET = f"{SUPA_URL}/storage/v1/object/hermes-state"
SUPA_HEADERS = {"Authorization": f"Bearer {SUPA_KEY}", "apikey": SUPA_KEY}

PROVIDERS = [
    ("cerebras", "https://api.cerebras.ai/v1", os.environ.get("CEREBRAS_API_KEY", ""), "gpt-oss-120b"),
    ("groq70",  "https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""), "llama-3.3-70b-versatile"),
    ("groq8",   "https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""), "llama-3.1-8b-instant"),
]
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
if NVIDIA_KEY:
    PROVIDERS.append(("nvidia", "https://integrate.api.nvidia.com/v1", NVIDIA_KEY, "meta/llama-3.3-70b-instruct"))

IMAGE_MODELS = ["black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-xl-base-1.0"]
MAX_HISTORY = 24

app = FastAPI()
mem_lock = asyncio.Lock()
rem_lock = asyncio.Lock()
client: httpx.AsyncClient = None


# ---------- Supabase JSON storage ----------
async def _load(name, default):
    try:
        r = await client.get(f"{BUCKET}/{name}", headers=SUPA_HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning("load %s: %s", name, e)
    return default


async def _save(name, data):
    try:
        await client.post(f"{BUCKET}/{name}",
                          headers={**SUPA_HEADERS, "x-upsert": "true", "Content-Type": "application/json"},
                          content=json.dumps(data, ensure_ascii=False).encode("utf-8"), timeout=15)
    except Exception as e:
        log.warning("save %s: %s", name, e)


# ---------- LLM rotation ----------
async def llm(messages):
    for name, base, key, model in PROVIDERS:
        if not key:
            continue
        try:
            r = await client.post(f"{base}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json={"model": model, "messages": messages, "temperature": 0.6, "max_tokens": 1200},
                                  timeout=60)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            log.warning("provider %s: %s %s", name, r.status_code, r.text[:100])
        except Exception as e:
            log.warning("provider %s err: %s", name, e)
    return "סליחה, כל הספקים תפוסים כרגע. נסה שוב עוד רגע."


# ---------- Web search ----------
async def web_search(q):
    def _s():
        try:
            from ddgs import DDGS
            with DDGS() as d:
                return list(d.text(q, max_results=6, region="il-he"))
        except Exception as e:
            log.warning("search err: %s", e)
            return []
    res = await asyncio.to_thread(_s)
    if not res:
        return "לא נמצאו תוצאות."
    return "\n".join(f"- {r.get('title','')}: {r.get('body','')}" for r in res[:6])


# ---------- Image generation (FLUX via HuggingFace) ----------
DIMS = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832)}
QUALITY = ", highly detailed, sharp focus, professional, cinematic lighting, 8k"
REALISM = ", photorealistic, ultra realistic, DSLR photo, natural lighting, lifelike, high detail"


async def gen_image(prompt, orientation="square"):
    w, h = DIMS.get(orientation, DIMS["square"])
    low = prompt.lower()
    full = prompt + (REALISM if any(k in low for k in ("photo", "realis", "real ", "person", "portrait", "man", "woman", "face")) else QUALITY)
    for model in IMAGE_MODELS:
        for attempt in range(3):
            try:
                r = await client.post(f"https://router.huggingface.co/hf-inference/models/{model}",
                                      headers={"Authorization": f"Bearer {HF_TOKEN}", "Content-Type": "application/json"},
                                      json={"inputs": full, "parameters": {"width": w, "height": h}}, timeout=120)
                if r.status_code == 200 and r.content[:2] in (b"\xff\xd8", b"\x89P"):
                    return r.content
                if r.status_code == 503:
                    await asyncio.sleep(8)
                    continue
                log.warning("img %s: %s %s", model, r.status_code, r.text[:120])
                break
            except Exception as e:
                log.warning("img %s err: %s", model, e)
                break
    raise RuntimeError("image gen failed")


# ---------- Groq Whisper STT ----------
async def transcribe(ogg):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ""
    try:
        r = await client.post("https://api.groq.com/openai/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {key}"},
                              files={"file": ("v.ogg", ogg, "audio/ogg")},
                              data={"model": "whisper-large-v3", "language": "he", "response_format": "text"},
                              timeout=60)
        if r.status_code == 200:
            return r.text.strip()
        log.warning("STT %s: %s", r.status_code, r.text[:100])
    except Exception as e:
        log.warning("STT err: %s", e)
    return ""


# ---------- Telegram ----------
async def tg_send(chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        await client.post(f"{TG_API}/sendMessage", json=payload, timeout=20)
    except Exception as e:
        log.warning("send: %s", e)


async def tg_photo(chat_id, img, caption=""):
    try:
        await client.post(f"{TG_API}/sendPhoto",
                          data={"chat_id": str(chat_id), "caption": caption[:1000]},
                          files={"photo": ("img.jpg", img, "image/jpeg")}, timeout=60)
    except Exception as e:
        log.warning("photo: %s", e)


async def tg_action(chat_id, action):
    try:
        await client.post(f"{TG_API}/sendChatAction", json={"chat_id": chat_id, "action": action}, timeout=10)
    except Exception:
        pass


async def tg_answer_cb(cb_id, text=""):
    try:
        await client.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": text}, timeout=10)
    except Exception:
        pass


async def tg_voice_bytes(file_id):
    r = await client.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=20)
    path = r.json()["result"]["file_path"]
    fr = await client.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60)
    return fr.content


# ---------- Prompt ----------
def system_prompt(profile):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (%A)")
    return (
        "אתה מרדי, העוזר האישי של מרדכי בטלגרם. ענה תמיד ורק בעברית, קצר וישיר וחם.\n"
        "בצע מיד בלי לשאול אישורים מיותרים. אם הבקשה ברורה - פשוט תעשה ותאשר בקצרה.\n"
        f"השעה הנוכחית בישראל: {now}.\n"
        f"מה שאתה כבר יודע על מרדכי: {profile or 'עדיין כלום'}.\n\n"
        "יש לך גישה מלאה לאינטרנט בזמן אמת. כלים (השתמש בשורה נפרדת, אל תסביר עליהם למשתמש):\n"
        "• חיפוש: לכל שאלה שתלויה במידע עדכני (חדשות, מזג אוויר, מחירים, ספורט, אירועים, שעות פתיחה, "
        "כל עובדה שעלולה להשתנות או שאינך בטוח בה ב-100%) כתוב **רק** את השורה: SEARCH|מה לחפש - ותקבל תוצאות.\n"
        "• תזכורת: REMINDER|מתי|טקסט קצר. ל'עוד X דקות/שעות' כתוב +דקות (למשל +5 לחמש דקות, +90 לשעה וחצי). "
        "לזמן מוחלט (מחר, שעה ספציפית, תאריך) כתוב ISO: YYYY-MM-DDTHH:MM. אחר כך אשר בעברית 'אזכיר לך ...'.\n"
        "• עובדה לזיכרון קבוע: FACT|העובדה  (שמור כל פרט קבוע על מרדכי - שם, משפחה, עבודה, העדפות, אנשים, הרגלים, תאריכים).\n"
        "• תמונה: IMAGE|כיוון|prompt מפורט באנגלית. כיוון = square/portrait/landscape (לפי מה שמתאים). "
        "כתוב prompt עשיר ומפורט; אם מבקשים ריאליסטי הוסף תיאור צילומי מפורט.\n"
        "שורות הכלים פנימיות - לעולם אל תציג אותן בשיחה."
    )


RE_REM = re.compile(r"^REMINDER\|([^|]+)\|(.+)$", re.MULTILINE)
RE_FACT = re.compile(r"^FACT\|(.+)$", re.MULTILINE)
RE_SEARCH = re.compile(r"^SEARCH\|(.+)$", re.MULTILINE)
RE_IMG = re.compile(r"^IMAGE\|(.+)$", re.MULTILINE)


def parse_when(when: str):
    """Relative '+5' (minutes) computed in code (reliable), else ISO datetime."""
    when = when.strip()
    if when.startswith("+"):
        return datetime.now(TZ) + timedelta(minutes=int(re.sub(r"[^0-9]", "", when) or "0"))
    dt = datetime.fromisoformat(when)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def rem_buttons(rid):
    return [[
        {"text": "⏰ +10 דק'", "callback_data": f"snz:10:{rid}"},
        {"text": "⏰ +1 שעה", "callback_data": f"snz:60:{rid}"},
        {"text": "✓ בוצע", "callback_data": f"done:{rid}"},
    ]]


async def add_reminder(chat_id, dt, text):
    rid = secrets.token_hex(3)
    async with rem_lock:
        rems = await _load("reminders.json", [])
        rems.append({"id": rid, "fire_at": dt.astimezone(TZ).isoformat(),
                     "text": text, "sent": False, "chat_id": chat_id})
        await _save("reminders.json", rems)
    return rid


async def handle_message(chat_id, text):
    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
    profile = mem.get("profile", "")
    history = mem.get("messages", [])[-MAX_HISTORY:]

    msgs = [{"role": "system", "content": system_prompt(profile)}] + history + [{"role": "user", "content": text}]
    reply = await llm(msgs)

    sm = RE_SEARCH.search(reply)
    if sm:
        await tg_action(chat_id, "typing")
        results = await web_search(sm.group(1).strip())
        msgs.append({"role": "assistant", "content": reply})
        msgs.append({"role": "user", "content": f"תוצאות חיפוש עדכניות:\n{results}\n\nענה למרדכי בעברית קצר וברור על סמך זה."})
        reply = await llm(msgs)

    im = RE_IMG.search(reply)
    if im:
        await tg_action(chat_id, "upload_photo")
        spec = im.group(1).strip()
        if "|" in spec:
            orient, iprompt = spec.split("|", 1)
            orient = orient.strip().lower()
        else:
            orient, iprompt = "square", spec
        if orient not in DIMS:
            orient = "square"
        try:
            await tg_photo(chat_id, await gen_image(iprompt.strip(), orient), "🎨")
        except Exception as e:
            log.warning("img: %s", e)
            await tg_send(chat_id, "לא הצלחתי ליצור את התמונה כרגע, נסה שוב.")

    for m in RE_REM.finditer(reply):
        try:
            await add_reminder(chat_id, parse_when(m.group(1)), m.group(2).strip())
        except Exception as e:
            log.warning("bad reminder: %s", e)

    new_facts = [m.group(1).strip() for m in RE_FACT.finditer(reply)]

    clean = RE_REM.sub("", reply)
    clean = RE_FACT.sub("", clean)
    clean = RE_IMG.sub("", clean)
    clean = RE_SEARCH.sub("", clean).strip()
    if clean:
        await tg_send(chat_id, clean)
    elif not im:
        await tg_send(chat_id, "✅")

    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
        hist = mem.get("messages", [])
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": clean or "[תמונה]"})
        mem["messages"] = hist[-MAX_HISTORY:]
        if new_facts:
            ex = mem.get("profile", "").split(" | ") if mem.get("profile") else []
            for f in new_facts:
                if f and f not in ex:
                    ex.append(f)
            mem["profile"] = " | ".join(ex[-60:])
        await _save("memory.json", mem)


# ---------- Callback (buttons) ----------
async def handle_callback(cb):
    data = cb.get("data", "")
    cb_id = cb["id"]
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id", ALLOWED_USER)
    if data.startswith("done:"):
        await tg_answer_cb(cb_id, "סומן כבוצע ✓")
        return
    if data.startswith("snz:"):
        _, mins, rid = data.split(":", 2)
        async with rem_lock:
            rems = await _load("reminders.json", [])
            orig = next((r for r in rems if r.get("id") == rid), None)
            txt = orig["text"] if orig else "תזכורת"
        new_dt = datetime.now(TZ) + timedelta(minutes=int(mins))
        await add_reminder(chat_id, new_dt, txt)
        await tg_answer_cb(cb_id, f"נדחה ל-{new_dt.strftime('%H:%M')}")
        await tg_send(chat_id, f"⏰ אזכיר לך שוב ב-{new_dt.strftime('%H:%M')}: {txt}")
        return
    await tg_answer_cb(cb_id)


# ---------- Reminders ----------
async def check_reminders():
    async with rem_lock:
        rems = await _load("reminders.json", [])
        now = datetime.now(TZ)
        fired = False
        for r in rems:
            if r.get("sent"):
                continue
            try:
                fire = datetime.fromisoformat(r["fire_at"])
            except Exception:
                r["sent"] = True
                fired = True
                continue
            if fire <= now:
                rid = r.get("id", secrets.token_hex(3))
                await tg_send(r.get("chat_id", ALLOWED_USER), f"⏰ תזכורת: {r['text']}", buttons=rem_buttons(rid))
                r["sent"] = True
                fired = True
                log.info("fired: %s", r["text"])
        if fired:
            keep = []
            for r in rems:
                if not r.get("sent"):
                    keep.append(r)
                else:
                    try:
                        if (now - datetime.fromisoformat(r["fire_at"])).total_seconds() < 86400:
                            keep.append(r)
                    except Exception:
                        pass
            await _save("reminders.json", keep)


async def reminder_loop():
    while True:
        try:
            await check_reminders()
        except Exception as e:
            log.warning("rem loop: %s", e)
        await asyncio.sleep(30)


# ---------- Polling ----------
async def poll_loop():
    offset = 0
    try:
        r = await client.get(f"{TG_API}/getUpdates", params={"offset": -1, "timeout": 0}, timeout=20)
        res = r.json().get("result", [])
        if res:
            offset = res[-1]["update_id"] + 1
    except Exception:
        pass
    log.info("polling started")
    while True:
        try:
            r = await client.get(f"{TG_API}/getUpdates",
                                 params={"offset": offset, "timeout": 50, "allowed_updates": '["message","callback_query"]'},
                                 timeout=60)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    if not ALLOWED_USER or (cb.get("from") or {}).get("id") == ALLOWED_USER:
                        asyncio.create_task(handle_callback(cb))
                    continue
                msg = upd.get("message") or {}
                uid = (msg.get("from") or {}).get("id")
                chat_id = (msg.get("chat") or {}).get("id")
                if not chat_id:
                    continue
                if ALLOWED_USER and uid != ALLOWED_USER:
                    await tg_send(chat_id, "גישה נדחתה.")
                    continue
                asyncio.create_task(route(chat_id, msg))
        except Exception as e:
            log.warning("poll: %s", e)
            await asyncio.sleep(3)


async def route(chat_id, msg):
    try:
        await tg_action(chat_id, "typing")
        text = msg.get("text")
        if not text and msg.get("voice"):
            text = await transcribe(await tg_voice_bytes(msg["voice"]["file_id"]))
            if not text:
                await tg_send(chat_id, "לא הצלחתי להבין את ההקלטה, נסה שוב.")
                return
        if not text:
            return
        if text.strip() in ("/start", "/new"):
            if text.strip() == "/new":
                async with mem_lock:
                    cur = await _load("memory.json", {})
                    await _save("memory.json", {"profile": cur.get("profile", ""), "messages": []})
            await tg_send(chat_id, "היי מרדכי! אני מרדי 🤖\nאני מחובר לאינטרנט - יכול לחפש מידע עדכני, ליצור תמונות, לקבוע תזכורות (עם דחייה), ולזכור אותך לאורך זמן. מה תרצה?")
            return
        await handle_message(chat_id, text)
    except Exception as e:
        log.warning("route: %s", e)
        await tg_send(chat_id, "אופס, תקלה רגעית. נסה שוב.")


@app.get("/")
async def root():
    return "Mordi OK"


@app.api_route("/tick", methods=["GET", "HEAD"])
async def tick():
    await check_reminders()
    return "tick"


@app.on_event("startup")
async def startup():
    global client
    client = httpx.AsyncClient()
    asyncio.create_task(poll_loop())
    asyncio.create_task(reminder_loop())
    log.info("Mordi started")
