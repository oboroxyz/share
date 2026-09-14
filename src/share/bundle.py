"""Immutable, multi-file shares. Cloud credentials stay in the existing CLI config."""

import argparse
import fnmatch
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_FILES = 1000
MAX_BYTES = 100 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
PREFIX_RE = re.compile(r"__bundles__/[0-9a-f]{32}/\Z")
SECRET_NAMES = {
    "credentials", "credentials.json", "secrets.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
SKIP_DIRS = {"node_modules", "__pycache__"}


class BundleError(Exception):
    """A preflight, publication, or cleanup error safe to show to the user."""


@dataclass(frozen=True)
class BundleFile:
    path: str
    size: int
    content_type: str
    mtime_ns: int


def safe_path(path: str) -> bool:
    return (
        bool(path)
        and len(path.encode("utf-8")) <= 900
        and not re.search(r"[\\\x00-\x1f\x7f]", path)
        and all(part and not part.startswith(".") for part in path.split("/"))
    )


def collect_files(root: Path, excludes: list[str]) -> tuple[list[BundleFile], list[str]]:
    """Preflight only: no credentials, network access, or source-file changes."""
    if root.is_symlink() or not root.is_dir():
        raise BundleError(f"Expected a real directory (not a symlink): {root}")
    files, skipped = [], []
    total = 0

    def walk_error(error):
        raise BundleError(f"Cannot read bundle directory: {error}") from error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            lower = name.lower()
            excluded = (
                name.startswith(".")
                or lower in SECRET_NAMES
                or path.suffix.lower() in SECRET_SUFFIXES
                or path.is_symlink()
                or (name in dirs and name in SKIP_DIRS)
                or any(fnmatch.fnmatchcase(relative, pattern) for pattern in excludes)
            )
            if excluded:
                skipped.append(relative)
                if name in dirs:
                    dirs.remove(name)
                continue
            if not safe_path(relative):
                raise BundleError(f"Unsafe or overlong bundle path: {relative!r}")
            if name in dirs:
                continue
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise BundleError(f"Not a regular file: {relative}")
            total += info.st_size
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise BundleError("Bundle limit exceeded: 1,000 files / 100 MiB total")
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if path.suffix.lower() in {".js", ".mjs"}:
                content_type = "text/javascript"
            files.append(BundleFile(relative, info.st_size, content_type, info.st_mtime_ns))
        dirs.sort()
    if not files:
        raise BundleError("No files to upload after exclusions")
    return sorted(files, key=lambda file: file.path), sorted(skipped)


def choose_entry(files: list[BundleFile], entry: str | None) -> str | None:
    paths = {file.path for file in files}
    if entry is not None:
        if not safe_path(entry) or entry not in paths:
            raise BundleError("--entry must name an included file relative to the bundle root")
        return entry
    return "index.html" if "index.html" in paths else None


def snapshot(root: Path, files: list[BundleFile], destination: Path) -> None:
    """Stage exact bytes; refuse symlink swaps in files AND parent directories.

    dir_fd and O_NOFOLLOW are supported on macOS/Linux. Fail closed elsewhere.
    Staging finishes before the first object upload and never strips metadata.
    """
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise BundleError("Safe bundle staging currently requires macOS or Linux")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise BundleError(f"Cannot safely open bundle root: {error}") from error
    try:
        for file in files:
            directory_fd = os.dup(root_fd)
            try:
                parts = file.path.split("/")
                for part in parts[:-1]:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    os.close(directory_fd)
                    directory_fd = next_fd
                fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_size != file.size
                        or before.st_mtime_ns != file.mtime_ns
                    ):
                        raise BundleError(f"File changed during preflight: {file.path}")
                    output = destination / file.path
                    output.parent.mkdir(parents=True, exist_ok=True)
                    copied = 0
                    with output.open("wb") as target:
                        while chunk := source.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > file.size:
                                raise BundleError(f"File grew during staging: {file.path}")
                            target.write(chunk)
                    after = os.fstat(source.fileno())
                    if copied != file.size or after.st_mtime_ns != before.st_mtime_ns:
                        raise BundleError(f"File changed during staging: {file.path}")
            finally:
                os.close(directory_fd)
    except OSError as error:
        raise BundleError(f"Cannot safely stage bundle: {error}") from error
    finally:
        os.close(root_fd)


def delete_prefix(s3, bucket: str, prefix: str) -> None:
    """Revoke the manifest first; retain KV until all object deletions succeed."""
    if not PREFIX_RE.fullmatch(prefix):
        raise BundleError("Refusing to delete an invalid bundle prefix")
    s3.delete_object(Bucket=bucket, Key=prefix + ".manifest.json")
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    for page in pages:
        keys = [item["Key"] for item in page.get("Contents", [])]
        if any(not key.startswith(prefix) for key in keys):
            raise BundleError("R2 listing returned an object outside the bundle prefix")
        for offset in range(0, len(keys), 1000):
            response = s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in keys[offset:offset + 1000]], "Quiet": True},
            )
            if response.get("Errors"):
                raise BundleError("Some bundle objects could not be deleted; retry share rm")


