# Multi-file bundles

`share bundle <directory>` publishes related files as one immutable share. It
works for static HTML output, PDF/image collections, coverage reports, and other
review artifacts. It does not run a server-side application or build the files.

```bash
# Inspect exactly which paths will be included. No credentials/network needed.
share bundle ./dist --dry-run

# Upload the finished output; prints one URL, ending in /.
share bundle ./dist

# Other examples
share bundle ./screenshots
share bundle ./report --entry pages/report.html
share bundle ./report --exclude '*.map' --exclude 'raw-data/*'

# Remove the whole shared copy, not the original directory.
share rm <slug>
share rm https://yourdomain.com/<slug>/
```

## CLI options

| Option | Behavior |
| --- | --- |
| `--entry PATH` | Included file relative to the root. Defaults to `index.html`, otherwise a directory listing. |
| `--name NAME` | Display name; defaults to the directory name. |
| `--slug SLUG` | Explicit URL slug. Omit for a cryptographically random 128-bit slug. Existing slugs are not overwritten. |
| `--public` | Include the bundle on the landing page and public stats API. Default is unlisted. |
| `--exclude GLOB` | Exclude a case-sensitive, root-relative glob. Repeatable; excluded directories are not traversed. |
| `--dry-run` | Show included and excluded paths, byte count, and entry point without uploading. |

The existing `~/.config/share/config.toml` is reused. No additional account,
credentials, bucket, KV namespace, or R2 public domain is required. Clipboard
copying is optional on macOS; a missing/unavailable clipboard does not fail a
remote upload. The successful command prints the URL to stdout; exclusion
warnings go to stderr.

The first implementation supports macOS/Linux and limits each bundle to
**1,000 files and 100 MiB total**, with a maximum 1 MiB manifest and 900-byte
UTF-8 relative paths. Empty directories are not retained. Files are staged in a
temporary local snapshot before uploading, so allow disk space for a second
copy. Finish the build first: files changed during scanning/staging are rejected.

## Browser behavior

```text
report/
  index.html
  assets/style.css
  assets/chart.png
  attachment.pdf

/<slug>/                    -> index.html
/<slug>/assets/style.css     -> assets/style.css
/<slug>/attachment.pdf       -> PDF (inline)
```

A missing trailing slash on a directory redirects to its canonical URL; query
strings are preserved. Subdirectories serve their own `index.html` when present,
otherwise a small file listing. A custom entry other than the root `index.html`
redirects to the entry's real path, preserving the meaning of relative assets.
Files that are not in the manifest return 404; there is no SPA fallback or
cross-bundle file resolution. Root listings without `index.html` intentionally
list all included files in that bundle to anyone who has its URL.

**Use relative asset URLs.** A reference such as `/assets/app.js` points at the
hosting domain's root, not at the bundle. For example, build a Vite project with
`base: './'` (or its equivalent `vite build --base=./`). No HTML/CSS/JS rewriting
is performed. History-mode SPA routing, server rendering, APIs, and automatic
ZIP downloads are not included.

PDFs/media support GET, HEAD, and a single byte range (including suffix and
open-ended ranges). Multiple ranges are not implemented. If-Range requests
conservatively receive the full representation. Other HTTP methods receive 405.

## Privacy and trust boundary

Unlisted is **not authentication**. Anyone who receives or copies a bundle URL
can access every included file. Do not upload secrets or sensitive artifacts.
Use `--dry-run` on a dedicated output directory, rather than a repository root.

The scanner always skips dotfiles/dot-directories, `node_modules`, `__pycache__`,
symlinks, common credential/private-key filenames, and `.pem`, `.key`, `.p12`,
`.pfx`, and `.keystore` files. It rejects special files and unsafe paths and uses
no-follow file/directory descriptors during staging to reject symlink swaps.
These are accident-prevention measures, **not a comprehensive secret scanner**.
Ordinary HTML, screenshots, JSON, source maps, and database files can still
contain sensitive information. Add explicit exclusions or sanitize them first.

Unlike `share upload`, **bundle files are uploaded byte-for-byte**. The
`upload.strip_metadata` setting is not applied: changing images/media could
break hashed assets, integrity checks, or animations. Strip sensitive metadata
before building a bundle when needed.

HTML and JavaScript execute on the sharing domain. This is for trusted output,
not an isolation sandbox for arbitrary third-party HTML. Bundles block web and
service workers (`worker-src 'none'`), but can still execute scripts and make
network requests. Features requiring web workers will not work. Use a separate
origin with an appropriate sandbox/authentication design for untrusted output.

