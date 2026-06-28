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
