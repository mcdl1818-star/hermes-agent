#!/bin/bash

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
  threshold: 0.40
  protect_last_n: 8
  protect_first_n: 1
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

sleep 2
echo "Starting Hermes..." | tee "$LOGFILE"
hermes gateway >> "$LOGFILE" 2>&1 &
echo "Hermes PID: $!" | tee -a "$LOGFILE"
tail -f "$LOGFILE"
