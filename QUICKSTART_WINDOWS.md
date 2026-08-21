# zspan on Windows — quick start

Process your city's public meetings on your own computer, into a private
workspace only you can see. Ten minutes, one API key (or none — see step 3).

## 1. Install Python

Install **Python 3.11 or newer** from [python.org/downloads](https://www.python.org/downloads/).
On the first installer screen, check **"Add python.exe to PATH"** — that's
what makes `python` and `pip` work in a terminal.

(Already have Python? `python --version` in a terminal tells you.)

## 2. Install the CLI

Open a terminal (Windows key → type `cmd` → Enter) and run ONE of these:

**From the tagged release (the normal path):**

```
pip install https://github.com/anitacigawet/Z-SPAN/releases/download/zspan-cli-v0/zspan_cli-0.1.0-py3-none-any.whl
```

**From a copied repo folder** (for example, a clone carried over on a
USB drive — run this inside the folder):

```
cd 02_Core_Project\zspan_cli
pip install .
```

Either way, `zspan --version` should now answer.

## 3. Store a key (or skip this)

```
zspan init
```

Paste an API key when asked — a **free Gemini key**
([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) runs the
whole pipeline at $0, since transcription happens locally on your machine.
OpenAI and Anthropic keys work too. The key is stored in a file in your
user folder (`~/.zspan/config.json`) and **never leaves your machine** —
the CLI talks to your AI provider directly, never through Z-SPAN.

**No key at all?** If you have the Codex CLI installed
(`npm install -g @openai/codex`, signed into your ChatGPT account), the
pipeline and the site's Librarian both run keyless through it.

## 4. Pick, pull, process, open

```
zspan pick            :: choose your state, county, city
zspan pull            :: fetch that city's public meeting catalog
zspan process         :: transcribe + synthesize one meeting, locally
zspan open            :: view your workspace as the real Z-SPAN site
```

`zspan process` does the real work: it downloads the meeting recording,
transcribes it on your CPU (free; an OpenAI key can speed this up with
`--cloud-transcribe`), and synthesizes the same output shapes zspan.org
shows — with a deterministic fact-check gate on every output.

`zspan open` serves the site at a local address like `http://127.0.0.1:8741`
— local only, nothing leaves your machine. The first run offers a one-time
~176 MB download of the full site bundle (SHA256-verified); saying no
serves a lean fallback view instead.

## The .bat shortcut

`zspan.bat` (next to this file in the repo) lets you double-click-run or
type `zspan` from the repo folder without touching PATH. It just forwards
to `python -m zspan_cli`.

## If something doesn't work

- **`pip` isn't recognized** → Python wasn't added to PATH; re-run the
  installer and check the box, or use `py -m pip` instead of `pip`.
- **`zspan` isn't recognized** → use `python -m zspan_cli` (same thing).
- **The pull answers "no published meetings"** → the public catalog serves
  meetings whose broadcasts Z-SPAN has published; `zspan pick --list`
  shows which cities have some today.
- Everything the CLI prints is meant to be readable — the error text
  names the fix.
