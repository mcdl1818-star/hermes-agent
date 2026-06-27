#!/bin/bash

# Memory tuning for 512MB container: limit glibc malloc arenas (threaded
# Python otherwise spawns many per-thread arenas that inflate RSS) and trim
# freed memory back to the OS aggressively. Classic fix for Python OOM in
# small containers - typically saves 100-200MB RSS.
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=100000
export PYTHONMALLOC=malloc
export PYTHONUNBUFFERED=1

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
agent:
  reasoning_effort: "low"
gateway:
  message_timestamps:
    enabled: true
model:
  default: "gpt-oss-120b"
  provider: "openai-api"
fallback_providers:
  - provider: "openai-api"
    model: "zai-glm-4.7"
auxiliary:
  title_generation:
    provider: "openai-api"
    model: "gpt-oss-120b"
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
  threshold: 0.20
  protect_last_n: 10
  protect_first_n: 2
memory:
  enabled: true
YAMLEOF

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
  done
) &

sleep 2
echo "Starting Hermes..." | tee -a "$LOGFILE"
hermes gateway >> "$LOGFILE" 2>&1 &
echo "Hermes PID: $!" | tee -a "$LOGFILE"
tail -f "$LOGFILE"
