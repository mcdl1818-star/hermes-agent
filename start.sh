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

# Health check + Telegram proxy on port 7860
python3 << 'PYEOF' &
import http.server, urllib.request, json

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hermes Agent OK")

    def do_POST(self):
        # Forward Telegram webhooks to Hermes on port 8644
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = urllib.request.Request(
                f"http://localhost:8644{self.path}",
                data=body,
                headers=dict(self.headers),
                method="POST"
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

print("Proxy/health server on port 7860")
http.server.HTTPServer(("0.0.0.0", 7860), ProxyHandler).serve_forever()
PYEOF

PROXY_PID=$!
sleep 2

# Start Hermes in webhook mode pointing to itself on 8644
cat >> "$HERMES_HOME/.env" << ENVEOF2
TELEGRAM_WEBHOOK_URL=https://mcdl1818-hermes-agent.hf.space
TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET}
WEBHOOK_PORT=8644
ENVEOF2

echo "Starting Hermes Agent..."
hermes gateway &
HERMES_PID=$!

echo "Hermes PID: $HERMES_PID | Proxy PID: $PROXY_PID"

# Keep container alive while both processes run
while kill -0 $PROXY_PID 2>/dev/null && kill -0 $HERMES_PID 2>/dev/null; do
    sleep 10
done
echo "A process exited - container stopping"