Bundle responses use `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
`X-Robots-Tag: noindex, nofollow, noarchive`, and `X-Content-Type-Options: nosniff`.
These reduce caching/indexing/referrer leakage; they do not stop recipients from
saving files, scripts from sending data, or non-compliant clients retaining it.

The public `/api/stats` endpoint now returns **only `public: true` entries**,
including for existing single-file/link shares. Previously that endpoint exposed
unlisted metadata. The landing-page copy button also uses the configured domain
and preserves the bundle's trailing slash.

Existing single-file/link automatic slugs are unchanged (time-derived Sqids).
The stronger random-slug default described here applies to the new bundle
command, not retroactively to existing shares.

## Storage and lifecycle

One KV entry per share:

```json
{
  "type": "bundle",
  "slug": "<random slug>",
  "r2_prefix": "__bundles__/<independent random id>/",
  "name": "report",
  "file_count": 3,
  "size": 12345,
  "entry": "index.html",
  "uploaded_at": "<ISO timestamp>",
  "downloads": 0,
  "public": false
}
```

R2 stores the original paths under that unique prefix plus a private
`.manifest.json` containing version, entry point, paths, MIME types, and sizes.
The Worker blocks direct access to the internal prefix, and only serves
manifest-listed paths. Keep both the R2 development URL and bucket custom
domains disabled; a public bucket would bypass Worker controls.

Publishing uploads all objects and the manifest **before** writing the KV slug.
Each upload is an independent snapshot; rerunning does not update an old URL.
The CLI checks custom-slug collisions, but KV provides no atomic reservation:
do not concurrently publish the same explicit custom slug. Random defaults
avoid that coordination requirement for normal use.

Deleting removes the R2 manifest first, then paginates and deletes objects under
that exact validated prefix, then deletes the KV entry. The Worker checks the
manifest on every request, so stale KV metadata cannot keep serving a normally
deleted bundle. A failed object deletion leaves KV metadata for a retry of
`share rm`, but the manifest gate is already closed. Original local files and
unrelated/shared-prefix objects are not deleted. Requests already in flight or
copies already saved by a recipient cannot be recalled.

Per-request bundle counters are deliberately not written to KV: they would add
writes for every asset and could race deletion. `share ls` labels bundles with
their file count and shows a dash for downloads. The legacy single-file/link
counters keep their existing behavior. There is no automatic expiry, revision
replacement, or "review completed" detection in this feature.

### Failure recovery

An upload failure before the KV write triggers best-effort removal of that new
prefix. If cleanup itself fails, the exact prefix is printed for manual cleanup.
A killed process or interrupted multipart transfer can leave orphaned objects
or multipart uploads; there is no background garbage collector yet.

A KV write timeout is ambiguous: it may have succeeded. In that case the CLI
retains the fully uploaded objects and prints the slug/prefix rather than
risking destroying a published share. After KV propagation, inspect `share ls`:
if the share is present, use `share rm`; otherwise remove only the printed prefix
in the R2 dashboard. Consult R2 multipart-upload cleanup for unfinished transfers.

KV is eventually consistent, so a newly published URL can briefly return 404.
R2 provides the strongly consistent manifest gate, not instantaneous KV
publication. See Cloudflare's [KV consistency documentation](https://developers.cloudflare.com/kv/concepts/how-kv-works/)
and [R2 consistency documentation](https://developers.cloudflare.com/r2/reference/consistency/).

## Upgrade and test

**Both the CLI and Worker must be updated** for bundle support. Preserve your
existing `worker/wrangler.toml` bindings/routes and CLI config. No migration is
needed for existing file/link entries. Deploy the Worker before using the new
CLI command. An older Worker cannot serve bundle entries; rollbacks must take
existing bundles into account (remove them with the new CLI first).

Use a separate Worker and test R2/KV resources for initial end-to-end validation.
This branch does not deploy Cloudflare resources or include Cloudflare secrets.

```bash
# Repository root; Python >= 3.12 with project dependencies installed
uv sync --dev
PYTHONPATH=src uv run python -m unittest discover -s tests -v
uv run ruff check
uv run ruff format --check

# Worker tests use Node's built-in test runner; Node >= 22.6
cd worker
npm test
```

The automated tests use in-memory R2/KV fakes and temporary filesystem fixtures,
not production Cloudflare resources. Before production use, smoke-test an
HTML/CSS/image bundle, a PDF Range request, and delete-then-open behavior on the
actual Worker. Verify that `/api/stats` omits an unlisted bundle and that an
internal `__bundles__/...` path cannot be served.
