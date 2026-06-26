#!/bin/bash
set -e

HERMES_HOME="/root/.hermes"
mkdir -p "$HERMES_HOME"

cat > "$HERMES_HOME/.env" << ENVEOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
GROQ_API_KEY=${GROQ_API_KEY}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS}
TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL}
SUPABASE_URL=${SUPABASE_URL}
SUPABASE_KEY=${SUPABASE_KEY}
ENVEOF

cat > "$HERMES_HOME/config.yaml" << YAMLEOF
model:
  default: "qwen/qwen3.6-plus:free"
  provider: "openrouter"
terminal:
  backend: "local"
  timeout: 180
compression:
  enabled: true
  threshold: 0.50
memory:
  enabled: true
YAMLEOF

echo "Starting Hermes gateway..."
hermes gateway start || hermes gateway run &
sleep 5
echo "Hermes started. Starting health server on port 7860..."

exec python3 -c "
import http.server, subprocess, os, time

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Hermes Agent OK')
    def log_message(self, *a): pass

print('Health server listening on :7860')
httpd = http.server.HTTPServer(('0.0.0.0', 7860), H)
httpd.serve_forever()
"
