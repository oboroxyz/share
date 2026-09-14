import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.ts";

const prefix = `__bundles__/${"a".repeat(32)}/`;
const slug = "bundle-test";
const origin = "https://share.example";

class MemoryR2 {
  objects = new Map();
  gets = [];
  set(key, content, contentType = "application/octet-stream") {
    this.objects.set(key, { bytes: Buffer.from(content), contentType });
  }
  async get(key, options) {
    this.gets.push(key);
    const file = this.objects.get(key);
    if (!file) return null;
    const range = options?.range;
    const bytes = range ? file.bytes.subarray(range.offset, range.offset + range.length) : file.bytes;
    return {
      size: file.bytes.length,
      body: bytes,
      range,
      httpMetadata: { contentType: file.contentType },
      json: async () => JSON.parse(file.bytes.toString()),
    };
  }
  async head(key) {
    const file = this.objects.get(key);
    return file ? { size: file.bytes.length } : null;
  }
}

class MemoryKV {
  values = new Map();
  writes = [];
  async get(key) { return this.values.get(key) ?? null; }
  async put(key, value) { this.writes.push(key); this.values.set(key, value); }
  async list({ prefix }) {
    return { keys: [...this.values.keys()].filter((key) => key.startsWith(prefix)).map((name) => ({ name })), list_complete: true };
  }
}

function fixture(files = {
  "index.html": ["<h1>Report</h1>", "text/html"],
  "assets/style.css": ["body{}", "text/css"],
  "assets/app.js": ["console.log('ok')", "text/javascript"],
  "report.pdf": ["0123456789", "application/pdf"],
}, entry = Object.hasOwn(files, "index.html") ? "index.html" : null) {
  const env = { SITE_NAME: "share.example", R2: new MemoryR2(), KV: new MemoryKV() };
  const manifest = {
    version: 1, entry,
    files: Object.entries(files).map(([path, [body, content_type]]) => ({ path, content_type, size: Buffer.byteLength(body) })),
  };
  for (const [path, [body, type]] of Object.entries(files)) env.R2.set(prefix + path, body, type);
  env.R2.set(prefix + ".manifest.json", JSON.stringify(manifest), "application/json");
  const meta = {
    type: "bundle", slug, name: "Test bundle", public: false,
    r2_prefix: prefix, size: manifest.files.reduce((sum, file) => sum + file.size, 0),
    file_count: manifest.files.length, uploaded_at: "2026-01-01T00:00:00Z", downloads: 0,
  };
  env.KV.values.set(`slug:${slug}`, JSON.stringify(meta));
  const request = (path = `/${slug}/`, init) => worker.fetch(new Request(origin + path, init), env);
  return { env, meta, manifest, request };
}

