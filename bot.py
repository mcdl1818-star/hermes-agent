import os, json, asyncio, tempfile, httpx
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
from supabase import create_client

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USERS", "0"))
OPENROUTER_KEY = os.environ["OPENROUTER_API_KEY"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Clients ---
llm = AsyncOpenAI(
    api_key=OPENROUTER_KEY,
    base_url="https://openrouter.ai/api/v1"
)
groq = AsyncOpenAI(
    api_key=GROQ_KEY,
    base_url="https://api.groq.com/openai/v1"
)
db = create_client(SUPABASE_URL, SUPABASE_KEY)

SYSTEM_PROMPT = """אתה עוזר אישי חכם ואוטונומי שעובד בשביל המשתמש 24/7 דרך טלגרם.
אתה מכיר אותו לאורך זמן ומשתפר ככל שהשיחה מתקדמת.
ענה תמיד בעברית, בצורה קצרה וישירה.
כשהמשתמש מבקש ממך לעשות משהו - תעשה אותו לבד מקצה לקצה.
אתה זוכר את כל מה שהמשתמש סיפר לך עליו עצמו."""

async def get_memory(user_id: int) -> str:
    try:
        res = db.table("memory").select("content").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0]["content"]
    except:
        pass
    return ""

async def save_memory(user_id: int, content: str):
    try:
        existing = db.table("memory").select("id").eq("user_id", user_id).execute()
        if existing.data:
            db.table("memory").update({"content": content, "updated_at": datetime.utcnow().isoformat()}).eq("user_id", user_id).execute()
        else:
            db.table("memory").insert({"user_id": user_id, "content": content, "updated_at": datetime.utcnow().isoformat()}).execute()
    except:
        pass

async def get_history(user_id: int, limit: int = 20) -> list:
    try:
        res = db.table("messages").select("role,content").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
        if res.data:
            return list(reversed(res.data))
    except:
        pass
    return []

async def save_message(user_id: int, role: str, content: str):
    try:
        db.table("messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    except:
        pass

async def update_memory_from_conversation(user_id: int, history: list):
    if len(history) < 6:
        return
    current_memory = await get_memory(user_id)
    summary_prompt = f"""בהתבסס על השיחה הבאה, עדכן את הזיכרון הנוכחי על המשתמש.
זיכרון נוכחי: {current_memory or 'אין עדיין'}

שיחה אחרונה:
{chr(10).join([f"{m['role']}: {m['content']}" for m in history[-6:]])}

כתוב זיכרון מעודכן קצר (עד 300 מילים) שמכיל את כל מה שחשוב לזכור על המשתמש: מי הוא, מה הוא עושה, העדפות, בקשות חוזרות, מידע אישי שציין."""

    try:
        resp = await llm.chat.completions.create(
            model="qwen/qwen3-8b:free",
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=500
        )
        new_memory = resp.choices[0].message.content
        await save_memory(user_id, new_memory)
    except:
        pass

async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        resp = await groq.audio.transcriptions.create(
            model="whisper-large-v3",
            file=f,
            language="he"
        )
    return resp.text

async def chat(user_id: int, user_message: str) -> str:
    memory = await get_memory(user_id)
    history = await get_history(user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if memory:
        messages.append({"role": "system", "content": f"מה שאתה זוכר על המשתמש:\n{memory}"})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    resp = await llm.chat.completions.create(
        model="qwen/qwen3.6-plus:free",
        messages=messages,
        max_tokens=1000
    )
    answer = resp.choices[0].message.content

    await save_message(user_id, "user", user_message)
    await save_message(user_id, "assistant", answer)
    await update_memory_from_conversation(user_id, history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": answer}
    ])

    return answer

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text(f"גישה נדחתה. ה-ID שלך: {user_id}")
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    text = update.message.text
    answer = await chat(user_id, text)
    await update.message.reply_text(answer)

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

    voice = update.message.voice
    file = await ctx.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    await file.download_to_drive(tmp_path)

    try:
        text = await transcribe_voice(tmp_path)
        await update.message.reply_text(f"🎤 {text}")
        answer = await chat(user_id, text)
        await update.message.reply_text(answer)
    finally:
        os.unlink(tmp_path)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("Bot started - polling mode")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
