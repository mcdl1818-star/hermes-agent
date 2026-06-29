"""
מרדי - עוזר אישי עברי בטלגרם.
בוט קל: קריאת LLM אחת להודעה, רוטציה בין ספקים חינמיים (פותר rate limits),
תמלול עברי (Groq Whisper), זיכרון + תזכורות ב-Supabase Storage.
"""
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request

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

# LLM providers, tried in order; rotate to next on failure/rate-limit.
PROVIDERS = [
    ("cerebras", "https://api.cerebras.ai/v1", os.environ.get("CEREBRAS_API_KEY", ""), "gpt-oss-120b"),
    ("groq70",  "https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""), "llama-3.3-70b-versatile"),
    ("groq8",   "https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""), "llama-3.1-8b-instant"),
]
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
if NVIDIA_KEY:
    PROVIDERS.append(("nvidia", "https://integrate.api.nvidia.com/v1", NVIDIA_KEY, "meta/llama-3.3-70b-instruct"))

MAX_HISTORY = 16  # recent messages kept in context

app = FastAPI()
_lock = asyncio.Lock()
client: httpx.AsyncClient = None


# ---------- Supabase JSON storage ----------
async def _load(name: str, default):
    try:
        r = await client.get(f"{BUCKET}/{name}", headers=SUPA_HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning("load %s failed: %s", name, e)
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
        log.warning("save %s failed: %s", name, e)


# ---------- LLM with provider rotation ----------
async def llm(messages: list) -> str:
    last_err = ""
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
            last_err = f"{name} {r.status_code}: {r.text[:120]}"
            log.warning("provider %s failed: %s", name, last_err)
        except Exception as e:
            last_err = f"{name} {e}"
            log.warning("provider %s error: %s", name, e)
    return "סליחה, כל הספקים תפוסים כרגע. נסה שוב עוד רגע."


# ---------- Groq Whisper STT (forced Hebrew) ----------
async def transcribe(ogg_bytes: bytes) -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ""
    try:
        files = {"file": ("voice.ogg", ogg_bytes, "audio/ogg")}
        data = {"model": "whisper-large-v3", "language": "he", "response_format": "text"}
        r = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files=files, data=data, timeout=60,
        )
        if r.status_code == 200:
            return r.text.strip()
        log.warning("STT failed %s: %s", r.status_code, r.text[:120])
    except Exception as e:
        log.warning("STT error: %s", e)
    return ""


# ---------- Telegram ----------
async def tg_send(chat_id: int, text: str):
    try:
        await client.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception as e:
        log.warning("send failed: %s", e)


async def tg_typing(chat_id: int):
    try:
        await client.post(f"{TG_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        pass


async def tg_voice_bytes(file_id: str) -> bytes:
    r = await client.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=20)
    path = r.json()["result"]["file_path"]
    fr = await client.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60)
    return fr.content


# ---------- Prompt building ----------
def system_prompt(profile: str) -> str:
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (%A)")
    return (
        "אתה מרדי, העוזר האישי של מרדכי בטלגרם. ענה תמיד ורק בעברית, קצר וישיר וחם.\n"
        "בצע מיד בלי לשאול אישורים מיותרים. אם הבקשה ברורה - פשוט תעשה ותאשר בקצרה.\n"
        f"השעה הנוכחית בישראל: {now}.\n"
        f"מה שאתה כבר יודע על מרדכי: {profile or 'עדיין כלום'}.\n\n"
        "כללי פעולה מיוחדים (השתמש בהם בשקט - אל תסביר עליהם למשתמש):\n"
        "1. תזכורת: אם מרדכי מבקש שתזכיר לו משהו, הוסף בשורה נפרדת בסוף בדיוק את הפורמט:\n"
        "   REMINDER|YYYY-MM-DDTHH:MM|טקסט קצר של מה להזכיר\n"
        "   חשב את הזמן המדויק לפי השעה הנוכחית בישראל. ואז אשר לו בעברית: 'אזכיר לך בשעה HH:MM ...'.\n"
        "2. זיכרון: אם למדת עובדה קבועה וחשובה על מרדכי (שם, משפחה, העדפות, אנשים, הרגלים),\n"
        "   הוסף בשורה נפרדת: FACT|העובדה בקצרה.\n"
        "שורות REMINDER ו-FACT הן פנימיות - לעולם אל תכתוב אותן כחלק מהשיחה הגלויה."
    )


REMINDER_RE = re.compile(r"^REMINDER\|([0-9T:\-]+)\|(.+)$", re.MULTILINE)
FACT_RE = re.compile(r"^FACT\|(.+)$", re.MULTILINE)


async def handle_message(chat_id: int, user_id: int, text: str):
    mem = await _load("memory.json", {"profile": "", "messages": []})
    profile = mem.get("profile", "")
    history = mem.get("messages", [])[-MAX_HISTORY:]

    msgs = [{"role": "system", "content": system_prompt(profile)}]
    msgs += history
    msgs.append({"role": "user", "content": text})

    reply = await llm(msgs)

    # Extract reminders
    new_reminders = []
    for m in REMINDER_RE.finditer(reply):
        iso, rtext = m.group(1), m.group(2).strip()
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            new_reminders.append({"fire_at": dt.astimezone(TZ).isoformat(), "text": rtext, "sent": False, "chat_id": chat_id})
        except Exception as e:
            log.warning("bad reminder iso %s: %s", iso, e)

    # Extract facts
    new_facts = [m.group(1).strip() for m in FACT_RE.finditer(reply)]

    # Clean reply (strip internal lines)
    clean = REMINDER_RE.sub("", reply)
    clean = FACT_RE.sub("", clean).strip()
    if not clean:
        clean = "✅"

    await tg_send(chat_id, clean)

    # Persist reminders
    if new_reminders:
        rems = await _load("reminders.json", [])
        rems.extend(new_reminders)
        await _save("reminders.json", rems)
        log.info("added %d reminder(s)", len(new_reminders))

    # Persist memory
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": clean})
    mem["messages"] = history[-MAX_HISTORY:]
    if new_facts:
        existing = profile.split(" | ") if profile else []
        for f in new_facts:
            if f not in existing:
                existing.append(f)
        mem["profile"] = " | ".join(existing[-40:])
    await _save("memory.json", mem)


# ---------- Reminder loop ----------
async def reminder_loop():
    while True:
        try:
            rems = await _load("reminders.json", [])
            now = datetime.now(TZ)
            changed = False
            for r in rems:
                if r.get("sent"):
                    continue
                try:
                    fire = datetime.fromisoformat(r["fire_at"])
                except Exception:
                    r["sent"] = True
                    changed = True
                    continue
                if fire <= now:
                    await tg_send(r.get("chat_id", ALLOWED_USER), f"⏰ תזכורת: {r['text']}")
                    r["sent"] = True
                    changed = True
                    log.info("fired reminder: %s", r["text"])
            if changed:
                # keep only recent/unsent to avoid unbounded growth
                rems = [r for r in rems if not r.get("sent") or
                        (datetime.now(TZ) - datetime.fromisoformat(r["fire_at"])).total_seconds() < 86400]
                await _save("reminders.json", rems)
        except Exception as e:
            log.warning("reminder loop error: %s", e)
        await asyncio.sleep(30)


# ---------- Telegram polling loop ----------
async def poll_loop():
    offset = 0
    # drop backlog
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
                frm = msg.get("from") or {}
                uid = frm.get("id")
                chat_id = (msg.get("chat") or {}).get("id")
                if not chat_id:
                    continue
                if ALLOWED_USER and uid != ALLOWED_USER:
                    await tg_send(chat_id, "גישה נדחתה.")
                    continue
                asyncio.create_task(_route(chat_id, uid, msg))
        except Exception as e:
            log.warning("poll error: %s", e)
            await asyncio.sleep(3)


async def _route(chat_id: int, uid: int, msg: dict):
    try:
        await tg_typing(chat_id)
        text = msg.get("text")
        if not text and msg.get("voice"):
            ob = await tg_voice_bytes(msg["voice"]["file_id"])
            text = await transcribe(ob)
            if not text:
                await tg_send(chat_id, "לא הצלחתי להבין את ההקלטה, נסה שוב.")
                return
        if not text:
            return
        if text.strip() in ("/start", "/new"):
            if text.strip() == "/new":
                await _save("memory.json", {"profile": (await _load("memory.json", {})).get("profile", ""), "messages": []})
            await tg_send(chat_id, "היי מרדכי! אני מרדי, העוזר שלך. מה תרצה?")
            return
        async with _lock:
            await handle_message(chat_id, uid, text)
    except Exception as e:
        log.warning("route error: %s", e)
        await tg_send(chat_id, "אופס, נתקלתי בתקלה. נסה שוב.")


# ---------- FastAPI (keep-alive) ----------
@app.get("/")
async def root():
    return "Mordi OK"


@app.on_event("startup")
async def startup():
    global client
    client = httpx.AsyncClient()
    asyncio.create_task(poll_loop())
    asyncio.create_task(reminder_loop())
    log.info("Mordi started")
