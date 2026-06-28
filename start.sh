#!/bin/bash

# Memory tuning for 512MB container: limit glibc malloc arenas (threaded
# Python otherwise spawns many per-thread arenas that inflate RSS) and trim
# freed memory back to the OS aggressively. Classic fix for Python OOM in
# small containers - typically saves 100-200MB RSS.
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=100000
export PYTHONMALLOC=malloc
export PYTHONUNBUFFERED=1
export STT_LANGUAGE=he

HERMES_HOME="/root/.hermes"
mkdir -p "$HERMES_HOME"
LOGFILE="/tmp/hermes.log"
PORT=${PORT:-10000}

cat > "$HERMES_HOME/.env" << ENVEOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
GROQ_API_KEY=${GROQ_API_KEY}
CEREBRAS_API_KEY=${CEREBRAS_API_KEY}
OPENAI_API_KEY=${CEREBRAS_API_KEY}
OPENAI_BASE_URL=https://api.cerebras.ai/v1
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS}
TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL}
SUPABASE_URL=${SUPABASE_URL}
SUPABASE_KEY=${SUPABASE_KEY}
ENVEOF

cat > "$HERMES_HOME/config.yaml" << YAMLEOF
max_live_sessions: 2
max_concurrent_sessions: 2
timezone: "Asia/Jerusalem"
toolsets:
  - "hermes-cli"
  - "web"
  - "todo"
  - "memory"
  - "cronjob"
web:
  search_backend: "ddgs"
  extract_backend: "ddgs"
agent:
  reasoning_effort: "low"
  max_turns: 18
  gateway_timeout: 240
  api_max_retries: 2
  disabled_toolsets:
    - "clarify"
gateway:
  message_timestamps:
    enabled: true
model:
  default: "gpt-oss-120b"
  provider: "openai-api"
  context_length: 64000
stt:
  enabled: true
  provider: "groq"
fallback_providers:
  # Different provider (Groq) with its OWN daily quota - when Cerebras hits its
  # daily/minute token limit, fail over here instead of dying. Small requests
  # (~5K tokens after compression) fit Groq's 12K TPM comfortably.
  - provider: "openai-api"
    model: "llama-3.3-70b-versatile"
    base_url: "https://api.groq.com/openai/v1"
    api_key: "${GROQ_API_KEY}"
  - provider: "openai-api"
    model: "llama-3.1-8b-instant"
    base_url: "https://api.groq.com/openai/v1"
    api_key: "${GROQ_API_KEY}"
auxiliary:
  # Background tasks routed to Groq (separate rate-limit bucket from Cerebras),
  # so they never compete with user-facing responses for Cerebras's 5 RPM.
  background_review:
    base_url: "https://api.groq.com/openai/v1"
    api_key: "${GROQ_API_KEY}"
    model: "llama-3.3-70b-versatile"
  title_generation:
    base_url: "https://api.groq.com/openai/v1"
    api_key: "${GROQ_API_KEY}"
    model: "llama-3.1-8b-instant"
  # Compression handles large contexts - keep on Cerebras (30K TPM headroom).
  compression:
    provider: "openai-api"
    model: "gpt-oss-120b"
  vision:
    provider: "openai-api"
    model: "gpt-oss-120b"
  web_extract:
    provider: "openai-api"
    model: "gpt-oss-120b"
terminal:
  backend: "local"
  timeout: 180
compression:
  enabled: true
  threshold: 0.10
  protect_last_n: 4
  protect_first_n: 1
memory:
  enabled: true
YAMLEOF

# --- Persona / behavior instructions (Hebrew-first personal assistant) ---
cat > "$HERMES_HOME/SOUL.md" << 'SOULEOF'
אתה עוזר אישי חכם של מרדכי. אתה פועל בטלגרם 24/7.

## שפה - חוק עליון
ענה **תמיד ורק בעברית**, בכל מצב וללא יוצא מן הכלל - כולל:
- כל התשובות וההודעות.
- שאלות הבהרה.
- טקסט של כפתורים ואפשרויות בחירה (labels).
- הודעות סטטוס, אישורים, ושגיאות.
לעולם אל תכתוב באנגלית, גם לא חלקית. אם כלי מחזיר אנגלית - תרגם לעברית לפני שאתה מציג למשתמש.

## סגנון
- קצר, ישיר, ענייני. בלי הקדמות מיותרות.
- כשמרדכי מבקש משימה - בצע אותה עד הסוף בעצמך, אל תשאל אישור על כל צעד.
- כשמגיעה הודעה קולית - פעל ישר על מה שנאמר, בלי לשאול אישור על התמלול.
- אל תשאל שאלות אישור מיותרות. אם הבקשה ברורה - בצע מיד. למשל "תזכיר לי עוד 3 דקות" = קבע תזכורת מיד ואשר בקצרה, בלי לשאול "האם אתה רוצה טיימר?".
- שאל רק כשבאמת חסר מידע קריטי שאי אפשר לנחש (למשל זמן לא ברור לגמרי).

