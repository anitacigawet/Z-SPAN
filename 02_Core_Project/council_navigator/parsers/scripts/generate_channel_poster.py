#!/usr/bin/env python3.11
"""V1-Catalog-2 (2026-06-12): Generate a Z-SPAN channel poster via gemini-webapi.

Same surface James used manually (gemini.google.com with the "create image"
button + a Pro 3.x model with thinking), just driven from Python via the
existing gemini-webapi library and the cookies already in user_settings.json.
The prompt-side inputs (global style anchor + per-city composition template)
come from 01_Project_Overview/IMAGE_PROMPTS.md so the output stays cohesive
across the network — every poster reads as a sibling of Kingman / Bullhead
City / Lake Havasu / Colorado City.

Usage:
    python3.11 parsers/scripts/generate_channel_poster.py \\
        --city "Phoenix" \\
        --landmarks "the South Mountain ridgeline as a low silhouette across the lower third; a single small saguaro silhouette in the right third" \\
        --foreground "open Sonoran desert plain in deep blue-black; a thin horizontal road implied by converging perspective lines vanishing into the ridge" \\
        --mood "metropolitan-but-quiet"

    # smoke test (regenerates Lake Havasu — known winner; compare visually)
    python3.11 parsers/scripts/generate_channel_poster.py --smoke-lake-havasu

Defaults:
    --model    BASIC_PRO (gemini-3-pro). Override with --model PLUS_PRO,
               ADVANCED_PRO, or any Model enum name from gemini_webapi.constants.
    --out      Auto-derived from city slug at
               client/public/channels/<slug>-poster.png

Cookies: pulled from env_config.get_gemini_consumer_cookies() — sourced from
user_settings.json:gemini_secure_1psid + gemini_secure_1psidts (the same
cookies pipeline_operator_gemini_verify.py uses).

Reproducibility: the full prompt sent to Gemini is logged to the console and
saved next to the output as <slug>-poster.prompt.txt. Re-running with the
same args produces a NEW image (Gemini's RNG is per-call) but the prompt
inputs are pinned, so style stays controlled.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Optional

# ── repo-relative imports ─────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_PARSERS = _HERE.parent
if str(_PARSERS) not in sys.path:
    sys.path.insert(0, str(_PARSERS))

from env_config import get_gemini_consumer_cookies  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("zspan.generate_channel_poster")


# ── prompt assembly ──────────────────────────────────────────────────

# Repo-root anchor so we can resolve IMAGE_PROMPTS.md regardless of cwd.
_REPO_ROOT = _PARSERS.parent.parent.parent  # parsers/scripts/.. → parsers/.. → council_navigator/.. → 02_Core_Project/.. → ZSPAN/
_IMAGE_PROMPTS_PATH = _REPO_ROOT / "01_Project_Overview" / "IMAGE_PROMPTS.md"
_CHANNELS_DIR = _PARSERS.parent / "client" / "public" / "channels"


def _load_style_anchor() -> str:
    """Extract the 'Global style anchor' block from IMAGE_PROMPTS.md.

    Pinned to the canonical doc so style updates flow through automatically.
    """
    if not _IMAGE_PROMPTS_PATH.is_file():
        raise FileNotFoundError(
            f"IMAGE_PROMPTS.md not found at {_IMAGE_PROMPTS_PATH}"
        )
    text = _IMAGE_PROMPTS_PATH.read_text(encoding="utf-8")
    # The doc uses a single '> ' blockquote line for the anchor. Find it.
    match = re.search(r"^> (Z-SPAN is a civic streaming network.*?)(?:\n\n|\Z)",
                      text, re.DOTALL | re.MULTILINE)
    if not match:
        raise RuntimeError(
            "Could not locate the 'Z-SPAN is a civic streaming network…' "
            "anchor block in IMAGE_PROMPTS.md. Has the doc structure changed?"
        )
    # Strip the blockquote prefix from each line.
    anchor = re.sub(r"^>\s?", "", match.group(1), flags=re.MULTILINE).strip()
    return anchor


def build_poster_prompt(
    city: str,
    landmarks: str,
    foreground: str = "open high-desert plain in deep blue-black",
    mood: str = "dignified, dusk, civic-quiet",
    sibling_cities: Optional[list[str]] = None,
) -> str:
    """Assemble the full prompt by combining the style anchor + the per-city
    composition template that the 4 existing poster prompts share.

    Matches the structure of IMAGE_PROMPTS.md §4-7 (Kingman / Bullhead /
    Lake Havasu / Colorado City) so output is sibling-coherent.
    """
    anchor = _load_style_anchor()
    siblings = sibling_cities or [
        "Kingman", "Bullhead City", "Lake Havasu", "Colorado City",
    ]
    siblings_str = ", ".join(siblings)
    composition = (
        f"A 1600x900 cinematic channel-poster artwork for the {city} civic "
        f"broadcast channel. Composition: {landmarks}. Sky above is a "
        f"graduated dusk tone — warm amber #F5A524 at the horizon fading to "
        f"deep navy #0E1828 at top, no clouds, no stars. Foreground: "
        f"{foreground}. No text, no logos, no people. Slight 35mm film "
        f"grain. Mood: {mood}. Same color treatment, same time-of-day, same "
        f"restraint as the {siblings_str} posters — these are channels on "
        f"the same network. No glow effects, no lens flares, no neon. "
        f"Output 1600x900 PNG, no text anywhere."
    )
    # The anchor is style guidance the model reads first; the composition is
    # the per-image brief. Two paragraphs separated by a blank line — same
    # shape James pasted manually.
    return f"{anchor}\n\n{composition}"


# ── slug helpers ─────────────────────────────────────────────────────


def city_slug(city: str) -> str:
    """Lowercase, hyphen-joined, alphanumeric-only slug for the file name."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", city.strip().lower())
    return s.strip("-")


