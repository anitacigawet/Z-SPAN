#!/usr/bin/env python3
"""Create a new country-library repository from the Respawn template."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path


KERNEL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = KERNEL_ROOT / "template"
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
RTL_LANGUAGES = {"ar", "ckb", "dv", "fa", "he", "ku", "ps", "sd", "ug", "ur", "yi"}


def _nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def _country_code(value: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise argparse.ArgumentTypeError("country code must contain exactly two letters")
    return code


def _language_tag(value: str) -> str:
    tag = value.strip()
    if not LANGUAGE_TAG.fullmatch(tag):
        raise argparse.ArgumentTypeError("primary locale must be a BCP 47-style language tag")
    return tag


def _replace_tokens(root: Path, replacements: dict[str, str]) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for token, value in replacements.items():
            text = text.replace(token, value)
        path.write_text(text, encoding="utf-8")


def _materialize_repository_documents(output: Path) -> None:
    for name in ("AGENTS", "TASKS", "DECISIONS"):
        source = output / f"{name}.template.md"
        source.rename(output / f"{name}.md")


def _copy_standalone_runtime(output: Path) -> None:
    """Carry conformance and reference-site tools into the new repository."""

    tools_output = output / "tools"
    tools_output.mkdir()
    for tool_name in ("validate_seed.py", "build_site.py"):
        shutil.copy2(KERNEL_ROOT / "tools" / tool_name, tools_output / tool_name)
    shutil.copytree(KERNEL_ROOT / "contracts", output / "contracts")
    shutil.copytree(KERNEL_ROOT / "site_assets", output / "site_assets")


def _configure_locales(
    output: Path,
    *,
    country_name: str,
    project_name: str,
    primary_locale: str,
    language_name: str | None,
    direction: str | None,
) -> None:
    if primary_locale == "en":
        return

    english_path = output / "locales" / "en.json"
    english = json.loads(english_path.read_text(encoding="utf-8"))
    base_language = primary_locale.split("-", 1)[0].lower()
    resolved_direction = direction or ("rtl" if base_language in RTL_LANGUAGES else "ltr")

    primary_strings = {
        key: project_name if key == "project_name" else f"[Translation needed] {value}"
        for key, value in english["strings"].items()
    }
    primary = {
        "schema_version": 1,
        "locale": primary_locale,
        "language_name": language_name or primary_locale,
        "direction": resolved_direction,
        "status": "draft",
        "strings": primary_strings,
        "review": {
            "method": "unreviewed",
            "notes": "English source placeholders. Translate and independently back-check before local use."
        }
    }
    target = output / "locales" / f"{primary_locale}.json"
    target.write_text(json.dumps(primary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    profile_path = output / "country" / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["locales"]["supported"] = [primary_locale, "en"]
    profile["country"]["name"].setdefault("en", country_name)
    profile["jurisdiction_model"]["levels"][0]["terms"].setdefault("en", country_name)
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # The English source remains useful for back-checking even when it is not
    # the public default. Keep its generated project wording intact.
    english["strings"]["project_name"] = project_name
    english_path.write_text(json.dumps(english, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_seed(
    output: Path,
    *,
    country_name: str,
    country_code: str,
    project_name: str,
    primary_locale: str,
    language_name: str | None = None,
    direction: str | None = None,
) -> Path:
    """Create a seed without overwriting any existing path."""

    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {output}")
    if not TEMPLATE_ROOT.is_dir():
        raise FileNotFoundError(f"template directory is missing: {TEMPLATE_ROOT}")

    shutil.copytree(TEMPLATE_ROOT, output)
    _materialize_repository_documents(output)
    replacements = {
        "{{PROJECT_NAME}}": project_name,
        "{{COUNTRY_NAME}}": country_name,
        "{{COUNTRY_CODE}}": country_code,
        "{{PRIMARY_LOCALE}}": primary_locale,
        "{{GENERATED_ON}}": date.today().isoformat(),
    }
    _replace_tokens(output, replacements)
    _copy_standalone_runtime(output)
    _configure_locales(
        output,
        country_name=country_name,
        project_name=project_name,
        primary_locale=primary_locale,
        language_name=language_name,
        direction=direction,
    )
    # A generated repository is derived from the kernel. Carry the current
    # repository terms with it instead of leaving the new project unlicensed
    # or relying on historical prose about prior licenses.
    for legal_name in ("LICENSE", "NOTICE"):
        legal_source = KERNEL_ROOT.parent / legal_name
        if legal_source.is_file():
            shutil.copy2(legal_source, output / legal_name)
    (output / "registry" / "private").mkdir(parents=True, exist_ok=True)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an independent country-library seed from Respawn Kernel."
    )
    parser.add_argument("--country", required=True, type=_nonempty, help="Country name")
    parser.add_argument("--code", required=True, type=_country_code, help="Two-letter country code")
    parser.add_argument("--project-name", required=True, type=_nonempty, help="Locally chosen project name")
    parser.add_argument("--primary-locale", required=True, type=_language_tag, help="Primary BCP 47 language tag")
    parser.add_argument("--language-name", type=_nonempty, help="Human-readable primary language name")
    parser.add_argument("--direction", choices=("ltr", "rtl"), help="Override inferred text direction")
    parser.add_argument("--output", required=True, type=Path, help="New repository path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = create_seed(
            args.output,
            country_name=args.country,
            country_code=args.code,
            project_name=args.project_name,
            primary_locale=args.primary_locale,
            language_name=args.language_name,
            direction=args.direction,
        )
    except (FileExistsError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Created country-library seed at {output}")
    print(f"Next: python3 {KERNEL_ROOT / 'tools' / 'validate_seed.py'} {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
