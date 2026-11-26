"""Generate one MP3 announcement per line of a TSV file, and check each one by ear (via Whisper).

    python3 -u gen_clips.py changes.tsv

Each line is `HH:MM<TAB>spoken text`; the clip is written to $OUT_DIR/<HHMM>.mp3.

Steps per line:
1. Gemini TTS speaks "<style tag> <text>" in the chosen voice.
2. ffmpeg trims silence, pads the end, and loudness-normalises for a noisy kitchen.
3. Groq Whisper transcribes the clip. Gemini sometimes reads the style tag out loud
   ("slow, smooth, laid-back ..."). If the transcript contains any of LEAK_WORDS, the
   clip is cut to start just before the first real word of the text.
4. 300 ms of lead-in silence is added (some speakers clip the first syllable).
5. The clip is transcribed again; if the style tag is still audible, the line is
   regenerated (up to 4 tries). A line that prints BAD four times needs rephrasing.

Configuration comes from environment variables; see ../.env.example.
Requires ffmpeg and curl on PATH.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = os.environ.get("TTS_MODEL", "gemini-3.8-flash-tts")
STYLE = os.environ.get(
    "TTS_STYLE", "[slow, smooth, laid-back West Coast hip-hop drawl, relaxed and cool, grinning]"
)
VOICE = os.environ.get("TTS_VOICE", "Algenib")
OUT_DIR = os.environ.get("OUT_DIR", "clips")
# Words from the style tag that should never be heard in a finished clip.
LEAK_WORDS = os.environ.get(
    "LEAK_WORDS", "drawl,laid-back,laid back,hip-hop,hip hop,grinning,west coast hip"
).lower().split(",")

TRIES = 4


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def transcribe(path):
    out = subprocess.run(
        ["curl", "-s", "https://api.groq.com/openai/v1/audio/transcriptions",
         "-H", f"Authorization: Bearer {GROQ_API_KEY}",
         "-F", f"file=@{path}",
         "-F", "model=whisper-large-v3",
         "-F", "response_format=verbose_json",
         "-F", "timestamp_granularities[]=word"],
        capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def leaked(transcript_text):
    t = transcript_text.lower()
    return any(w in t for w in LEAK_WORDS)


def tts(text):
    body = {
        "contents": [{"role": "user", "parts": [{"text": f"{STYLE} {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}},
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    last = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
            )
            d = json.load(urllib.request.urlopen(req, timeout=90))
            return base64.b64decode(d["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
        except Exception as e:  # rate limits and quota errors: back off and retry
            last = e
            print("  retry", e, flush=True)
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"TTS failed after 6 attempts: {last}")


def generate(name, text):
    """Make <name>.mp3. Returns (ok, final transcript text)."""
    raw = tts(text)
    src = f"{OUT_DIR}/{name}.src"
    mp3 = f"{OUT_DIR}/{name}.mp3"
    tmp = mp3 + ".t.mp3"
    with open(src, "wb") as f:
        f.write(raw)

    # Gemini returns raw 24 kHz 16-bit mono PCM unless it sends a WAV header.
    fmt = [] if raw[:4] == b"RIFF" else ["-f", "s16le", "-ar", "24000", "-ac", "1"]
    ffmpeg(*fmt, "-i", src,
           "-af", "silenceremove=stop_periods=-1:stop_threshold=-45dB:stop_duration=0.3,"
                  "apad=pad_dur=0.6,loudnorm=I=-11:TP=-1.0",
           "-ar", "44100", "-ac", "2", "-b:a", "192k", mp3)

    t = transcribe(mp3)
    if leaked(t.get("text", "")):
        first = text.split()[0].lower().strip(",!.")
        start = next((w["start"] for w in t.get("words", []) if first in w["word"].lower()), None)
        if start is None:
            return False, t.get("text", "")
        ffmpeg("-ss", str(max(0, start - 0.25)), "-i", mp3, "-af", "afade=t=in:d=0.08", "-b:a", "192k", tmp)
        os.replace(tmp, mp3)

    ffmpeg("-i", mp3, "-af", "adelay=300|300", "-b:a", "192k", tmp)
    os.replace(tmp, mp3)
    return True, transcribe(mp3).get("text", "")


def main(tsv):
    os.makedirs(OUT_DIR, exist_ok=True)
    for line in open(tsv):
        if not line.strip():
            continue
        t, text = line.rstrip("\n").split("\t", 1)
        name = t.replace(":", "")
        time.sleep(6)  # stay under the TTS per-minute rate limit
        for _ in range(TRIES):
            ok, heard = generate(name, text)
            if ok and not leaked(heard):
                print(name, "clean", heard[:90], flush=True)
                break
            print(name, "BAD", heard[:90], flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
