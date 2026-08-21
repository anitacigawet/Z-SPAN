#!/usr/bin/env python3
"""Fail if Docker's local build context contains denylisted material.

This is the interim guard for sol pen-test Finding #6. It evaluates the
repository's `.dockerignore`, walks only paths that remain in the context, and
reports any included path matching the local-secret/internal-state policy.

It is intentionally a denylist check. It does not inspect image layers and
cannot prove that an image is clean. Full closure requires allowlisted COPY
declarations (or an allowlisted generated context), a clean build/start test,
and inspection of every `docker save` layer.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class IgnoreRule:
    """One normalized `.dockerignore`-style rule."""

    source: str
    negated: bool
    directory_only: bool
    basename_only: bool
    regex: re.Pattern[str]

    def matches(self, relative_path: PurePosixPath, is_directory: bool) -> bool:
        parts = relative_path.parts
        if self.basename_only:
            limit = len(parts) if is_directory else max(len(parts) - 1, 0)
            if self.directory_only:
                return any(self.regex.fullmatch(part) for part in parts[:limit])
            return any(self.regex.fullmatch(part) for part in parts)

        candidates: list[str] = []
        if not self.directory_only or is_directory:
            candidates.append(relative_path.as_posix())
        candidates.extend(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )
        return any(self.regex.fullmatch(candidate) for candidate in candidates)


@dataclass(frozen=True)
class DenyRule:
    pattern: str
    reason: str
    rule: IgnoreRule


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate Docker's useful glob subset, including `**`, to a regex."""
    output = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    output.append("(?:.*/)?")
                    index += 1
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
        elif char == "?":
            output.append("[^/]")
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close == -1:
                output.append(r"\[")
            else:
                content = pattern[index + 1 : close]
                if content.startswith("!"):
                    content = "^" + content[1:]
                output.append("[" + content.replace("\\", r"\\") + "]")
                index = close
        else:
            output.append(re.escape(char))
        index += 1
    output.append("$")
    return re.compile("".join(output))


def _parse_rule(line: str, *, source: str) -> IgnoreRule | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    negated = stripped.startswith("!")
    if negated:
        stripped = stripped[1:]
    directory_only = stripped.endswith("/")
    normalized = stripped.strip("/")
    if not normalized or normalized == ".":
        return None

    return IgnoreRule(
        source=source,
        negated=negated,
        directory_only=directory_only,
        basename_only="/" not in normalized,
        regex=_glob_regex(normalized),
    )


