#!/usr/bin/env python3
import json
import os
import platform
import random
import re
import shutil
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ============================================================
# Config (via env vars)
# ============================================================
API_KEY = os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or os.getenv("ELEVEN_VOICE_ID")
MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3")
OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")

# How much we will actually speak (after sanitizing / summarizing)
MAX_SPOKEN_CHARS = int(os.getenv("ELEVENLABS_MAX_CHARS", "900"))

# Summarization: 0 = off, N>=1 = on + N sentences
try:
    SUMMARY_SENTENCES = int(os.getenv("CLAUDE_TTS_SUMMARIZE", "0"))
except ValueError:
    SUMMARY_SENTENCES = 0
SUMMARY_SENTENCES = max(0, min(SUMMARY_SENTENCES, 8))
SUMMARIZE = SUMMARY_SENTENCES > 0

# Which Claude Code model to use for summarization.
SUMMARY_MODEL = os.getenv("CLAUDE_TTS_SUMMARY_MODEL", "haiku")

# Limit how much text we feed into the summarizer (after removing code fences)
SUMMARY_MAX_INPUT_CHARS = int(os.getenv("CLAUDE_TTS_SUMMARY_MAX_INPUT_CHARS", "4000"))

# Master enable switch
ENABLED = os.getenv("CLAUDE_TTS", "1") not in ("0", "false", "False", "no", "NO")

# Internal recursion guard (set when we call `claude -p` from inside this hook)
INTERNAL = os.getenv("CLAUDE_TTS_INTERNAL", "") == "1"

# Optional: disable playback (still generates audio)
PLAY_ENABLED = os.getenv("CLAUDE_TTS_PLAY", "1") not in ("0", "false", "False", "no", "NO")

# Playback speed (local player). 1.0 = normal, 2.0 = 2x, etc.
try:
    PLAYBACK_SPEED = float(os.getenv("CLAUDE_TTS_SPEED", "1.0"))
except ValueError:
    PLAYBACK_SPEED = 2.0
PLAYBACK_SPEED = max(0.5, min(PLAYBACK_SPEED, 4.0))

SPOOL_DIR = Path.home() / ".claude" / "tts_queue"
JOBS_DIR = SPOOL_DIR / "jobs"
AUDIO_DIR = SPOOL_DIR / "audio"
FAILED_DIR = SPOOL_DIR / "failed"
LOCK_PATH = SPOOL_DIR / "queue.lock"

CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# ============================================================
# Cross-platform file lock (serializes playback globally)
# ============================================================
class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt
            self.fp.seek(0)
            msvcrt.locking(self.fp.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.fp:
                if os.name == "nt":
                    import msvcrt
                    self.fp.seek(0)
                    msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        finally:
            if self.fp:
                self.fp.close()

# ============================================================
# Helpers
# ============================================================
def load_hook_input():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}

def make_job_id():
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}_{os.getpid()}_{rand}"