def default_output_path(city: str) -> Path:
    return _CHANNELS_DIR / f"{city_slug(city)}-poster.png"


# ── core run ─────────────────────────────────────────────────────────


async def generate_one(
    city: str,
    landmarks: str,
    foreground: str,
    mood: str,
    out_path: Path,
    model_name: str,
    sibling_cities: Optional[list[str]] = None,
) -> int:
    """Build the prompt, fire Gemini, save the image. Returns exit code."""
    # Cookies first — fail fast if missing.
    psid, psidts = get_gemini_consumer_cookies()
    if not psid or not psidts:
        logger.error(
            "Gemini cookies missing. Add gemini_secure_1psid + "
            "gemini_secure_1psidts to user_settings.json (see "
            "pipeline_operator_gemini_verify.py docstring for how to grab "
            "them from chrome://settings/cookies/detail?site=google.com)."
        )
        return 3

    # Lazy-imports so --help / smoke-test wiring works without the dep.
    from gemini_webapi import GeminiClient
    from gemini_webapi.constants import Model

    if not hasattr(Model, model_name):
        valid = [m.name for m in Model]
        logger.error(
            "Unknown model %r. Available: %s",
            model_name, ", ".join(valid),
        )
        return 4
    model = getattr(Model, model_name)

    prompt = build_poster_prompt(
        city=city,
        landmarks=landmarks,
        foreground=foreground,
        mood=mood,
        sibling_cities=sibling_cities,
    )

    # Sidecar: write the exact prompt next to where the image will land so
    # reruns are auditable and we can diff what changed when a result looks
    # off.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = out_path.with_suffix(".prompt.txt")
    prompt_path.write_text(prompt, encoding="utf-8")
    logger.info("Prompt written to %s (%d chars)", prompt_path, len(prompt))
    logger.info("Model: %s (%s)", model_name, model.model_name)

    logger.info("Initializing GeminiClient…")
    client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts)
    try:
        await asyncio.wait_for(client.init(timeout=30), timeout=45)
    except Exception as exc:
        logger.error("GeminiClient init failed: %s: %s", type(exc).__name__, exc)
        return 5

    try:
        logger.info("Generating image for %s…", city)
        response = await asyncio.wait_for(
            client.generate_content(prompt, model=model),
            timeout=180,
        )
    except asyncio.TimeoutError:
        logger.error("Gemini timed out after 180s")
        return 6
    except Exception as exc:
        logger.error("generate_content raised %s: %s", type(exc).__name__, exc)
        return 7
    finally:
        try:
            await client.close()
        except Exception:
            pass

    images = response.candidates[response.chosen].generated_images
    if not images:
        text_excerpt = (response.text or "")[:300]
        logger.error(
            "No generated_images in response. text excerpt: %r",
            text_excerpt,
        )
        return 8

    if len(images) > 1:
        logger.warning(
            "%d images returned; saving the first only.", len(images),
        )

    saved = await images[0].save(
        path=str(out_path.parent),
        filename=out_path.name,
        verbose=True,
    )
    logger.info("Saved → %s", saved)
    return 0


