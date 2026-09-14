/** Static bundles: immutable R2 snapshot + manifest, with one KV entry per share. */
interface BundleMeta {
	slug: string;
	name: string;
	r2_prefix?: string;
}
interface BundleFile {
	path: string;
	size: number;
	content_type: string;
}
interface Manifest {
	version: number;
	entry: string | null;
	files: BundleFile[];
}

export function escapeHTML(value: string): string {
	return value.replace(/[&<>"']/g, (char) => ({
		"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
	})[char]!);
}

function safePath(path: string): boolean {
	return !!path && !/[\\\x00-\x1f\x7f]/.test(path) &&
		new TextEncoder().encode(path).length <= 900 &&
		path.split("/").every((part) => !!part && !part.startsWith("."));
}

function validManifest(value: unknown): value is Manifest {
	if (!value || typeof value !== "object") return false;
	const m = value as Manifest;
	if (m.version !== 1 || !Array.isArray(m.files) || !m.files.length || m.files.length > 1000) return false;
	const paths = new Set<string>();
	let size = 0;
	for (const file of m.files) {
		if (!file || typeof file.path !== "string" || !safePath(file.path) || paths.has(file.path) ||
			!Number.isSafeInteger(file.size) || file.size < 0 || typeof file.content_type !== "string" ||
			!/^[\w!#$&^_.+-]+\/[\w!#$&^_.+-]+$/.test(file.content_type)) return false;
		paths.add(file.path);
		size += file.size;
	}
	return size <= 100 * 1024 * 1024 && (m.entry === null || paths.has(m.entry));
}

function responseHeaders(): Headers {
	return new Headers({
		"Cache-Control": "no-store",
		"Referrer-Policy": "no-referrer",
		"X-Robots-Tag": "noindex, nofollow, noarchive",
		"X-Content-Type-Options": "nosniff",
		// Previews must not leave a service worker behind after deletion.
		"Content-Security-Policy": "worker-src 'none'",
	});
}

function errorResponse(status: number, message: string, request: Request): Response {
	return new Response(request.method === "HEAD" ? null : message, { status, headers: responseHeaders() });
}

function encodedPath(path: string): string {
	return path.split("/").map(encodeURIComponent).join("/");
}

function redirect(request: Request, pathname: string): Response {
	const url = new URL(request.url);
	url.pathname = pathname;
	const headers = responseHeaders();
	headers.set("Location", url.toString());
	return new Response(null, { status: 308, headers });
}

function listing(meta: BundleMeta, manifest: Manifest, directory: string): string {
	const children = new Map<string, boolean>();
	for (const file of manifest.files) {
		if (!file.path.startsWith(directory)) continue;
		const tail = file.path.slice(directory.length);
		const [name, ...rest] = tail.split("/");
		children.set(name, rest.length > 0 || children.get(name) === true);
	}
	const base = `/${encodeURIComponent(meta.slug)}/`;
	const links = [...children].sort(([a], [b]) => a.localeCompare(b)).map(([name, isDirectory]) => {
		const path = directory + name + (isDirectory ? "/" : "");
		return `<li><a href="${escapeHTML(base + encodedPath(path))}">${escapeHTML(name)}${isDirectory ? "/" : ""}</a></li>`;
	}).join("\n");
	const parent = directory ? directory.slice(0, -1).split("/").slice(0, -1).join("/") : "";
	const up = directory ? `<p><a href="${escapeHTML(base + encodedPath(parent) + (parent ? "/" : ""))}">../</a></p>` : "";
	const title = escapeHTML(directory || meta.name);
	return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title>
<style>body{font:16px system-ui,sans-serif;max-width:60rem;margin:3rem auto;padding:0 1rem}li{margin:.6rem 0;overflow-wrap:anywhere}</style>
</head><body><h1>${title}</h1>${up}<ul>${links}</ul></body></html>`;
}

/** One bytes range (including suffix/open-ended); false means unsatisfiable. */
function byteRange(header: string, size: number): { offset: number; length: number } | false {
	const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
	if (!match || (!match[1] && !match[2]) || size === 0) return false;
	const first = match[1] ? Number(match[1]) : null;
	const last = match[2] ? Number(match[2]) : null;
	if ((first !== null && !Number.isSafeInteger(first)) ||
		(last !== null && !Number.isSafeInteger(last))) return false;
	if (first === null) {
		if (!last) return false;
		const length = Math.min(last, size);
		return { offset: size - length, length };
	}
	const end = Math.min(last ?? size - 1, size - 1);
	if (first >= size || end < first) return false;
	return { offset: first, length: end - first + 1 };
}

async function serveAsset(file: BundleFile, prefix: string, request: Request, r2: R2Bucket): Promise<Response> {
	const headers = responseHeaders();
	const ct = file.content_type;
	const text = ct.startsWith("text/") || /json|xml|javascript/.test(ct);
	headers.set("Content-Type", text ? `${ct}; charset=utf-8` : ct);
	headers.set("Accept-Ranges", "bytes");
	const name = file.path.split("/").pop()!;
	const ascii = name.replace(/[^\x20-\x7e]|["\\]/g, "_");
	const inline = text || /^(image|video|audio|font)\//.test(ct) || ct === "application/pdf" || ct === "application/wasm";
	headers.set("Content-Disposition", `${inline ? "inline" : "attachment"}; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(name).replace(/['()*]/g, (char) => "%" + char.charCodeAt(0).toString(16).toUpperCase())}`);
	const key = prefix + file.path;
	if (request.method === "HEAD") {
		const object = await r2.head(key);
		if (!object) return errorResponse(404, "Not found", request);
		headers.set("Content-Length", object.size.toString());
		return new Response(null, { headers });
	}
	// If-Range cannot be evaluated from a manifest. Send a full response instead.
	const rangeHeader = request.headers.has("If-Range") ? null : request.headers.get("Range");
	const range = rangeHeader ? byteRange(rangeHeader, file.size) : undefined;
	if (range === false) {
		headers.set("Content-Range", `bytes */${file.size}`);
		return new Response(null, { status: 416, headers });
	}
	const object = await r2.get(key, range ? { range } : undefined);
	if (!object) return errorResponse(404, "Not found", request);
	if (object.size !== file.size) return errorResponse(502, "Bundle object was modified", request);
	headers.set("Content-Length", (range ? range.length : object.size).toString());
	if (range) headers.set("Content-Range", `bytes ${range.offset}-${range.offset + range.length - 1}/${file.size}`);
	return new Response(object.body, { status: range ? 206 : 200, headers });
}

export async function serveBundle(meta: BundleMeta, request: Request, env: { R2: R2Bucket }): Promise<Response> {
	if (request.method !== "GET" && request.method !== "HEAD") {
		const response = errorResponse(405, "Method not allowed", request);
		response.headers.set("Allow", "GET, HEAD");
		return response;
	}
	const prefix = meta.r2_prefix;
	if (!prefix || !/^__bundles__\/[0-9a-f]{32}\/$/.test(prefix)) return errorResponse(404, "Not found", request);

	// The manifest is a revocation gate in strongly consistent R2. Even a stale
	// KV slug must fail after rm removes it. Do not cache it or write hit counts
	// back into KV (a racing hit could resurrect a deleted entry).
	const object = await env.R2.get(prefix + ".manifest.json");
	if (!object) return errorResponse(404, "Not found", request);
	if (object.size > 1024 * 1024) return errorResponse(502, "Invalid bundle manifest", request);
	let manifest: unknown;
	try { manifest = await object.json(); }
	catch { return errorResponse(502, "Invalid bundle manifest", request); }
	if (!validManifest(manifest)) return errorResponse(502, "Invalid bundle manifest", request);

	const url = new URL(request.url);
	const base = `/${encodeURIComponent(meta.slug)}`;
	if (url.pathname === base) return redirect(request, base + "/");
	if (!url.pathname.startsWith(base + "/")) return errorResponse(404, "Not found", request);
	const rawPath = url.pathname.slice(base.length + 1);
	const trailing = rawPath.endsWith("/");
	let path: string;
	try {
		const parts = (trailing ? rawPath.slice(0, -1) : rawPath).split("/").map(decodeURIComponent);
		if (parts.some((part) => part.includes("/"))) return errorResponse(400, "Invalid path", request);
		path = parts.join("/");
	} catch { return errorResponse(400, "Invalid path", request); }
	if (path && !safePath(path)) return errorResponse(400, "Invalid path", request);
	if (rawPath && !path) return errorResponse(400, "Invalid path", request);

	if (!path && manifest.entry && manifest.entry !== "index.html") {
		return redirect(request, base + "/" + encodedPath(manifest.entry));
	}
	const isDirectory = !path || trailing;
	const directory = path ? path + "/" : "";
	const candidate = isDirectory ? directory + "index.html" : path;
	const file = manifest.files.find((entry) => entry.path === candidate);
	if (file) return serveAsset(file, prefix, request, env.R2);
	const children = manifest.files.some((entry) => entry.path.startsWith(directory));
	if (path && !children) return errorResponse(404, "Not found", request);
	if (!isDirectory) return redirect(request, base + "/" + encodedPath(directory));
	const headers = responseHeaders();
	headers.set("Content-Type", "text/html; charset=utf-8");
	return new Response(request.method === "HEAD" ? null : listing(meta, manifest, directory), { headers });
}
