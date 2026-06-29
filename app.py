"""
מרדי - עוזר אישי עברי בטלגרם.
בוט קל: קריאת LLM אחת להודעה (שתיים כשצריך חיפוש), רוטציה בין ספקים חינמיים,
תמלול עברי (Groq Whisper), זיכרון + תזכורות ב-Supabase, חיפוש אינטרנט, יצירת תמונות.
"""
import os
import re
import json
import asyncio
import logging
from urllib.parse import quote
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mordi")

# ---------- Config ----------
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER = int(os.environ.get("TELEGRAM_ALLOWED_USERS", "0"))
SUPA_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPA_KEY = os.environ["SUPABASE_KEY"]
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

MAX_HISTORY = 16

app = FastAPI()
mem_lock = asyncio.Lock()
rem_lock = asyncio.Lock()
client: httpx.AsyncClient = None


# ---------- Supabase JSON storage ----------
async def _load(name: str, default):
    try:
        r = await client.get(f"{BUCKET}/{name}", headers=SUPA_HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning("load %s: %s", name, e)
    return default


async def _save(name: str, data):
    try:
        await client.post(
            f"{BUCKET}/{name}",
            headers={**SUPA_HEADERS, "x-upsert": "true", "Content-Type": "application/json"},
            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            timeout=15,
        )
    except Exception as e:
        log.warning("save %s: %s", name, e)


# ---------- LLM with provider rotation ----------
async def llm(messages: list) -> str:
    last = ""
    for name, base, key, model in PROVIDERS:
        if not key:
            continue
        try:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.6, "max_tokens": 1200},
                timeout=60,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            last = f"{name} {r.status_code}"
            log.warning("provider %s: %s %s", name, r.status_code, r.text[:100])
        except Exception as e:
            last = f"{name} {e}"
            log.warning("provider %s err: %s", name, e)
    return "סליחה, כל הספקים תפוסים כרגע. נסה שוב עוד רגע."


# ---------- Web search (free, no key) ----------
async def web_search(q: str) -> str:
    def _search():
        try:
            from ddgs import DDGS
            with DDGS() as d:
                return list(d.text(q, max_results=5, region="il-he"))
        except Exception as e:
            log.warning("search err: %s", e)
            return []
    results = await asyncio.to_thread(_search)
    if not results:
        return "לא נמצאו תוצאות."
    return "\n".join(f"- {r.get('title','')}: {r.get('body','')}" for r in results[:5])


# ---------- Image generation (free, no key) ----------
async def gen_image(prompt: str) -> bytes:
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1024&height=1024&nologo=true"
    r = await client.get(url, timeout=120)
    if r.status_code == 200 and r.content[:2] in (b"\xff\xd8", b"\x89P"):
        return r.content
    raise RuntimeError(f"image gen failed {r.status_code}")


# ---------- Groq Whisper STT (forced Hebrew) ----------
async def transcribe(ogg: bytes) -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ""
    try:
        r = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("voice.ogg", ogg, "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "he", "response_format": "text"},
            timeout=60,
        )
        if r.status_code == 200:
            return r.text.strip()
        log.warning("STT %s: %s", r.status_code, r.text[:100])
    except Exception as e:
        log.warning("STT err: %s", e)
    return ""


# ---------- Telegram ----------
async def tg_send(chat_id: int, text: str):
    try:
        await client.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception as e:
        log.warning("send: %s", e)


async def tg_photo(chat_id: int, img: bytes, caption: str = ""):
    try:
        await client.post(
            f"{TG_API}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption[:1000]},
            files={"photo": ("image.jpg", img, "image/jpeg")},
            timeout=60,
        )
    except Exception as e:
        log.warning("photo: %s", e)


async def tg_action(chat_id: int, action: str):
    try:
        await client.post(f"{TG_API}/sendChatAction", json={"chat_id": chat_id, "action": action}, timeout=10)
    except Exception:
        pass


async def tg_voice_bytes(file_id: str) -> bytes:
    r = await client.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=20)
    path = r.json()["result"]["file_path"]
    fr = await client.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60)
    return fr.content


# ---------- Prompt ----------
def system_prompt(profile: str) -> str:
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (%A)")
    return (
        "אתה מרדי, העוזר האישי של מרדכי בטלגרם. ענה תמיד ורק בעברית, קצר וישיר וחם.\n"
        "בצע מיד בלי לשאול אישורים מיותרים. אם הבקשה ברורה - פשוט תעשה ותאשר בקצרה.\n"
        f"השעה הנוכחית בישראל: {now}.\n"
        f"מה שאתה כבר יודע על מרדכי: {profile or 'עדיין כלום'}.\n\n"
        "כלים (השתמש בהם בשקט, בשורה נפרדת, אל תסביר עליהם):\n"
        "• תזכורת: REMINDER|YYYY-MM-DDTHH:MM|טקסט  (חשב לפי השעה בישראל, ואז אשר 'אזכיר לך בשעה HH:MM').\n"
        "• עובדה לזיכרון: FACT|העובדה  (כשלומד משהו קבוע על מרדכי).\n"
        "• חיפוש באינטרנט: אם צריך מידע עדכני/חדשות/מזג אוויר/מחירים שאינך יודע, כתוב **רק** SEARCH|מה לחפש (בלי טקסט נוסף), ותקבל תוצאות להמשך.\n"
        "• יצירת תמונה: אם מרדכי מבקש תמונה/ציור, כתוב IMAGE|תיאור התמונה באנגלית מפורט.\n"
        "שורות הכלים פנימיות - לעולם אל תציג אותן כחלק מהשיחה."
    )