# ── CLI ─────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a Z-SPAN channel poster via gemini.google.com.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--city", help="City name, e.g. 'Phoenix' or 'Lake Havasu City'.")
    p.add_argument(
        "--landmarks",
        help="Composition brief — the distinctive part of the image "
             "(e.g. 'South Mountain ridgeline as a low silhouette; "
             "saguaro in the right third').",
    )
    p.add_argument(
        "--foreground",
        default="open high-desert plain in deep blue-black",
        help="Foreground description (default tracks Colorado City's prompt).",
    )
    p.add_argument(
        "--mood",
        default="dignified, dusk, civic-quiet",
        help="Mood phrase appended to the prompt (default: %(default)r).",
    )
    p.add_argument(
        "--model",
        default="BASIC_PRO",
        help="gemini-webapi Model enum name. Default %(default)s "
             "(=gemini-3-pro). Other options: PLUS_PRO, ADVANCED_PRO, "
             "BASIC_THINKING, PLUS_THINKING, ADVANCED_THINKING.",
    )
    p.add_argument(
        "--out",
        help="Output path. Default: client/public/channels/<slug>-poster.png",
    )
    p.add_argument(
        "--sibling-cities",
        help="Comma-separated list of sibling channel names referenced in the "
             "prompt for style coherence. Default: Kingman, Bullhead City, "
             "Lake Havasu, Colorado City.",
    )
    p.add_argument(
        "--smoke-lake-havasu",
        action="store_true",
        help="Smoke test — regenerate Lake Havasu (existing winner). Compares "
             "the new output to the committed lake-havasu-poster.png by eye.",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.smoke_lake_havasu:
        city = "Lake Havasu City"
        landmarks = (
            "a stylized silhouette of the original London Bridge (the "
            "relocated stone-arch bridge, with its three repeating arches) "
            "crossing the lower-middle of the frame in deep blue-black "
            "(#0F1218), reflected in still lake water below as a slightly "
            "softer mirrored shape. A single thin pinpoint of light at one "
            "of the bridge's lampposts, warm amber. The far shoreline is "
            "implied by a low dark ridgeline"
        )
        foreground = "still lake water below the bridge, mirroring the arches"
        mood = "dignified, dusk, monumental-but-quiet"
        # Smoke test goes to a sibling path so we don't overwrite the
        # committed winner.
        out_path = _CHANNELS_DIR / "lake-havasu-poster.smoke.png"
        sibling_cities = ["Kingman", "Bullhead City", "Colorado City"]
    else:
        if not args.city or not args.landmarks:
            logger.error("--city and --landmarks are required (or use --smoke-lake-havasu).")
            return 2
        city = args.city
        landmarks = args.landmarks
        foreground = args.foreground
        mood = args.mood
        out_path = Path(args.out) if args.out else default_output_path(city)
        sibling_cities = (
            [s.strip() for s in args.sibling_cities.split(",") if s.strip()]
            if args.sibling_cities else None
        )

    rc = asyncio.run(generate_one(
        city=city,
        landmarks=landmarks,
        foreground=foreground,
        mood=mood,
        out_path=out_path,
        model_name=args.model,
        sibling_cities=sibling_cities,
    ))
    return rc


if __name__ == "__main__":
    sys.exit(main())
