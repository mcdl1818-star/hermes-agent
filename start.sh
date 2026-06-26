#!/bin/bash
set -e

HERMES_HOME="/root/.hermes"
mkdir -p "$HERMES_HOME"

# Write secrets to .env (no webhook URL = polling mode)
cat > "$HERMES_HOME/.env" << ENVEOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
GROQ_API_KEY=${GROQ_API_KEY}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS}
TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL}
SUPABASE_URL=${SUPABASE_URL}
SUPABASE_KEY=${SUPABASE_KEY}
ENVEOF

# Write config.yaml
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

echo "Starting health check server on port 7860..."
# Simple health check server for HuggingFace Spaces
python3 -c "
import http.server, threading, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Hermes Agent running')
    def log_message(self, *a): pass
httpd = http.server.HTTPServer(('0.0.0.0', 7860), H)
t = threading.Thread(target=httpd.serve_forever, daemon=True)
t.start()
print('Health server up on port 7860')
" &

echo "Starting Hermes Agent gateway in polling mode..."
exec hermes gateway run
