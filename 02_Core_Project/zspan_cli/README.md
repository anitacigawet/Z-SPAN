# zspan-cli

Process your city's public meetings on your own computer, with your own AI
key. The same pipeline zspan.org runs for Arizona resolves the video,
transcribes it, checks the generated material, and keeps a local workspace
you can browse. When processing finishes, the transcript, final outputs, and
audit record are also sent to Z-SPAN's private intake for review. They are not
published automatically.

*(Package copy is provisional — the launch-facing wording gets its own
review pass before the public tag.)*

## Install

**From a clone (no build step):**

```bash
cd 02_Core_Project/zspan_cli
pip install -r requirements.txt
python -m zspan_cli init
```

**Or as a package (from a tagged release):**

```bash
pip install ./02_Core_Project/zspan_cli
zspan init
```

Python 3.11+ recommended (3.10 works).

## Commands

| Command | What it does | Status |
|---|---|---|
| `zspan init` | Paste your API key, validate it, pick a provider; writes `~/.zspan/config.json` | ✅ this build |
| `zspan providers` | Show the provider matrix (what each key unlocks) | ✅ this build |
| `zspan pick` | Choose your city from the live coverage list (`--list` prints the whole tree) | ✅ this build |
| `zspan home` | Show your home city; `--change` re-picks it, `--city <name>` sets it directly | ✅ this build |
| `zspan pull [city]` | Fetch the city's meeting catalog into your local workspace (`~/.zspan/workspace.db`) | ✅ this build |
| `zspan process [meeting]` | Resolve video → transcribe (locally by default) → index → synthesize with your key → deterministic grounding gate → private contribution intake | ✅ this build |
| `zspan open [meeting]` | View the local copy of your workspace in the browser, presented the way zspan.org presents broadcasts | ✅ this build |
| `zspan register-protocol` | Opt in to opening `zspan://` links with this CLI (`--remove` uninstalls) | ✅ this build |

`[meeting]` for `open` and `process` takes three forms: a numeric local id
from `zspan pull`, a public id like `m_QKQR6sGF6WP5koWphY4zBs` copied from
a meeting card on zspan.org, or that card's `zspan://meeting/…` link. A
public id imports the meeting's factual record into your workspace first —
processing stays your explicit choice, and a meeting from outside your
home city never changes your home or enters its channel tree.

`zspan process` with no argument takes the newest unprocessed meeting
that has a video source. Useful flags: `--whisper-model tiny.en` (faster,
rougher local transcription), `--cloud-transcribe` (OpenAI whisper-1 on
your key — speed opt-in, needs ffmpeg), `--force` (re-synthesize cached
outputs), `--keep-media` (keep the downloaded audio).

Every synthesis passes a **deterministic grounding gate** before it's
cached: resolution/ordinance references must appear in the transcript,
quoted text must be verbatim, and claimed votes must have a vote moment
in the record. Refuted material triggers one targeted retry, then gets
stripped — the record, not the model, is the authority. Paraphrase and
spoken-number ambiguity are left alone (uncheckable is not failure).

Processing is complete only after Z-SPAN's private intake has received the
transcript, the four final outputs, and the grounding-gate record. If the
endpoint is temporarily unavailable, the exact contribution stays queued in
your workspace and the next run retries it. Z-SPAN does not publish a client
contribution automatically; it enters a private review path.

## zspan:// links (optional)

A pip console script has no URL-handler registration of its own, so
`zspan://` links are strictly opt-in: `zspan register-protocol` writes one
per-user artifact and nothing else —

- **macOS** — a minimal handler app at `~/.zspan/Z-SPAN Handler.app`
  (an AppleScript applet that forwards the link to `zspan open`).
- **Windows** — the per-user registry key `HKCU\Software\Classes\zspan`
  (no admin rights involved).
- **Linux** — `~/.local/share/applications/zspan-handler.desktop`
  (routed via `xdg-mime` when xdg-utils is present).

Clicking `zspan://meeting/<public_id>` then does exactly what the copied
`zspan open <public_id>` command does — same resolver, same import, same
choice-to-process. Browsers show their own "open this link in…?" prompt
first; that confirmation is honest friction, not a bug.

`zspan register-protocol --remove` deletes the artifact for your OS. If
the CLI itself is already gone, deleting the artifact by hand (the app,
the registry key, or the desktop file named above) is the whole cleanup.

## Keys and custody

Your key lives in `~/.zspan/config.json` on your machine (file permissions
restricted on Mac/Linux) and is sent **only** to the provider you chose —
Google, OpenAI, or Anthropic — directly. It never touches Z-SPAN's servers.
The one exception to "never sent anywhere" is the validation ping at
`init`, which also goes straight to your provider.

Transcription runs **locally on your machine**, free — a built-in Whisper
model does the listening, no key involved, just patience (roughly
real-time on an ordinary laptop). That means any single key runs the
whole pipeline end-to-end — **a free Gemini key runs it at $0**. An
OpenAI key can optionally speed transcription up through their `whisper-1`
cloud service (~$0.36 per hour of meeting audio); it's a speed upgrade,
never a requirement.

Raw downloaded media also stays on your computer unless your chosen provider
receives it for optional cloud transcription. Z-SPAN's private intake receives
the resulting transcript, final generated outputs, and audit metadata—not your
provider key or the raw media file.

## Environment overrides

- `ZSPAN_HOME` — config/workspace directory (default `~/.zspan`)
- `ZSPAN_FLAGSHIP_URL` — the Z-SPAN endpoint server (default `https://zspan.org`)
