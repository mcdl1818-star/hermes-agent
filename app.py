"""
מרדי - עוזר אישי עברי מתקדם בטלגרם.
Function-calling אמין (תזכורות/תמונות/חיפוש/זיכרון) + רוטציית ספקים חינמיים,
תמלול עברי (Groq Whisper), זיכרון קבוע + תזכורות עם כפתורי דחייה (Supabase), תמונות FLUX.
"""
import os
import re
import json
import asyncio
import logging
import secrets
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
DIMS = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832)}
MAX_HISTORY = 24

app = FastAPI()
mem_lock = asyncio.Lock()
rem_lock = asyncio.Lock()
client: httpx.AsyncClient = None

TOOLS = [
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "קבע תזכורת למרדכי. השתמש בזה תמיד כשמרדכי מבקש שתזכיר לו משהו.",
        "parameters": {"type": "object", "properties": {
            "when": {"type": "string", "description": "לזמן יחסי: '+דקות' (למשל '+5' לחמש דקות, '+120' לשעתיים). לזמן מוחלט: ISO 'YYYY-MM-DDTHH:MM'."},
            "text": {"type": "string", "description": "מה להזכיר, קצר ובעברית"},
        }, "required": ["when", "text"]}}},
    {"type": "function", "function": {
        "name": "generate_image",
        "description": "צור תמונה כשמרדכי מבקש תמונה/ציור/צילום.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "תיאור מפורט באנגלית. אם ריאליסטי - הוסף photorealistic, ultra detailed, DSLR."},
            "orientation": {"type": "string", "enum": ["square", "portrait", "landscape"]},
        }, "required": ["prompt"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "חפש מידע עדכני באינטרנט (חדשות, מזג אוויר, מחירים, ספורט, אירועים, כל דבר שמשתנה או שאינך בטוח בו).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "save_fact",
        "description": "שמור עובדה קבועה וחשובה על מרדכי לזיכרון ארוך-טווח (שם, משפחה, עבודה, העדפות, אנשים, תאריכים).",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string"}}, "required": ["fact"]}}},
]


# ---------- Supabase storage ----------
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


# ---------- LLM (returns full message; supports tools) ----------
async def llm_call(messages, tools=None):
    body = {"messages": messages, "temperature": 0.5, "max_tokens": 1200}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    for name, base, key, model in PROVIDERS:
        if not key:
            continue
        try:
            r = await client.post(f"{base}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json={**body, "model": model}, timeout=60)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]
            log.warning("provider %s: %s %s", name, r.status_code, r.text[:120])
        except Exception as e:
            log.warning("provider %s err: %s", name, e)
    return {"role": "assistant", "content": "סליחה, כל הספקים תפוסים כרגע. נסה שוב עוד רגע."}


# ---------- Web search ----------
async def web_search(q):
    def _s():
        try:
            from ddgs import DDGS
            with DDGS() as d:
                return list(d.text(q, max_results=6, region="il-he"))
        except Exception as e:
            log.warning("search: %s", e)
            return []
    res = await asyncio.to_thread(_s)
    if not res:
        return "לא נמצאו תוצאות."
    return "\n".join(f"- {r.get('title','')}: {r.get('body','')}" for r in res[:6])


# ---------- Image (FLUX) ----------
async def gen_image(prompt, orientation="square"):
    w, h = DIMS.get(orientation, DIMS["square"])
    for model in IMAGE_MODELS:
        for _ in range(3):
            try:
                r = await client.post(f"https://router.huggingface.co/hf-inference/models/{model}",
                                      headers={"Authorization": f"Bearer {HF_TOKEN}", "Content-Type": "application/json"},
                                      json={"inputs": prompt, "parameters": {"width": w, "height": h}}, timeout=120)
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
    raise RuntimeError("image failed")


# ---------- STT ----------
async def transcribe(ogg):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ""
    try:
        r = await client.post("https://api.groq.com/openai/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {key}"},
                              files={"file": ("v.ogg", ogg, "audio/ogg")},
                              data={"model": "whisper-large-v3", "language": "he", "response_format": "text"}, timeout=60)
        if r.status_code == 200:
            return r.text.strip()
        log.warning("STT %s: %s", r.status_code, r.text[:100])
    except Exception as e:
        log.warning("STT err: %s", e)
    return ""


# ---------- Telegram ----------
async def tg_send(chat_id, text, buttons=None):
    p = {"chat_id": chat_id, "text": text}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    try:
        await client.post(f"{TG_API}/sendMessage", json=p, timeout=20)
    except Exception as e:
        log.warning("send: %s", e)


async def tg_photo(chat_id, img, caption=""):
    try:
        await client.post(f"{TG_API}/sendPhoto", data={"chat_id": str(chat_id), "caption": caption[:1000]},
                          files={"photo": ("i.jpg", img, "image/jpeg")}, timeout=60)
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


# ---------- Reminders ----------
def parse_when(when):
    when = str(when).strip()
    if when.startswith("+") or re.fullmatch(r"\d+", when):
        return datetime.now(TZ) + timedelta(minutes=int(re.sub(r"[^0-9]", "", when) or "0"))
    dt = datetime.fromisoformat(when)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def rem_buttons(rid):
    return [[{"text": "⏰ +10 דק'", "callback_data": f"snz:10:{rid}"},
             {"text": "⏰ +1 שעה", "callback_data": f"snz:60:{rid}"},
             {"text": "✓ בוצע", "callback_data": f"done:{rid}"}]]


async def add_reminder(chat_id, dt, text):
    rid = secrets.token_hex(3)
    async with rem_lock:
        rems = await _load("reminders.json", [])
        rems.append({"id": rid, "fire_at": dt.astimezone(TZ).isoformat(), "text": text, "sent": False, "chat_id": chat_id})
        await _save("reminders.json", rems)
    log.info("reminder set %s -> %s", dt.strftime("%H:%M"), text)
    return rid


async def save_fact(fact):
    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
        ex = mem.get("profile", "").split(" | ") if mem.get("profile") else []
        if fact not in ex:
            ex.append(fact)
        mem["profile"] = " | ".join(ex[-60:])
        await _save("memory.json", mem)


# ---------- Tool execution ----------
async def exec_tool(name, args, chat_id):
    """Returns (user_reply_text, search_result_or_None). Non-search tools build
    their own confirmation so no second LLM call is needed (saves rate limit)."""
    try:
        if name == "set_reminder":
            dt = parse_when(args["when"])
            await add_reminder(chat_id, dt, args.get("text", "תזכורת"))
            today = datetime.now(TZ).date()
            when_str = dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%d/%m בשעה %H:%M")
            return (f"✅ אזכיר לך ב-{when_str}: {args.get('text','')}", None)
        if name == "generate_image":
            await tg_action(chat_id, "upload_photo")
            img = await gen_image(args["prompt"], args.get("orientation", "square"))
            await tg_photo(chat_id, img, "🎨")
            return ("", None)  # photo already sent
        if name == "web_search":
            await tg_action(chat_id, "typing")
            return (None, await web_search(args["query"]))
        if name == "save_fact":
            await save_fact(args["fact"])
            return ("", None)  # silent
    except Exception as e:
        log.warning("tool %s err: %s", name, e)
        return (f"לא הצלחתי לבצע ({name}), נסה שוב.", None)
    return ("", None)


def system_prompt(profile):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (%A)")
    return (
        "אתה מרדי, העוזר האישי של מרדכי בטלגרם. ענה תמיד ורק בעברית, קצר וישיר וחם.\n"
        "בצע מיד בלי לשאול אישורים מיותרים. יש לך כלים אמיתיים - השתמש בהם:\n"
        "- כשמבקשים תזכורת → קרא ל-set_reminder.\n"
        "- כשמבקשים תמונה → קרא ל-generate_image.\n"
        "- כששאלה תלויה במידע עדכני → קרא ל-web_search.\n"
        "- כשלומד פרט קבוע על מרדכי → קרא ל-save_fact.\n"
        f"השעה הנוכחית בישראל: {now}.\n"
        f"מה שאתה כבר יודע על מרדכי: {profile or 'עדיין כלום'}."
    )


async def handle_message(chat_id, text):
    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
    profile = mem.get("profile", "")
    history = mem.get("messages", [])[-MAX_HISTORY:]

    messages = [{"role": "system", "content": system_prompt(profile)}] + history + [{"role": "user", "content": text}]

    msg = await llm_call(messages, tools=TOOLS)
    tcs = msg.get("tool_calls")
    final = (msg.get("content") or "").strip()

    if tcs:
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
        confirmations, searched = [], False
        for tc in tcs:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            reply_txt, search_res = await exec_tool(fn, args, chat_id)
            if search_res is not None:
                searched = True
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": search_res})
            else:
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": reply_txt or "בוצע"})
                if reply_txt:
                    confirmations.append(reply_txt)
        if searched:
            # one more call (no tools) to answer using search results
            msg2 = await llm_call(messages)
            final = (msg2.get("content") or "").strip()
        else:
            final = final or "\n".join(confirmations)

    if final:
        await tg_send(chat_id, final)

    async with mem_lock:
        mem = await _load("memory.json", {"profile": "", "messages": []})
        h = mem.get("messages", [])
        h.append({"role": "user", "content": text})
        h.append({"role": "assistant", "content": final or "[פעולה בוצעה]"})
        mem["messages"] = h[-MAX_HISTORY:]
        await _save("memory.json", mem)


# ---------- Callbacks ----------
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


# ---------- Reminder firing ----------
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
                await tg_send(r.get("chat_id", ALLOWED_USER), f"⏰ תזכורת: {r['text']}",
                              buttons=rem_buttons(r.get("id", secrets.token_hex(3))))
                r["sent"] = True
                fired = True
                log.info("FIRED: %s", r["text"])
        if fired:
            keep = [r for r in rems if not r.get("sent")]
            for r in rems:
                if r.get("sent"):
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
            await tg_send(chat_id, "היי מרדכי! אני מרדי 🤖 - תזכורות, תמונות, חיפוש באינטרנט וזיכרון. מה תרצה?")
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