RE_REM = re.compile(r"^REMINDER\|([0-9T:\-]+)\|(.+)$", re.MULTILINE)
RE_FACT = re.compile(r"^FACT\|(.+)$", re.MULTILINE)
RE_SEARCH = re.compile(r"^SEARCH\|(.+)$", re.MULTILINE)
RE_IMG = re.compile(r"^IMAGE\|(.+)$", re.MULTILINE)


async def handle_message(chat_id: int, text: str):
    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
    profile = mem.get("profile", "")
    history = mem.get("messages", [])[-MAX_HISTORY:]

    msgs = [{"role": "system", "content": system_prompt(profile)}] + history + [{"role": "user", "content": text}]
    reply = await llm(msgs)

    # Web search round (max 1)
    sm = RE_SEARCH.search(reply)
    if sm:
        await tg_action(chat_id, "typing")
        results = await web_search(sm.group(1).strip())
        msgs.append({"role": "assistant", "content": reply})
        msgs.append({"role": "user", "content": f"תוצאות חיפוש:\n{results}\n\nענה למרדכי בעברית על סמך זה."})
        reply = await llm(msgs)

    # Image generation
    im = RE_IMG.search(reply)
    if im:
        await tg_action(chat_id, "upload_photo")
        try:
            img = await gen_image(im.group(1).strip())
            await tg_photo(chat_id, img, "הנה התמונה 🎨")
        except Exception as e:
            log.warning("img gen: %s", e)
            await tg_send(chat_id, "לא הצלחתי ליצור את התמונה כרגע, נסה שוב.")

    # Reminders
    new_rem = []
    for m in RE_REM.finditer(reply):
        try:
            dt = datetime.fromisoformat(m.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            new_rem.append({"fire_at": dt.astimezone(TZ).isoformat(), "text": m.group(2).strip(),
                            "sent": False, "chat_id": chat_id})
        except Exception as e:
            log.warning("bad reminder: %s", e)
    if new_rem:
        async with rem_lock:
            rems = await _load("reminders.json", [])
            rems.extend(new_rem)
            await _save("reminders.json", rems)
        log.info("added %d reminders", len(new_rem))

    # Facts
    new_facts = [m.group(1).strip() for m in RE_FACT.finditer(reply)]

    # Clean + send
    clean = RE_REM.sub("", reply)
    clean = RE_FACT.sub("", clean)
    clean = RE_IMG.sub("", clean)
    clean = RE_SEARCH.sub("", clean).strip()
    if clean:
        await tg_send(chat_id, clean)
    elif not im:
        await tg_send(chat_id, "✅")

    # Save memory
    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
        hist = mem.get("messages", [])
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": clean or "[תמונה]"})
        mem["messages"] = hist[-MAX_HISTORY:]
        if new_facts:
            existing = mem.get("profile", "").split(" | ") if mem.get("profile") else []
            for f in new_facts:
                if f and f not in existing:
                    existing.append(f)
            mem["profile"] = " | ".join(existing[-40:])
        await _save("memory.json", mem)


# ---------- Reminder firing (lock-guarded, no race) ----------
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
                await tg_send(r.get("chat_id", ALLOWED_USER), f"⏰ תזכורת: {r['text']}")
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


# ---------- Telegram polling ----------
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
            r = await client.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
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


async def route(chat_id: int, msg: dict):
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
            await tg_send(chat_id, "היי מרדכי! אני מרדי, העוזר שלך - תזכורות, חיפוש, תמונות ועוד. מה תרצה?")
            return
        await handle_message(chat_id, text)
    except Exception as e:
        log.warning("route: %s", e)
        await tg_send(chat_id, "אופס, תקלה רגעית. נסה שוב.")


# ---------- FastAPI ----------
@app.get("/")
async def root():
    return "Mordi OK"


@app.get("/tick")
async def tick():
    # External cron hits this every minute: keeps the service awake AND fires
    # any due reminders immediately, even if the in-process loop missed them.
    await check_reminders()
    return "tick"


@app.on_event("startup")
async def startup():
    global client
    client = httpx.AsyncClient()
    asyncio.create_task(poll_loop())
    asyncio.create_task(reminder_loop())
    log.info("Mordi started")
