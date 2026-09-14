// Run from worker/: npm test (Node.js 22.16+; no external dependencies).
import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.ts";

function file(slug, visibility) {
	return {
		name: `${slug}.pdf`, size: 4, content_type: "application/pdf",
		uploaded_at: "2025-01-01T00:00:00Z", downloads: 0,
		r2_key: `2025-01-01/${slug}/${slug}.pdf`, slug, public: visibility,
	};
}

function link(slug, visibility) {
	return {
		type: "link", url: `https://example.com/${slug}`, slug,
		created_at: "2025-01-01T00:00:00Z", clicks: 0, public: visibility,
	};
}

function environment(entries, wrapped = false) {
	const values = new Map(entries.map((entry) => {
		const value = JSON.stringify(entry);
		return [`slug:${entry.slug}`, wrapped
			? JSON.stringify({ metadata: {}, value }) : value];
	}));
	const writes = [];
	const env = {
		SITE_NAME: "share.example.com",
		KV: {
			async list({ prefix }) {
				return { keys: [...values.keys()]
					.filter((name) => name.startsWith(prefix))
					.map((name) => ({ name })), list_complete: true };
			},
			async get(key) { return values.get(key) ?? null; },
			async put(key, value) { writes.push(key); values.set(key, value); },
		},
		R2: { async get() { assert.fail("Unexpected R2 read"); } },
	};
	return { env, values, writes };
}

function request(env, path = "/api/stats") {
	// Call the real Worker entry point; no live network requests or credentials.
	return worker.fetch(new Request(`https://${env.SITE_NAME}${path}`), env);
}

for (const [kind, make] of [["file", file], ["link", link]]) {
	for (const wrapped of [false, true]) {
		test(`stats only lists explicitly public ${kind}s (${wrapped ? "SDK-wrapped" : "raw"} KV)`, async () => {
			const entries = [
				make("listed", true), make("unlisted", false), make("legacy"),
				make("null-visibility", null), make("string-true", "true"),
				make("numeric-true", 1),
			];
			const { env, values, writes } = environment(entries, wrapped);
			const before = new Map(values);
			const response = await request(env);

			assert.equal(response.status, 200);
			assert.equal(response.headers.get("Content-Type"), "application/json");
			assert.equal(response.headers.get("Access-Control-Allow-Origin"), "*");
			assert.deepEqual(await response.json(), [entries[0]]);
			// Filtering the response must not remove private entries from KV.
			assert.deepEqual(values, before);
			assert.deepEqual(writes, []);
		});
	}
}

test("stats returns an empty array when no entries are public", async () => {
	const { env } = environment([
		file("unlisted-file", false), file("legacy-file"),
		link("unlisted-link", false), link("legacy-link"),
	]);
	const response = await request(env);
	assert.equal(response.status, 200);
	assert.deepEqual(await response.json(), []);
});

test("stats handles an empty namespace and disables response storage", async () => {
	const { env } = environment([]);
	const response = await request(env);
	assert.equal(response.status, 200);
	assert.deepEqual(await response.json(), []);
	assert.equal(response.headers.get("Cache-Control"), "no-store");
});

test("stats preserves public metadata and file ordering", async () => {
	const older = file("older", true);
	const newer = { ...file("newer", true), uploaded_at: "2025-01-02T00:00:00Z" };
	const { env } = environment([older, file("unlisted", false), newer]);
	const response = await request(env);
	assert.equal(response.headers.get("Cache-Control"), "no-store");
	assert.deepEqual(await response.json(), [newer, older]);
});

test("an unlisted file is still accessible by its known slug", async () => {
	const entry = file("unlisted-file", false);
	const { env, writes } = environment([entry]);
	env.R2.get = async (key) => {
		assert.equal(key, entry.r2_key);
		return { body: "test", size: 4 };
	};
	const response = await request(env, `/${entry.slug}`);
	assert.equal(response.status, 200);
	assert.equal(response.headers.get("Content-Type"), "application/pdf");
	assert.equal(await response.text(), "test");
	assert.deepEqual(writes, [`slug:${entry.slug}`]);
});

test("an unlisted link still redirects when its slug is known", async () => {
	const entry = link("unlisted-link", false);
	const { env, writes } = environment([entry]);
	const response = await request(env, `/${entry.slug}`);
	assert.equal(response.status, 301);
	assert.equal(response.headers.get("Location"), entry.url);
	assert.deepEqual(writes, [`slug:${entry.slug}`]);
});

test("the landing page still displays public entries only", async () => {
	const { env } = environment([
		file("listed-file", true), file("hidden-file", false), file("legacy-file"),
		link("listed-link", true), link("hidden-link", false), link("legacy-link"),
	]);
	const response = await request(env, "/");
	assert.equal(response.status, 200);
	const html = await response.text();
	assert.ok(html.includes('href="/listed-file"'));
	assert.ok(html.includes('href="/listed-link"'));
	for (const slug of ["hidden-file", "legacy-file", "hidden-link", "legacy-link"]) {
		assert.ok(!html.includes(slug));
	}
});