## תזכורות - דיוק קריטי
- כשמבקשים תזכורת, חשב את הזמן המדויק לפי השעה הנוכחית (אזור זמן ישראל) וקבע אותה מיד דרך כלי ה-cron.
- אשר בקצרה עם **השעה המדויקת** שבה התזכורת תישלח. לדוגמה: "קבעתי. אזכיר לך ב-14:35".
- ודא שהחישוב נכון: "עוד 5 דקות" = השעה הנוכחית + 5 דקות בדיוק. אל תטעה בחישוב.
- כשהתזכורת מגיעה, שלח הודעה ברורה וקצרה עם תוכן התזכורת.

## זיכרון
זכור פרטים שמרדכי מספר על עצמו, העדפותיו, ומשימות חוזרות. השתמש בזיכרון כדי להכיר אותו לאורך זמן.
SOULEOF

python3 << PYEOF &
import http.server, os
PORT = int(os.environ.get("PORT", 10000))
LOGFILE = "/tmp/hermes.log"
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/logs":
            try:
                with open(LOGFILE, "rb") as f: data = f.read()[-8000:]
            except: data = b"no logs yet"
            self.send_response(200); self.end_headers(); self.wfile.write(data)
        else:
            self.send_response(200); self.end_headers(); self.wfile.write(b"Hermes Agent OK")
    def log_message(self, *a): pass
print(f"Health server on :{PORT}")
http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
PYEOF

# --- Persistent memory: restore Hermes state from Supabase on boot ---
# Render free tier has no persistent disk, so ~/.hermes/state.db is wiped on
# every restart/deploy. We back it up to Supabase Storage and restore here so
# the assistant's long-term memory and conversation history survive restarts.
SUPA_OBJ="${SUPABASE_URL}/storage/v1/object/hermes-state/state.db"
echo "Restoring memory from Supabase..." | tee -a "$LOGFILE"
HTTP=$(curl -s -w "%{http_code}" -o "$HERMES_HOME/state.db" \
  -H "Authorization: Bearer ${SUPABASE_KEY}" -H "apikey: ${SUPABASE_KEY}" \
  "$SUPA_OBJ")
if [ "$HTTP" = "200" ]; then
  echo "Memory restored from Supabase ($(stat -c%s "$HERMES_HOME/state.db" 2>/dev/null) bytes)" | tee -a "$LOGFILE"
else
  echo "No prior memory found (HTTP $HTTP) - starting fresh" | tee -a "$LOGFILE"
  rm -f "$HERMES_HOME/state.db"
fi

# Restore scheduled reminders (cron jobs.json) - separate file from state.db,
# otherwise every restart wipes all the user's reminders.
SUPA_CRON="${SUPABASE_URL}/storage/v1/object/hermes-state/jobs.json"
mkdir -p "$HERMES_HOME/cron"
HTTPC=$(curl -s -w "%{http_code}" -o "$HERMES_HOME/cron/jobs.json" \
  -H "Authorization: Bearer ${SUPABASE_KEY}" -H "apikey: ${SUPABASE_KEY}" \
  "$SUPA_CRON")
if [ "$HTTPC" = "200" ]; then
  echo "Reminders restored from Supabase" | tee -a "$LOGFILE"
else
  echo "No prior reminders (HTTP $HTTPC)" | tee -a "$LOGFILE"
  rm -f "$HERMES_HOME/cron/jobs.json"
fi

# --- Background loop: back up state.db to Supabase every 2 minutes ---
(
  while true; do
    sleep 120
    if [ -f "$HERMES_HOME/state.db" ]; then
      # Consistent snapshot via SQLite backup API (safe during live writes)
      python3 -c "import sqlite3; s=sqlite3.connect('$HERMES_HOME/state.db'); d=sqlite3.connect('/tmp/state_backup.db'); s.backup(d); d.close(); s.close()" 2>>"$LOGFILE" && \
      curl -s -X POST \
        -H "Authorization: Bearer ${SUPABASE_KEY}" -H "apikey: ${SUPABASE_KEY}" \
        -H "x-upsert: true" -H "Content-Type: application/octet-stream" \
        --data-binary "@/tmp/state_backup.db" "$SUPA_OBJ" >/dev/null 2>&1 && \
      echo "Memory backed up to Supabase ($(date '+%H:%M:%S'))" >> "$LOGFILE"
    fi
    # Back up scheduled reminders (jobs.json) so they survive restarts
    if [ -f "$HERMES_HOME/cron/jobs.json" ]; then
      curl -s -X POST \
        -H "Authorization: Bearer ${SUPABASE_KEY}" -H "apikey: ${SUPABASE_KEY}" \
        -H "x-upsert: true" -H "Content-Type: application/json" \
        --data-binary "@$HERMES_HOME/cron/jobs.json" "$SUPA_CRON" >/dev/null 2>&1
    fi
  done
) &

sleep 2
echo "Starting Hermes..." | tee -a "$LOGFILE"
hermes gateway >> "$LOGFILE" 2>&1 &
echo "Hermes PID: $!" | tee -a "$LOGFILE"
tail -f "$LOGFILE"