def load_ignore_rules(ignore_file: Path) -> list[IgnoreRule]:
    if not ignore_file.is_file():
        raise ValueError(f"Docker ignore file not found: {ignore_file}")

    rules: list[IgnoreRule] = []
    for line_number, line in enumerate(
        ignore_file.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        rule = _parse_rule(line, source=f"{ignore_file.name}:{line_number}")
        if rule is not None:
            rules.append(rule)
    return rules


def is_included(
    relative_path: PurePosixPath,
    *,
    is_directory: bool,
    rules: list[IgnoreRule],
) -> bool:
    included = True
    for rule in rules:
        if rule.matches(relative_path, is_directory):
            included = rule.negated
    return included


DENY_SPECS: tuple[tuple[str, str], ...] = (
    (".venv*", "local Python environment"),
    (".wrangler/", "Cloudflare deploy/build state"),
    ("**/.claude/", "Claude session/release state"),
    ("**/.codex/", "Codex session state"),
    ("**/.agents/", "local agent state"),
    ("agents/", "operator agent fleet"),
    ("**/.bearer_token", "bearer token"),
    ("**/*key*.pem*", "private-key material"),
    ("**/secrets/", "secrets directory"),
    ("**/.env*", "environment/credential file"),
    ("**/*.local", "local-only configuration"),
    ("**/*.bak*", "backup file"),
    ("**/*.backup", "backup file"),
    ("**/dev-cert.pem", "local development certificate"),
    ("**/client_secret*.json", "OAuth client secret"),
    ("**/*_oauth*.json", "OAuth credential"),
    ("**/*.credentials.json", "downloaded credentials"),
    ("**/service-account*.json", "service-account credential"),
    ("**/youtube_refresh_token.json", "OAuth refresh token"),
    ("**/PROMPT_REVIEW_LEDGER*.md", "prompt-review ledger"),
    ("**/PROMPT_REVIEW_*.md", "prompt-review sibling"),
    ("**/city_intelligence/", "local city intelligence"),
    ("**/state_scaffolding/", "local state discovery scaffolding"),
    ("02_Core_Project/mac_*/", "local Mac relay/runtime node"),
    ("02_Core_Project/pc_agent_relay/", "local PC agent relay"),
    ("02_Core_Project/surfacepro_rag_node/", "local Surface Pro RAG node"),
    ("01_Project_Overview/", "sealed governance corpus"),
    ("03_Research/", "sealed research corpus"),
    ("04_Operator_Side/", "operator-only corpus"),
    ("journals/", "operator journal corpus"),
    ("verbatim_ledger/", "operator verbatim ledger"),
    ("CLAUDE*.md", "operator/AI instructions"),
    ("AGENTS.md", "operator/AI instructions"),
    ("AUTOPILOT_PROTOCOL.md", "operator protocol"),
    ("ROADMAP.md", "internal roadmap"),
    ("TASKS*.md", "internal task ledger"),
    ("BRAINSTORM_LOG.md", "internal brainstorm ledger"),
    ("HUMAN_API_HANDOFF.md", "internal handoff"),
    ("OCTANE_MODE_EXPERIMENT.md", "internal experiment record"),
    ("respawn*.md", "internal recovery material"),
    ("EMERGENCY_ONBOARDING.md", "internal recovery material"),
    ("PROJECT_GENOTYPE.md", "internal recovery material"),
    ("**/operator_only/", "operator-only source"),
    ("ops/prod_db_archive/", "PII-bearing production database archive"),
    ("ops/*-logs/", "operator/agent runtime logs"),
    ("ops/REMAKE2_EXECUTION_NOTES.md", "PII-bearing execution notes"),
    ("**/STATUS.json", "per-machine runtime state"),
    ("**/user_settings.json", "user settings/secrets"),
    ("**/orchestrator_autonomy.json", "operator runtime state"),
    ("**/parser_test_results.json", "parser runtime state"),
    ("**/FINAL_HEALTH_REPORT.json", "parser health state"),
    ("**/health_report_*.json", "parser health state"),
    ("**/pipeline_state.json", "pipeline runtime state"),
    ("**/meetings_cache.db*", "runtime meeting database"),
    ("**/*.db", "local database"),
    ("**/*.sqlite", "local database"),
    ("**/*.sqlite3", "local database"),
    ("**/backups/", "local backup material"),
    ("**/worker_logs/", "worker runtime logs"),
    ("**/pids/", "runtime process state"),
    ("**/*.pid", "runtime process state"),
    ("**/*.pid.lock", "runtime process state"),
    ("**/*.seed", "runtime seed state"),
    ("**/tmp/", "temporary runtime state"),
    ("**/temp/", "temporary runtime state"),
    ("**/media_quarantine/", "quarantined local media"),
    ("**/mac_*_result.json", "local relay diagnostic"),
    ("**/mac_*_diagnostic.json", "local relay diagnostic"),
    ("**/council_members_data.json", "sealed council roster"),
    ("**/USER_SETTINGS_KEYS.md", "user-settings key ledger"),
    (".preview/", "local preview state"),
    (".tmp/", "session scratch state"),
    ("docs_export_*/", "generated internal documentation export"),
    ("**/.node_repl_history", "local REPL history"),
    ("*.whl", "built Python distribution"),
    ("*.tar.gz", "built source distribution"),
    ("*.tgz", "built package distribution"),
    ("02_Core_Project/zspan_cli/build/", "built Python distribution tree"),
    ("02_Core_Project/zspan_cli/dist/", "built Python distribution tree"),
    (
        "02_Core_Project/zspan_cli/zspan_cli/_prompts/",
        "generated vendored prompt copy",
    ),
)


def build_deny_rules() -> list[DenyRule]:
    result: list[DenyRule] = []
    for pattern, reason in DENY_SPECS:
        parsed = _parse_rule(pattern, source=f"policy:{pattern}")
        if parsed is None:
            raise AssertionError(f"invalid built-in deny pattern: {pattern}")
        result.append(DenyRule(pattern=pattern, reason=reason, rule=parsed))
    return result


def scan_context(
    root: Path,
    ignore_rules: list[IgnoreRule],
) -> tuple[list[tuple[PurePosixPath, DenyRule]], int, int]:
    deny_rules = build_deny_rules()
    violations: list[tuple[PurePosixPath, DenyRule]] = []
    included_count = 0
    excluded_count = 0

    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            path = current_path / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if not is_included(relative, is_directory=True, rules=ignore_rules):
                excluded_count += 1
                continue
            included_count += 1
            for deny_rule in deny_rules:
                if deny_rule.rule.matches(relative, True):
                    violations.append((relative, deny_rule))
                    break
            if not path.is_symlink():
                kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in sorted(file_names):
            path = current_path / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if not is_included(relative, is_directory=False, rules=ignore_rules):
                excluded_count += 1
                continue
            included_count += 1
            for deny_rule in deny_rules:
                if deny_rule.rule.matches(relative, False):
                    violations.append((relative, deny_rule))
                    break

    return violations, included_count, excluded_count


def main() -> int:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Scan the effective Docker context for secrets/internal state.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help="Docker context root (default: repository root)",
    )
    parser.add_argument(
        "--dockerignore",
        type=Path,
        help="ignore file (default: <root>/.dockerignore)",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    ignore_file = (
        args.dockerignore.resolve()
        if args.dockerignore
        else root / ".dockerignore"
    )
    if not root.is_dir():
        print(f"error: context root is not a directory: {root}", file=sys.stderr)
        return 2

    try:
        ignore_rules = load_ignore_rules(ignore_file)
        violations, included_count, excluded_count = scan_context(root, ignore_rules)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if violations:
        print(
            f"FAIL: Docker context contains {len(violations)} denylisted path(s):",
            file=sys.stderr,
        )
        for path, deny_rule in violations:
            print(
                f"  {path.as_posix()}  [{deny_rule.pattern}: {deny_rule.reason}]",
                file=sys.stderr,
            )
        print(
            "Update .dockerignore or remove the material before building.",
            file=sys.stderr,
        )
        return 1

    print(
        "PASS: effective Docker context contains no denylisted paths "
        f"({included_count} included entries, {excluded_count} excluded roots).",
    )
    print(
        "INTERIM denylist check only; allowlisted COPY + docker-save layer "
        "inspection remain deferred.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
