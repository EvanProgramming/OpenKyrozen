from __future__ import annotations

import os
import subprocess

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

async def api_transcribe(self, request: Request):
    service = self
    """Transcribe audio to text using system tools (macOS say, Linux espeak)."""
    # This is a placeholder — real STT would use whisper or an API
    body = await service._json_object(request) if request.headers.get("content-type", "").lower().startswith("application/json") else {}
    if "text" in body and not isinstance(body["text"], str):
        raise HTTPException(400, "text must be a string")
    text = body.get("text", "")
    return {"text": text, "source": "passthrough", "note": "Use whisper or cloud STT for real transcription"}


async def api_speak(self, text: str = ""):
    service = self
    """Text-to-speech using system TTS tools."""
    if not text:
        return {"error": "No text provided"}
    import subprocess, platform
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["say", text])
        elif system == "Linux":
            subprocess.Popen(["espeak", text])
        elif system == "Windows":
            # Keep user text in the environment rather than interpolating it
            # into PowerShell source code.
            env = os.environ.copy()
            env["KYROZEN_TTS_TEXT"] = text
            subprocess.Popen([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Speak($env:KYROZEN_TTS_TEXT)",
            ], env=env)
        return {"status": "speaking", "text": text[:100]}
    except Exception as e:
        return {"error": str(e)}
