#!/usr/bin/env python3
"""Verify the immutable JavaScript entry asset served by Z-SPAN Pages.

The repository's Cloudflare Pages Functions are shared by both production
hosts: ``functions/edgeProxy.ts`` selects the public/operator trust plane from
the request hostname, while ``functions/_middleware.ts`` wraps both hosts.
Accordingly, the unauthenticated ``https://zspan.org`` HTML verifies the same
Pages deployment and client bundle used by the Access-protected operator host.

With ``--commit``, the verifier also asks the Cloudflare Pages API for the
latest production deployment and requires its source commit to match. Set
``CLOUDFLARE_API_TOKEN``, ``CLOUDFLARE_ACCOUNT_ID``, and
``CLOUDFLARE_PAGES_PROJECT`` (or pass the latter two as arguments).
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import logging
import os
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("verify_pages_deploy")
ENTRY_ASSET_RE = re.compile(r"(?:^|/)assets/index-([A-Za-z0-9_-]+)\.js$")
JAVASCRIPT_MIME_TYPES = {
    "application/javascript",
    "application/x-javascript",
    "text/javascript",
}
MAX_HTML_BYTES = 5_000_000
MAX_ASSET_BYTES = 50_000_000
MAX_API_BYTES = 5_000_000


class EntryScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "script":
            return
        source = dict(attrs).get("src")
        if source and ENTRY_ASSET_RE.search(urlparse(source).path):
            self.sources.append(source)


def _fetch(
    url: str,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, bytes, str]:
    request_headers = {
        "Accept": "*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "zspan-pages-deploy-verifier/1.0",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError(f"response exceeded {max_bytes} bytes: {url}")
            return (
                response.status,
                response.headers.get_content_type(),
                body,
                response.geturl(),
            )
    except HTTPError as error:
        detail = error.read(2_000).decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"HTTP {error.code} for {url}"
            + (f": {detail}" if detail else "")
        ) from error
    except URLError as error:
        raise RuntimeError(f"request failed for {url}: {error.reason}") from error


def _latest_production_deployment(
    *,
    account_id: str,
    project: str,
    api_token: str,
) -> dict[str, Any]:
    query = urlencode({"env": "production", "per_page": 1})
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/pages/projects/{project}/deployments?{query}"
    )
    status, mime, body, _final_url = _fetch(
        url,
        max_bytes=MAX_API_BYTES,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
        },
    )
    if status != 200 or mime != "application/json":
        raise RuntimeError(
            f"Cloudflare deployments API returned HTTP {status} with {mime}"
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("Cloudflare deployments API returned invalid JSON") from error
    if not payload.get("success"):
        raise RuntimeError(
            f"Cloudflare deployments API rejected the request: {payload.get('errors')}"
        )
    deployments = payload.get("result")
    if not isinstance(deployments, list) or not deployments:
        raise RuntimeError("Cloudflare returned no production Pages deployments")
    deployment = deployments[0]
    if not isinstance(deployment, dict):
        raise RuntimeError("Cloudflare returned an invalid deployment record")
    return deployment


def _verify_commit(args: argparse.Namespace) -> None:
    if not args.commit:
        return
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account_id = (
        args.account_id
        or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    )
    project = (
        args.project
        or os.environ.get("CLOUDFLARE_PAGES_PROJECT", "").strip()
    )
    missing = [
        name
        for name, value in (
            ("CLOUDFLARE_API_TOKEN", api_token),
            ("CLOUDFLARE_ACCOUNT_ID/--account-id", account_id),
            ("CLOUDFLARE_PAGES_PROJECT/--project", project),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "--commit requires Cloudflare deployment lookup; missing "
            + ", ".join(missing)
        )

    deployment = _latest_production_deployment(
        account_id=account_id,
        project=project,
        api_token=api_token,
    )
    metadata = (deployment.get("deployment_trigger") or {}).get("metadata") or {}
    deployed_commit = str(metadata.get("commit_hash") or "").strip().lower()
    expected_commit = args.commit.strip().lower()
    if not deployed_commit:
        raise RuntimeError("latest production deployment has no source commit hash")
    if not deployed_commit.startswith(expected_commit):
        raise RuntimeError(
            "latest production commit mismatch: "
            f"expected {expected_commit}, got {deployed_commit}"
        )
    latest_stage = deployment.get("latest_stage") or {}
    stage_status = str(latest_stage.get("status") or "").lower()
    if stage_status and stage_status != "success":
        raise RuntimeError(
            f"latest production deployment is not successful: {stage_status}"
        )
    LOGGER.info("Cloudflare production deployment commit: %s", deployed_commit)


def _verify_live_bundle(args: argparse.Namespace) -> None:
    origin = args.origin.rstrip("/") + "/"
    parsed_origin = urlparse(origin)
    if parsed_origin.scheme != "https" or not parsed_origin.netloc:
        raise RuntimeError("--origin must be an absolute HTTPS URL")

    cache_buster = urlencode({"deploy-verify": int(time.time())})
    html_url = f"{origin}?{cache_buster}"
    status, _mime, html_body, final_html_url = _fetch(
        html_url,
        max_bytes=MAX_HTML_BYTES,
        headers={"Accept": "text/html"},
    )
    if status != 200:
        raise RuntimeError(f"production HTML returned HTTP {status}")
    if urlparse(final_html_url).netloc != parsed_origin.netloc:
        raise RuntimeError(
            f"production HTML redirected off origin to {final_html_url}"
        )

    parser = EntryScriptParser()
    parser.feed(html_body.decode("utf-8", errors="strict"))
    unique_sources = list(dict.fromkeys(parser.sources))
    if len(unique_sources) != 1:
        raise RuntimeError(
            "expected exactly one immutable assets/index-*.js entry, "
            f"found {len(unique_sources)}: {unique_sources}"
        )
    source = unique_sources[0]
    match = ENTRY_ASSET_RE.search(urlparse(source).path)
    if match is None:
        raise RuntimeError(f"entry script is not immutable: {source}")
    asset_hash = match.group(1)
    if args.asset_hash and asset_hash != args.asset_hash:
        raise RuntimeError(
            f"entry asset hash mismatch: expected {args.asset_hash}, got {asset_hash}"
        )

    asset_url = urljoin(origin, source)
    if urlparse(asset_url).netloc != parsed_origin.netloc:
        raise RuntimeError(f"entry asset points off the production origin: {asset_url}")
    asset_status, asset_mime, asset_body, final_asset_url = _fetch(
        asset_url,
        max_bytes=MAX_ASSET_BYTES,
        headers={"Accept": "text/javascript, application/javascript"},
    )
    if asset_status != 200:
        raise RuntimeError(f"entry asset returned HTTP {asset_status}")
    if urlparse(final_asset_url).netloc != parsed_origin.netloc:
        raise RuntimeError(f"entry asset redirected off origin to {final_asset_url}")
    if asset_mime not in JAVASCRIPT_MIME_TYPES:
        raise RuntimeError(
            f"entry asset has non-JavaScript MIME type: {asset_mime}"
        )
    stripped = asset_body.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not stripped:
        raise RuntimeError("entry asset body is empty")
    if stripped.startswith((b"<!doctype", b"<html", b"<head", b"<body")):
        raise RuntimeError("entry asset body is HTML, not JavaScript")

    LOGGER.info("Production HTML: %s", final_html_url)
    LOGGER.info("Immutable entry asset: %s", asset_url)
    LOGGER.info("Entry hash: %s", asset_hash)
    LOGGER.info(
        "Verified HTTP 200, JavaScript MIME, and non-HTML body (%d bytes)",
        len(asset_body),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the current Z-SPAN Cloudflare Pages production bundle.",
    )
    parser.add_argument(
        "--origin",
        default="https://zspan.org",
        help="Unauthenticated public Pages origin (default: https://zspan.org)",
    )
    parser.add_argument(
        "--commit",
        help="Expected production source commit (full hash or leading prefix)",
    )
    parser.add_argument(
        "--asset-hash",
        help="Expected index-*.js hash when checking a known bundle directly",
    )
    parser.add_argument(
        "--account-id",
        help="Cloudflare account id (or CLOUDFLARE_ACCOUNT_ID)",
    )
    parser.add_argument(
        "--project",
        help="Cloudflare Pages project name (or CLOUDFLARE_PAGES_PROJECT)",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )
    args = _parse_args()
    try:
        _verify_commit(args)
        _verify_live_bundle(args)
    except (RuntimeError, ValueError, UnicodeDecodeError) as error:
        LOGGER.error("%s", error)
        return 1
    LOGGER.info("Pages deployment verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
