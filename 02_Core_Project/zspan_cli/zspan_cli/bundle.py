"""The built-site bundle fetcher.

The full zspan.org client bundle (dist/public) is ~200 MB of art-heavy
assets — far too heavy to vendor in the wheel the way the prompts corpus
is. It ships instead as a release asset on the same tagged release the
wheel comes from, and `zspan open` offers a one-time download into
~/.zspan/webapp when no bundle is present (consent-first: the size is
stated, nothing downloads without a yes).

Integrity: the zip's SHA256 is pinned HERE at release time, so the wheel
only ever serves the exact bundle its release shipped — a silently
edited release asset fails the check loudly instead of being served.

Belt-and-suspenders resource caps: the SHA
pin verifies "this is the file we tagged," but not that the tagged file
is safe. Download and extract are bounded on total size, member count,
per-file size, and compression ratio; the path-containment check uses
Path.is_relative_to (not str-prefix, which was defeatable by sibling
directory names like `unpacked-evil`); symlink/device zip entries are
rejected; extraction is per-file after validation instead of
zipfile.extractall.

If a release does not carry the optional bundle asset, an anonymous 404 is
reported plainly rather than dressed as a generic network failure. Every
published asset must match the digest pinned for its release.
"""
from __future__ import annotations

import hashlib
import shutil
import stat as _stat
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import requests

from zspan_cli.config import zspan_home

BUNDLE_URL = (
    "https://github.com/anitacigawet/Z-SPAN/releases/download/"
    "zspan-cli-v0.1.0/zspan-webapp-bundle-v0.zip"
)
# Pinned at release time — the sha256 of the exact zip attached to the
# zspan-cli-v0.1.0 release, built from dist/public at tag time. Re-pin
# this whenever a new bundle is attached to a release.
BUNDLE_SHA256 = "0236d1743f1f0e6b9f5b6284898b5a07e0aa425d855a72c6fbcecda33027184b"
BUNDLE_SIZE_MB = 158  # stated in the consent prompt; approximate on purpose

# Resource caps for the download + extract cycle. The current bundle is
# ~158MB compressed, packing dist/public which is ~180-200MB uncompressed
# across ~200 files. These caps give ~3x headroom for growth without
# leaving the fetcher unbounded.
_MB = 1024 * 1024
MAX_DOWNLOAD_BYTES = 500 * _MB          # abort mid-stream if the download exceeds this
MAX_UNCOMPRESSED_BYTES = 1024 * _MB     # sum of ZipInfo.file_size across all members
MAX_MEMBERS = 10_000                    # ZipInfo count in the archive
MAX_PER_FILE_BYTES = 200 * _MB          # any single member larger than this rejects
MAX_COMPRESSION_RATIO = 100             # per-file file_size / compress_size ceiling


class BundleError(Exception):
    """Download/verify/unpack failed in a way the user should read."""


def webapp_install_dir() -> Path:
    """Where the downloaded bundle lives: ~/.zspan/webapp."""
    return zspan_home() / "webapp"