def slug_from_url(value: str, public_base: str) -> str | None:
    """Accept only this deployment's share root URL, never an asset URL."""
    try:
        url, base = urlsplit(value), urlsplit(public_base)
    except ValueError:
        return None
    prefix = base.path.rstrip("/") + "/"
    if (url.scheme, url.netloc) != (base.scheme, base.netloc) or not url.path.startswith(prefix):
        return None
    slug = unquote(url.path[len(prefix):].removesuffix("/"))
    return slug if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", slug) else None


def delete_bundle(config: dict, cf, target: dict, entries: list[dict]) -> None:
    from . import console, kv_delete, r2_client

    prefix = target.get("r2_prefix", "")
    if not PREFIX_RE.fullmatch(prefix):
        raise BundleError("Refusing to delete an invalid bundle prefix")
    shared = any(
        entry.get("slug") != target["slug"] and entry.get("r2_prefix") == prefix
        for entry in entries
    )
    if not shared:
        delete_prefix(r2_client(config), config["cloudflare"]["bucket"], prefix)
    kv_delete(config, cf, f"slug:{target['slug']}")
    console.print(f"Deleted bundle /{target['slug']}/", markup=False)
    if shared:
        console.print("Objects kept: another share references this bundle", markup=False)


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("bundle", help="Share a directory under one URL")
    parser.add_argument("directory", help="Directory containing the finished artifacts")
    parser.add_argument("--entry", help="Entry file relative to the directory (default: index.html or listing)")
    parser.add_argument("--name", help="Display name (default: directory name)")
    parser.add_argument("--slug", help="Custom slug (default: 128-bit random slug)")
    parser.add_argument("--public", action="store_true", help="Show on landing page (default: unlisted)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="Exclude a root-relative glob; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="List included/excluded paths without uploading")


def cmd_bundle(args: argparse.Namespace) -> None:
    from . import cf_client, generate_slug, kv_get, kv_put, load_config, r2_client

    root = Path(args.directory).expanduser().absolute()
    files, skipped = collect_files(root, args.exclude)
    entry = choose_entry(files, args.entry)
    manifest = {
        "version": 1, "entry": entry,
        "files": [
            {"path": file.path, "size": file.size, "content_type": file.content_type}
            for file in files
        ],
    }
    manifest_body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    if len(manifest_body) > MAX_MANIFEST_BYTES:
        raise BundleError("Bundle manifest exceeds 1 MiB")
    if args.dry_run:
        for file in files:
            print(f"include  {file.path} ({file.size} bytes)")
        for path in skipped:
            print(f"exclude  {path}")
        print(f"{len(files)} files, {sum(file.size for file in files)} bytes; entry: {entry or '(listing)'}")
        return

    if skipped:
        print(f"Excluded {len(skipped)} paths; use --dry-run to inspect them.", file=sys.stderr)
    config = load_config()
    slug = generate_slug(args.slug) if args.slug else secrets.token_hex(16)
    if slug in {"index.html", "api"}:
        raise BundleError("This slug is reserved by the Worker")
    cf, s3 = cf_client(config), r2_client(config)
    if kv_get(config, cf, f"slug:{slug}") is not None:
        raise BundleError(f"Slug '{slug}' already exists; choose another slug")
    prefix = f"__bundles__/{secrets.token_hex(16)}/"
    bucket = config["cloudflare"]["bucket"]
    with tempfile.TemporaryDirectory(prefix="share-bundle-") as temporary:
        staged = Path(temporary)
        snapshot(root, files, staged)
        try:
            for file in files:
                s3.upload_file(
                    str(staged / file.path), bucket, prefix + file.path,
                    ExtraArgs={"ContentType": file.content_type},
                )
            s3.put_object(
                Bucket=bucket, Key=prefix + ".manifest.json", Body=manifest_body,
                ContentType="application/json",
            )
        except (Exception, KeyboardInterrupt):
            try:
                delete_prefix(s3, bucket, prefix)
            except Exception as cleanup_error:
                print(f"Cleanup failed for R2 prefix {prefix}: {cleanup_error}", file=sys.stderr)
            raise

    metadata = {
        "type": "bundle", "name": args.name or root.name,
        "slug": slug, "r2_prefix": prefix, "file_count": len(files),
        "size": sum(file.size for file in files), "entry": entry,
        "uploaded_at": datetime.now(UTC).isoformat(), "downloads": 0,
        "public": args.public,
    }
    try:
        # Commit last: a slug never points at a partially uploaded bundle.
        kv_put(config, cf, f"slug:{slug}", metadata)
    except Exception as error:
        # A timed-out write might have succeeded. Do not destroy its objects.
        raise BundleError(
            f"Publication could not be confirmed for /{slug}/. R2 prefix {prefix} "
            "was retained. Check share ls after KV propagation; if present, use "
            "share rm to remove it. Otherwise remove that exact prefix in R2."
        ) from error
    public_url = f"{config['urls']['public_base'].rstrip('/')}/{slug}/"
    try:
        if shutil.which("pbcopy"):
            subprocess.run(
                ["pbcopy"], input=public_url.encode(), check=False, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        pass  # Clipboard is optional, especially on remote machines.
    print(public_url)
