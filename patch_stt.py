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

# --- Patch 3: disable per-turn background self-improvement review ---
# It fires an extra LLM call (or two) after every message. On free tiers with a
# 5-requests/minute cap (Cerebras), this starves the next user message and the
# cron reminder firing, causing 429->Groq->413 cascades. Foreground memory tool
# still works, so the agent can still save facts during a turn.
TF_PATH = "/hermes-agent/agent/turn_finalizer.py"
T_OLD = "    if final_response and not interrupted and (_should_review_memory or _should_review_skills):"
T_NEW = "    if False and final_response and not interrupted and (_should_review_memory or _should_review_skills):  # disabled to conserve free-tier RPM"
tsrc = open(TF_PATH, encoding="utf-8").read()
if T_OLD not in tsrc:
    sys.exit("PATCH FAILED: background review anchor not found - upstream changed")
open(TF_PATH, "w", encoding="utf-8").write(tsrc.replace(T_OLD, T_NEW, 1))
print("OK: disabled per-turn background review")