test("bundle root redirects to trailing slash, retaining the query", async () => {
  const { request } = fixture();
  const response = await request(`/${slug}?page=2`);
  assert.equal(response.status, 308);
  assert.equal(response.headers.get("location"), `${origin}/${slug}/?page=2`);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("root index and relative assets render without KV writes", async () => {
  const { env, request } = fixture();
  const response = await request();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type"), /^text\/html/);
  assert.equal(await response.text(), "<h1>Report</h1>");
  const css = await request(`/${slug}/assets/style.css`);
  assert.equal(await css.text(), "body{}");
  assert.equal(css.headers.get("content-type"), "text/css; charset=utf-8");
  const js = await request(`/${slug}/assets/app.js`);
  assert.equal(js.headers.get("content-type"), "text/javascript; charset=utf-8");
  assert.deepEqual(env.KV.writes, []);
});

test("HEAD has headers but no body and does not increment counters", async () => {
  const { env, request } = fixture();
  const response = await request(`/${slug}/report.pdf`, { method: "HEAD", headers: { Range: "bytes=1-2" } });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-length"), "10");
  assert.equal(response.headers.get("content-type"), "application/pdf");
  assert.equal(await response.text(), "");
  assert.deepEqual(env.KV.writes, []);
});

for (const [range, body, contentRange] of [
  ["bytes=2-4", "234", "bytes 2-4/10"],
  ["bytes=7-", "789", "bytes 7-9/10"],
  ["bytes=-3", "789", "bytes 7-9/10"],
  ["bytes=8-999", "89", "bytes 8-9/10"],
  ["bytes=-99", "0123456789", "bytes 0-9/10"],
]) {
  test(`PDF range ${range}`, async () => {
    const response = await fixture().request(`/${slug}/report.pdf`, { headers: { Range: range } });
    assert.equal(response.status, 206);
    assert.equal(await response.text(), body);
    assert.equal(response.headers.get("content-range"), contentRange);
    assert.equal(response.headers.get("content-length"), String(body.length));
    assert.match(response.headers.get("content-disposition"), /^inline;/);
  });
}

for (const range of ["bytes=99-", "bytes=5-2", "bytes=-0", "bytes=-", "bytes=0-1,4-5", "garbage", "bytes=999999999999999999999-"]) {
  test(`invalid/unsupported range ${range} returns 416`, async () => {
    const response = await fixture().request(`/${slug}/report.pdf`, { headers: { Range: range } });
    assert.equal(response.status, 416);
    assert.equal(response.headers.get("content-range"), "bytes */10");
  });
}

test("If-Range conservatively sends the full representation", async () => {
  const response = await fixture().request(`/${slug}/report.pdf`, { headers: { Range: "bytes=1-2", "If-Range": '"old"' } });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "0123456789");
});

test("zero-byte asset works, but a range is unsatisfiable", async () => {
  const { request } = fixture({ "empty.txt": ["", "text/plain"] });
  assert.equal(await (await request(`/${slug}/empty.txt`)).text(), "");
  assert.equal((await request(`/${slug}/empty.txt`, { headers: { Range: "bytes=0-" } })).status, 416);
});

test("custom nested entry redirects so its relative assets resolve correctly", async () => {
  const { request } = fixture({
    "pages/report.html": ["<h1>Nested</h1>", "text/html"],
    "pages/style.css": ["h1{}", "text/css"],
  }, "pages/report.html");
  const response = await request();
  const target = response.headers.get("location");
  assert.equal(target, `${origin}/${slug}/pages/report.html`);
  const cssPath = new URL("style.css", target).pathname;
  assert.equal(await (await request(cssPath)).text(), "h1{}");
});

test("no index generates an escaped directory listing, not a bucket listing", async () => {
  const { env, request } = fixture({
    'a & "file".txt': ["text", "text/plain"],
    "pictures/猫.png": ["image", "image/png"],
  });
  env.R2.set(prefix + "not-in-manifest.txt", "do not list");
  const html = await (await request()).text();
  assert.match(html, /a &amp; &quot;file&quot;\.txt/);
  assert.match(html, /pictures\//);
  assert.doesNotMatch(html, /not-in-manifest/);
  assert.equal((await request(`/${slug}/pictures`)).status, 308);
  const nested = await (await request(`/${slug}/pictures/`)).text();
  assert.match(nested, /%E7%8C%AB\.png/);
  assert.match(nested, /\.\.\//);
  const image = await request(`/${slug}/pictures/${encodeURIComponent("猫.png")}`);
  assert.equal(await image.text(), "image");
});

test("nested directory index is served", async () => {
  const { request } = fixture({ "docs/index.html": ["docs", "text/html"] });
  assert.equal((await request(`/${slug}/docs`)).status, 308);
  assert.equal(await (await request(`/${slug}/docs/`)).text(), "docs");
});

test("directory listing escapes an attacker-controlled title", async () => {
  const { env, meta, request } = fixture({ "a.txt": ["a", "text/plain"] });
  meta.name = '<img src=x onerror="alert(1)">';
  env.KV.values.set(`slug:${slug}`, JSON.stringify(meta));
  const html = await (await request()).text();
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img/);
});

test("missing assets do not get SPA fallback or direct-prefix fallback", async () => {
  const { env, request } = fixture();
  env.R2.set(prefix + "secret.txt", "not in manifest");
  for (const path of ["missing.js", "secret.txt", "assets-nope/", "index.html/"]) {
    assert.equal((await request(`/${slug}/${path}`)).status, 404);
  }
});

for (const path of ["%", "%2e%2e%2fsecret", "assets%2fstyle.css", "%5csecret", ".env", ".manifest.json", "assets//style.css", "assets/%00x"]) {
  test(`rejects unsafe path ${path}`, async () => {
    const { request } = fixture();
    assert.equal((await request(`/${slug}/${path}`)).status, 400);
  });
}

test("double-encoded traversal is not decoded a second time", async () => {
  const { env, request } = fixture();
  assert.equal((await request(`/${slug}/%252e%252e/secret`)).status, 404);
  assert.ok(env.R2.gets.every((key) => !key.includes("../")));
});

test("internal R2 bundle keys cannot be read through the legacy fallback", async () => {
  const { env, request } = fixture();
  for (const path of [prefix + "index.html", prefix + ".manifest.json"]) {
    assert.equal((await request("/" + path)).status, 404);
  }
  assert.deepEqual(env.R2.gets, []);
});

test("deleted manifest revokes all routes even with a stale KV entry", async () => {
  const { env, request } = fixture();
  env.R2.objects.delete(prefix + ".manifest.json");
  for (const path of [`/${slug}`, `/${slug}/`, `/${slug}/assets/style.css`]) {
    const response = await request(path);
    assert.equal(response.status, 404);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
  assert.deepEqual(env.KV.writes, []);
});

test("HEAD checks that the underlying file still exists", async () => {
  const { env, request } = fixture();
  env.R2.objects.delete(prefix + "report.pdf");
  assert.equal((await request(`/${slug}/report.pdf`, { method: "HEAD" })).status, 404);
  assert.equal((await request(`/${slug}/report.pdf`)).status, 404);
});

test("preview responses avoid caching, referrer leakage, indexing, and service workers", async () => {
  const response = await fixture().request();
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.match(response.headers.get("x-robots-tag"), /noindex/);
  assert.equal(response.headers.get("content-security-policy"), "worker-src 'none'");
});

test("POST is rejected", async () => {
  const response = await fixture().request(undefined, { method: "POST" });
  assert.equal(response.status, 405);
  assert.equal(response.headers.get("allow"), "GET, HEAD");
});

test("SDK-wrapped metadata is accepted", async () => {
  const { env, meta, request } = fixture();
  env.KV.values.set(`slug:${slug}`, JSON.stringify({ metadata: {}, value: JSON.stringify(meta) }));
  assert.equal((await request()).status, 200);
});

test("invalid manifests fail closed", async () => {
  for (const value of ["not-json", JSON.stringify({ version: 1, entry: null, files: [{ path: "../secret", size: 1, content_type: "text/plain" }] }), JSON.stringify({ version: 2, files: [] })]) {
    const { env, request } = fixture();
    env.R2.set(prefix + ".manifest.json", value);
    assert.equal((await request()).status, 502);
  }
});

test("unlisted file, link, and bundle entries do not leak via stats or landing page", async () => {
  const { env, meta, request } = fixture();
  env.KV.values.set("slug:private-file", JSON.stringify({ name: "secret-file", slug: "private-file", r2_key: "hidden", size: 1, downloads: 0 }));
  env.KV.values.set("slug:private-link", JSON.stringify({ type: "link", slug: "private-link", url: "https://secret.example", clicks: 0 }));
  const stats = await (await request("/api/stats")).json();
  assert.deepEqual(stats, []);
  const html = await (await request("/")).text();
  for (const text of [slug, meta.name, "secret-file", "secret.example"]) assert.ok(!html.includes(text));
  meta.public = true;
  env.KV.values.set(`slug:${slug}`, JSON.stringify(meta));
  assert.equal((await (await request("/api/stats")).json()).length, 1);
  const publicHTML = await (await request("/")).text();
  assert.ok(publicHTML.includes(`/${slug}/`));
  assert.ok(publicHTML.includes(`${origin}/${slug}/`));
  assert.ok(!publicHTML.includes("icecube.to/"));
});

test("legacy single-file serving and download counts still work", async () => {
  const { env, request } = fixture();
  env.R2.set("legacy/report.pdf", "pdf-bytes", "application/pdf");
  env.KV.values.set("slug:legacy", JSON.stringify({ slug: "legacy", name: "report.pdf", r2_key: "legacy/report.pdf", size: 9, content_type: "application/pdf", downloads: 0 }));
  const response = await request("/legacy");
  assert.equal(await response.text(), "pdf-bytes");
  assert.match(response.headers.get("content-disposition"), /^inline;/);
  assert.equal(JSON.parse(env.KV.values.get("slug:legacy")).downloads, 1);
});

test("legacy link redirect and direct R2 URLs are preserved", async () => {
  const { env, request } = fixture();
  env.KV.values.set("slug:link", JSON.stringify({ type: "link", slug: "link", url: "https://example.com", clicks: 0 }));
  assert.equal((await request("/link")).headers.get("location"), "https://example.com/");
  env.R2.set("2026-01-01/old.txt", "legacy");
  assert.equal(await (await request("/2026-01-01/old.txt")).text(), "legacy");
});