def sanitize_for_summary_input(text: str) -> str:
    """
    Prepare text for summarization:
    - Remove fenced code blocks
    - Lightly de-markdown
    - Keep more text than speech (up to SUMMARY_MAX_INPUT_CHARS)
    """
    text = CODE_FENCE_RE.sub("", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > SUMMARY_MAX_INPUT_CHARS:
        text = text[:SUMMARY_MAX_INPUT_CHARS].rsplit(" ", 1)[0] + "…"
    return text

def sanitize_for_speech(text: str) -> str:
    """
    Prepare text for TTS:
    - Remove fenced code blocks
    - Lightly de-markdown
    - Truncate to MAX_SPOKEN_CHARS
    """
    text = CODE_FENCE_RE.sub("", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > MAX_SPOKEN_CHARS:
        text = text[:MAX_SPOKEN_CHARS].rsplit(" ", 1)[0] + "…"
    return text

def summarize_with_headless_haiku(text: str) -> str:
    """
    Calls `claude -p` to summarize into N sentences of prose.
    Returns "" on failure.
    """
    if not text.strip():
        return ""

    if shutil.which("claude") is None:
        return ""

    prompt = (
        "Summarize the following assistant response for text-to-speech.\n"
        f"- Write exactly {SUMMARY_SENTENCES} sentence(s) as plain prose.\n"
        "- No bullet points, no headings, no code, no URLs.\n"
        "- One paragraph, easy to listen to.\n\n"
        "ASSISTANT RESPONSE:\n"
        f"{text.strip()}"
    )

    cmd = [
        "claude",
        "-p", prompt,
        "--model", SUMMARY_MODEL,
        "--output-format", "json",
        "--no-session-persistence",
        "--max-turns", "1",
        "--tools", "",
    ]

    env = os.environ.copy()
    env["CLAUDE_TTS_INTERNAL"] = "1"

    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, env=env)
        data = json.loads(out)

        candidate = (
            (data.get("result") if isinstance(data, dict) else None)
            or (data.get("message") if isinstance(data, dict) else None)
            or (data.get("output") if isinstance(data, dict) else None)
        )
        if isinstance(candidate, str):
            return candidate.strip()

        if isinstance(candidate, dict):
            for k in ("result", "text", "content"):
                v = candidate.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()

        return ""
    except Exception:
        return ""

def elevenlabs_tts(text: str, voice_id: str, out_path: Path):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={OUTPUT_FORMAT}"
    payload = {
        "text": text,
        "model_id": MODEL_ID,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "xi-api-key": API_KEY,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        audio = resp.read()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)

def pick_player_cmd(audio_path: Path):
    sysname = platform.system()
    s = PLAYBACK_SPEED

    if sysname == "Darwin" and shutil.which("afplay"):
        return ["afplay", "--rate", str(s), str(audio_path)]

    if sysname == "Linux":
        if shutil.which("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", f"atempo={s}", str(audio_path)]
        if shutil.which("mpv"):
            return ["mpv", "--no-video", "--really-quiet", f"--speed={s}", str(audio_path)]
        for cmd in ("mpg123", "vlc", "play"):
            if shutil.which(cmd):
                if cmd == "vlc":
                    return ["vlc", "--intf", "dummy", "--play-and-exit", str(audio_path)]
                return [cmd, str(audio_path)]

    if sysname == "Windows":
        if shutil.which("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", f"atempo={s}", str(audio_path)]
        if shutil.which("mpv"):
            return ["mpv", "--no-video", "--really-quiet", f"--speed={s}", str(audio_path)]

    return None

def play_audio(audio_path: Path):
    if not PLAY_ENABLED:
        return
    cmd = pick_player_cmd(audio_path)
    if not cmd:
        return
    subprocess.run(cmd, check=False)

def move_to_failed(job_path: Path, reason: str):
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    dest = FAILED_DIR / f"{job_path.stem}_{ts}.json"
    try:
        job = {}
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        job["failed_reason"] = reason
        job["failed_ms"] = int(time.time() * 1000)
        dest.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    except Exception:
        try:
            shutil.copy2(job_path, dest)
        except Exception:
            pass
    finally:
        try:
            job_path.unlink(missing_ok=True)
        except Exception:
            pass

# ============================================================
# Main
# ============================================================
def main():
    if not ENABLED or INTERNAL:
        return 0

    hook = load_hook_input()
    if hook.get("hook_event_name") != "Stop":
        return 0

    if hook.get("stop_hook_active") is True:
        return 0

    raw_text = (hook.get("last_assistant_message") or "").strip()
    if not raw_text:
        return 0

    if not API_KEY or not VOICE_ID:
        return 0

    # 1) Prepare summary input (removes code blocks, keeps more text)
    summary_input = sanitize_for_summary_input(raw_text)
    if not summary_input:
        return 0

    # 2) Optional headless summarization (outside the queue lock)
    if SUMMARIZE:
        summary = summarize_with_headless_haiku(summary_input)
        if summary:
            spoken = sanitize_for_speech(summary)
        else:
            spoken = sanitize_for_speech(summary_input)
    else:
        spoken = sanitize_for_speech(summary_input)

    if not spoken:
        return 0

    # 3) Enqueue job
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    job_id = make_job_id()
    job_path = JOBS_DIR / f"{job_id}.json"
    job_path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "session_id": hook.get("session_id"),
                "voice_id": VOICE_ID,
                "text": spoken,
                "model_id": MODEL_ID,
                "output_format": OUTPUT_FORMAT,
                "created_ms": int(time.time() * 1000),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 4) Drain queue (only one process plays at a time)
    with FileLock(LOCK_PATH):
        while True:
            jobs = sorted(JOBS_DIR.glob("*.json"))
            if not jobs:
                break

            for jp in jobs:
                audio_path = None
                try:
                    job = json.loads(jp.read_text(encoding="utf-8"))
                    t = (job.get("text") or "").strip()
                    vid = (job.get("voice_id") or VOICE_ID).strip()
                    if not t:
                        jp.unlink(missing_ok=True)
                        continue

                    audio_path = AUDIO_DIR / f"{job.get('job_id','audio')}.mp3"
                    elevenlabs_tts(t, vid, audio_path)
                    play_audio(audio_path)

                    jp.unlink(missing_ok=True)
                    if audio_path:
                        audio_path.unlink(missing_ok=True)

                except Exception as e:
                    move_to_failed(jp, reason=str(e))
                    if audio_path:
                        try:
                            audio_path.unlink(missing_ok=True)
                        except Exception:
                            pass

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
