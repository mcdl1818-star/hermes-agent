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
TELEGRAM_WEBHOOK_URL=${TELEGRAM_WEBHOOK_URL}
TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET}
TELEGRAM_WEBHOOK_PORT=7860
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

echo "Starting Hermes Agent..."
exec hermes gateway
