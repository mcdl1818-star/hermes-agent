"""Build-time patch: force Hebrew (or $STT_LANGUAGE) on Groq Whisper.

Hermes' Groq STT call omits the `language` parameter, so Whisper auto-detects
and mistranscribes short Hebrew clips (often as Russian). Groq's API is
OpenAI-compatible and accepts `language`, so we inject it here. Build fails
loudly if the upstream code shape changes (anchor not found).
"""
import sys

PATH = "/hermes-agent/tools/transcription_tools.py"

OLD = (
    "                transcription = client.audio.transcriptions.create(\n"
    "                    model=model_name,\n"
    "                    file=audio_file,\n"
    '                    response_format="text",\n'
    "                )"
)

NEW = (
    "                transcription = client.audio.transcriptions.create(\n"
    "                    model=model_name,\n"
    "                    file=audio_file,\n"
    '                    response_format="text",\n'
    '                    language=(__import__("os").environ.get("STT_LANGUAGE") or "he"),\n'
    "                )"
)

src = open(PATH, encoding="utf-8").read()
if OLD not in src:
    sys.exit("PATCH FAILED: Groq STT anchor not found - upstream code changed")
open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
print("OK: forced Groq STT language (default he)")

# --- Patch 2: widen one-shot reminder grace window ---
# On free hosting the container can briefly sleep; the stock 120s grace means a
# reminder whose time passed during a nap is dropped. Widen to 30 min so an
# overdue reminder still fires the moment the container wakes (slightly late,
# never lost). The 60s cron ticker catches it on the first tick after wake.
JOBS_PATH = "/hermes-agent/cron/jobs.py"
G_OLD = "ONESHOT_GRACE_SECONDS = 120"
G_NEW = "ONESHOT_GRACE_SECONDS = 1800"
jsrc = open(JOBS_PATH, encoding="utf-8").read()
if G_OLD not in jsrc:
    sys.exit("PATCH FAILED: ONESHOT_GRACE_SECONDS anchor not found - upstream changed")
open(JOBS_PATH, "w", encoding="utf-8").write(jsrc.replace(G_OLD, G_NEW, 1))
print("OK: widened one-shot reminder grace to 1800s")
