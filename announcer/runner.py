"""Play each announcement in events.tsv at its time, then post its text to Telegram.

Primary speaker: a LinkPlay / Up2Stream streamer, driven through its HTTP API
(`playPromptUrl`), with a temporary volume boost that is restored after the clip.
Fallback: a Sonos speaker via sonos_fallback.py.

events.tsv is read once, at startup. On start, any event less than
REPLAY_WINDOW_MIN minutes in the past is played (so a restart right after an
event replays it).

Usage:
    python3 -u runner.py              # run today's schedule
    python3 runner.py --play 1712     # play one clip now, then exit

Configuration comes from environment variables; see ../.env.example.
"""

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

EVENTS = Path(os.environ.get("EVENTS", HERE / "events.tsv"))
# Directories (relative to this folder) searched for <HHMM>.mp3, first match wins.
CLIP_DIRS = os.environ.get("CLIP_DIRS", "clips").split()
# URL where `python3 -m http.server` (run in this folder) serves the clips.
CLIP_BASE_URL = os.environ["CLIP_BASE_URL"].rstrip("/") + "/"

SPEAKER_HOST = os.environ.get("SPEAKER_HOST", "")  # e.g. the speaker's mDNS name
SPEAKER_IP = os.environ.get("SPEAKER_IP", "")      # used if SPEAKER_HOST doesn't resolve
BOOST = int(os.environ.get("BOOST", "15"))         # volume points added while a clip plays

SONOS_IP = os.environ.get("SONOS_IP", "")
SONOS_VOLUME = os.environ.get("SONOS_VOLUME", "50")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_PREFIX = os.environ.get("TELEGRAM_PREFIX", "\U0001F983 ")  # turkey emoji

MUTE = set(os.environ.get("MUTE", "").split())  # HHMM names to post as text only
REPLAY_WINDOW = dt.timedelta(minutes=float(os.environ.get("REPLAY_WINDOW_MIN", "2")))


def log(*args):
    print(dt.datetime.now().strftime("%H:%M:%S"), *args, flush=True)


def speaker_ip():
    """Resolve the speaker by name each time (DHCP leases move); fall back to the fixed IP."""
    if SPEAKER_HOST:
        try:
            return socket.gethostbyname(SPEAKER_HOST)
        except OSError:
            pass
    return SPEAKER_IP


def find_clip(name):
    for d in CLIP_DIRS:
        if (HERE / d / f"{name}.mp3").exists():
            return d
    return CLIP_DIRS[-1]


def clip_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout
    return float(out or 10)


def play(name):
    d = find_clip(name)
    url = f"{CLIP_BASE_URL}{d}/{name}.mp3"

    for _ in range(2):
        ip = speaker_ip()

        def api(cmd):
            return urllib.request.urlopen(f"http://{ip}/httpapi.asp?command={cmd}", timeout=5).read()

        try:
            vol = int(json.loads(api("getPlayerStatus"))["vol"])
            boosted = min(100, vol + BOOST)
            dur = clip_duration(HERE / d / f"{name}.mp3")
            t0 = time.time()
            r = api(f"playPromptUrl:{url}")
            # The prompt starts at the current volume; raise it once playback has begun.
            time.sleep(0.7)
            api(f"setPlayerCmd:vol:{boosted}")
            log("played speaker", d, ip, name, r, f"vol {vol}->{boosted} for {dur:.0f}s")
            # Restore just before the clip ends so the music underneath doesn't blast.
            time.sleep(max(0, t0 + dur - 0.9 - time.time()))
            try:
                api(f"setPlayerCmd:vol:{vol}")
            except Exception:
                time.sleep(2)
                try:
                    api(f"setPlayerCmd:vol:{vol}")
                except Exception as e:
                    log("vol restore failed", e)
            return
        except Exception as e:
            log("speaker fail", ip, name, e)
            time.sleep(2)

    if not SONOS_IP:
        log("no fallback speaker configured", name)
        return
    r = subprocess.run(
        ["uv", "run", "-q", str(HERE / "sonos_fallback.py"), SONOS_IP, url, SONOS_VOLUME],
        capture_output=True, text=True, timeout=60,
    )
    log("FALLBACK sonos", name, r.returncode, (r.stdout + r.stderr)[-200:])


def post(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": TELEGRAM_PREFIX + text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data, timeout=10
        ).read()
        log("posted tg")
    except Exception as e:
        log("tg post failed", e)


def load_events(path):
    today = dt.date.today()
    events = []
    for line in open(path):
        if not line.strip():
            continue
        t, text = line.rstrip("\n").split("\t", 1)
        h, m = map(int, t.split(":"))
        events.append((dt.datetime.combine(today, dt.time(h, m)), t.replace(":", ""), text))
    return sorted(events)


def run():
    for when, name, text in load_events(EVENTS):
        if dt.datetime.now() > when + REPLAY_WINDOW:
            log("skip past", name)
            continue
        while dt.datetime.now() < when:
            time.sleep(min(15, max(0.5, (when - dt.datetime.now()).total_seconds())))
        if name in MUTE:
            log("muted (text only)", name)
        else:
            play(name)
        post(text)
    log("done")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--play":
        play(sys.argv[2])
    else:
        run()
