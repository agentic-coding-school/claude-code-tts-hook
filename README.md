# Claude Code TTS Hook (ElevenLabs)

A Claude Code `Stop` hook that speaks Claude's responses aloud using ElevenLabs TTS. Multiple Claude Code sessions serialize through a global file lock so they never talk over each other.

Optional: summarize long responses into N sentences using a headless `claude -p` call before speaking.

---

## Setup Instructions

These are step-by-step instructions for setting this up. If you're an AI agent implementing this, follow them exactly.

### 1. Copy the hook script

```bash
mkdir -p ~/.claude/hooks
cp eleven_speak_queue.py ~/.claude/hooks/eleven_speak_queue.py
chmod +x ~/.claude/hooks/eleven_speak_queue.py
```

### 2. Add env vars and the Stop hook to `~/.claude/settings.json`

Merge these keys into the existing settings file. Do NOT overwrite other existing keys.

**Add to `"env"`:**

```json
{
  "env": {
    "ELEVENLABS_API_KEY": "<YOUR_XI_API_KEY>",
    "ELEVENLABS_VOICE_ID": "<YOUR_VOICE_ID>"
  }
}
```

**Add to `"hooks"."Stop"`:**

If a `Stop` hook array already exists, append this entry to the existing `hooks` array inside it. If no `Stop` key exists, create it.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/eleven_speak_queue.py",
            "async": true,
            "timeout": 1200
          }
        ]
      }
    ]
  }
}
```

**Full example** (with other hooks already present):

```json
{
  "env": {
    "ELEVENLABS_API_KEY": "sk_...",
    "ELEVENLABS_VOICE_ID": "TABZn6CDfjMNGrsnGzzD"
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/eleven_speak_queue.py",
            "async": true,
            "timeout": 1200
          }
        ]
      }
    ]
  }
}
```

### 3. Restart Claude Code

Settings and hooks are read at startup. Restart any running Claude Code sessions.

### 4. Verify

Ask Claude something short. You should hear it spoken aloud after the response finishes.

---

## How It Works

1. Claude Code fires the `Stop` hook when it finishes responding. The hook receives JSON on stdin containing `last_assistant_message`.
2. The script strips code blocks, markdown formatting, and truncates to `MAX_SPOKEN_CHARS`.
3. (Optional) If `CLAUDE_TTS_SUMMARIZE` >= 1, it runs `claude -p` (headless, no tools, no session) to summarize the text into N sentences of prose before speaking.
4. It calls `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` to generate MP3 audio.
5. It plays the audio using `afplay` (macOS), `ffplay`/`mpv` (Linux), or `ffplay`/`mpv` (Windows).
6. A global file lock at `~/.claude/tts_queue/queue.lock` ensures only one session plays audio at a time. Jobs are spooled to `~/.claude/tts_queue/jobs/` so nothing is lost.

---

## Environment Variables

All configured via `"env"` in `~/.claude/settings.json` or your shell profile.

### Required

| Variable | Description |
|---|---|
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key (starts with `sk_`) |
| `ELEVENLABS_VOICE_ID` | The voice ID to use |

### Optional

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_TTS` | `1` | Master switch. Set to `0` to disable all TTS |
| `ELEVENLABS_MODEL_ID` | `eleven_multilingual_v2` | ElevenLabs model |
| `ELEVENLABS_OUTPUT_FORMAT` | `mp3_44100_128` | Audio output format |
| `ELEVENLABS_MAX_CHARS` | `900` | Max characters to speak |
| `CLAUDE_TTS_SUMMARIZE` | `0` | `0` = off. `1`-`8` = summarize into N sentences before speaking |
| `CLAUDE_TTS_SUMMARY_MODEL` | `haiku` | Claude model for summarization |
| `CLAUDE_TTS_SUMMARY_MAX_INPUT_CHARS` | `4000` | Max chars fed to the summarizer |
| `CLAUDE_TTS_PLAY` | `1` | Set to `0` to generate audio files but skip playback |

---

## Audio Player Requirements

| OS | Player | Notes |
|---|---|---|
| macOS | `afplay` | Built-in, no install needed |
| Linux | `ffplay`, `mpv`, `mpg123`, `vlc`, or `play` | Install one: `sudo apt install ffmpeg` or `sudo apt install mpv` |
| Windows | `ffplay` or `mpv` | Install ffmpeg or mpv and add to PATH |

---

## Troubleshooting

**No audio plays:**
- Check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` are set correctly
- On Linux/Windows, ensure an audio player is installed and on PATH
- Check `~/.claude/tts_queue/failed/` for failed job files with error details

**Audio overlaps between sessions:**
- This shouldn't happen. The file lock at `~/.claude/tts_queue/queue.lock` serializes playback. If it does, delete the lock file and restart.

**Summarization not working:**
- Ensure `claude` CLI is on your PATH
- Set `CLAUDE_TTS_SUMMARIZE=3` (or any number 1-8)
- Check that the recursion guard env var `CLAUDE_TTS_INTERNAL` is not set in your shell

**Toggle off quickly:**
- Set `CLAUDE_TTS=0` in your env or settings, or remove the hook entry from settings.json

---

## File Locations

| Path | Purpose |
|---|---|
| `~/.claude/hooks/eleven_speak_queue.py` | The hook script |
| `~/.claude/settings.json` | Claude Code settings (env vars + hook config) |
| `~/.claude/tts_queue/jobs/` | Pending job spool (auto-cleaned) |
| `~/.claude/tts_queue/audio/` | Temporary audio files (auto-cleaned) |
| `~/.claude/tts_queue/failed/` | Failed jobs with error details |
| `~/.claude/tts_queue/queue.lock` | Global playback lock file |
