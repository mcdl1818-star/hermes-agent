#!/bin/bash

HERMES_HOME="/root/.hermes"
mkdir -p "$HERMES_HOME"
LOGFILE="/tmp/hermes.log"

cat > "$HERMES_HOME/.env" << ENVEOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
GROQ_API_KEY=${GROQ_API_KEY}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS}
TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL}
TELEGRAM_WEBHOOK_URL=${TELEGRAM_WEBHOOK_URL}
TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET}
WEBHOOK_PORT=8644
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

# Health + log server on port 7860
python3 << 'PYEOF' &
import http.server, urllib.request

LOGFILE = "/tmp/hermes.log"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/logs":
            try:
                with open(LOGFILE, "rb") as f:
                    data = f.read()[-8000:]
            except:
                data = b"no logs yet"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Hermes Agent OK")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = urllib.request.Request(
                f"http://localhost:8644{self.path}",
                data=body, headers=dict(self.headers), method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=30)
            self.send_response(resp.status)
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *a): pass

print("Server on :7860")
http.server.HTTPServer(("0.0.0.0", 7860), Handler).serve_forever()
PYEOF

sleep 2
echo "Starting Hermes..." | tee "$LOGFILE"
hermes gateway >> "$LOGFILE" 2>&1 &
echo "Hermes PID: $!" | tee -a "$LOGFILE"

# Keep alive
tail -f "$LOGFILE"