def fetch_bundle(say: Callable[[str], None] = print) -> Path:
    """Download, verify, and unpack the bundle. Returns the webapp dir.

    Atomic-ish: everything happens in temp space; the final directory
    appears only after the hash verified and the zip unpacked clean.
    """
    dest = webapp_install_dir()
    say(f"Downloading the Z-SPAN site bundle (~{BUNDLE_SIZE_MB} MB) ...")
    say(f"  {BUNDLE_URL}")

    tmp_root = Path(tempfile.mkdtemp(prefix="zspan-bundle-"))
    zip_path = tmp_root / "bundle.zip"
    try:
        try:
            with requests.get(BUNDLE_URL, stream=True, timeout=60) as resp:
                if resp.status_code == 404:
                    raise BundleError(
                        "the release asset answered 404 — the zspan-cli-v0 "
                        "release isn't public yet (it opens with the site), "
                        "or the asset name changed. Run from a clone with "
                        "dist/public built in the meantime."
                    )
                if resp.status_code != 200:
                    raise BundleError(
                        f"the release asset answered HTTP {resp.status_code}."
                    )
                digest = hashlib.sha256()
                received = 0
                next_mark = 25 * 1024 * 1024
                with zip_path.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        received += len(chunk)
                        if received > MAX_DOWNLOAD_BYTES:
                            raise BundleError(
                                "the release asset is larger than the "
                                f"{MAX_DOWNLOAD_BYTES // _MB} MB safety cap "
                                "— refusing to keep downloading it."
                            )
                        out.write(chunk)
                        digest.update(chunk)
                        if received >= next_mark:
                            say(f"  ... {received // (1024 * 1024)} MB")
                            next_mark += 25 * 1024 * 1024
        except requests.exceptions.RequestException as e:
            raise BundleError(
                f"could not reach the release ({type(e).__name__}). "
                "Check your connection and re-run — nothing partial was kept."
            ) from e

        got = digest.hexdigest()
        if got != BUNDLE_SHA256:
            raise BundleError(
                "the downloaded bundle's SHA256 doesn't match the one this "
                "release pinned — refusing to install it. (Expected "
                f"{BUNDLE_SHA256[:12]}…, got {got[:12]}….) Re-run to retry; "
                "if it persists, the release asset changed after tagging."
            )
        say(f"  {received // (1024 * 1024)} MB received · SHA256 verified")

        unpack = tmp_root / "unpacked"
        unpack.mkdir()
        unpack_root = unpack.resolve()
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()

            if len(infos) > MAX_MEMBERS:
                raise BundleError(
                    f"the bundle zip carries {len(infos)} members, more than the "
                    f"{MAX_MEMBERS} safety cap — refusing to unpack it."
                )

            total_uncompressed = 0
            for info in infos:
                # Reject symlinks / devices via the Unix mode in external_attr.
                # Only inspect the file-type field; entries written via
                # zipfile.writestr often carry only permission bits (no type
                # bits), which is ambiguous — treat that as OK. A KNOWN
                # non-regular file type (symlink, device, fifo, socket) is
                # rejected outright.
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = mode & 0o170000  # stat.S_IFMT mask
                if file_type and file_type not in (
                    _stat.S_IFREG, _stat.S_IFDIR
                ):
                    raise BundleError(
                        f"the bundle zip carries a non-regular entry "
                        f"({info.filename!r}) — refusing to unpack it."
                    )

                if info.file_size > MAX_PER_FILE_BYTES:
                    raise BundleError(
                        f"the bundle zip member {info.filename!r} is "
                        f"{info.file_size} bytes, over the "
                        f"{MAX_PER_FILE_BYTES // _MB} MB per-file cap "
                        "— refusing to unpack it."
                    )

                # Compression-ratio (zip-bomb) guard: skip zero-length entries.
                if info.compress_size > 0 and (
                    info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise BundleError(
                        f"the bundle zip member {info.filename!r} compresses "
                        f"{info.file_size / info.compress_size:.1f}:1 (over "
                        f"the {MAX_COMPRESSION_RATIO}:1 ratio cap) "
                        "— refusing to unpack it."
                    )

                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise BundleError(
                        f"the bundle zip uncompresses to more than "
                        f"{MAX_UNCOMPRESSED_BYTES // _MB} MB — refusing to "
                        "unpack it."
                    )

                # Path containment: use is_relative_to, NOT str-prefix.
                # str-prefix accepted `unpacked-evil/foo` as inside `unpacked/`
                # because the two share a leading string.
                target = (unpack / info.filename).resolve()
                try:
                    _relative = target.relative_to(unpack_root)
                except ValueError:
                    raise BundleError(
                        f"the bundle zip carries an unsafe path "
                        f"({info.filename!r}) — refusing to unpack it."
                    )

            # All members validated; extract them one at a time (no extractall,
            # which iterates without any pre-flight in some Python versions).
            for info in infos:
                zf.extract(info, unpack)

        # The zip packs the bundle's CONTENTS at its root (index.html at
        # top level); tolerate one wrapping directory as well.
        root = unpack
        if not (root / "index.html").is_file():
            subdirs = [p for p in root.iterdir() if p.is_dir()]
            if len(subdirs) == 1 and (subdirs[0] / "index.html").is_file():
                root = subdirs[0]
            else:
                raise BundleError(
                    "the bundle unpacked but carries no index.html — "
                    "the asset doesn't look like the site bundle."
                )

        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(dest))
        say(f"Site bundle installed at {dest}")
        return dest
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
