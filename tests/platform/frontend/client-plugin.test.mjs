import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, copyFile, readFile, stat, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

test("subscription namespaces accept readable names but reject path injection", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-subscription-name-"));
  const nativeFetch = globalThis.fetch;
  try {
    await copyFile(new URL("../../../backend/services/client_plugin/index.mjs", import.meta.url), join(dir, "index.mjs"));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(dir, "pricing.mjs"));
    const factory = (await import(pathToFileURL(join(dir, "index.mjs")))).default;
    for (const id of ["aigateway-subscription-edwin-agent-012345abcdef", "aigateway-subscription-研发-012345abcdef", "aigateway-test"]) {
      await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({ version: 1, providerId: id,
        name: "Subscription", accountId: "Edwin-Agent", subscriptionKey: "fixture-key", baseURL: "https://gateway.example/llm", models: {}, modelsPath: "/v1/models" }));
      const hooks = await factory();
      assert.equal(hooks.auth.provider, id);
      assert.equal(hooks.provider.id, id);
      const cfg = {};
      await hooks.config(cfg);
      assert.deepEqual(Object.keys(cfg.provider), [id]);
      if (id.includes("研发")) {
        let target, body;
        globalThis.fetch = async input => { target = input instanceof Request ? input.url : String(input);
          body = input instanceof Request ? await input.text() : undefined;
          return Response.json({ model: "fixture" }); };
        const loader = await hooks.auth.loader(async () => ({ type: "api", key: "fixture-key" }));
        const broken = "https://gateway.example/llmplugin://" + encodeURIComponent(id) + "/v1/responses";
        await loader.fetch(new Request(broken, { method: "POST", body: '{"model":"fixture"}' }));
        assert.equal(target, "https://gateway.example/llm/responses");
        assert.equal(body, '{"model":"fixture"}');
        await assert.rejects(loader.fetch(broken.replace("/v1/responses", "/v1/admin")), /outside/);
        await assert.rejects(loader.fetch(broken.replace(encodeURIComponent(id), "another-provider")), /outside/);
      }
    }
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({ version: 1,
      providerId: "aigateway-../../other", subscriptionKey: "fixture-key", baseURL: "https://gateway.example/llm" }));
    await assert.rejects(factory(), /Invalid AIGateway subscription configuration/);
  } finally { globalThis.fetch = nativeFetch; await rm(dir, { recursive: true, force: true }); }
});

test("private provider import, bounded dispatch and live model intersection", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-plugin-test-"));
  const nativeFetch = globalThis.fetch;
  try {
    await copyFile(new URL("../../../backend/services/client_plugin/index.mjs", import.meta.url), join(dir, "index.mjs"));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(dir, "pricing.mjs"));
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({
      version: 1, providerId: "aigateway-test", name: "Test", baseURL: "https://gateway.example.com/llm",
      subscriptionKey: "private-key", accountId: "Test subscription", modelsPath: "/v1/models",
      models: { a: { name: "A" }, b: { name: "B" } },
    }));
    const { default: plugin } = await import(pathToFileURL(join(dir, "index.mjs")));
    const hooks = await plugin();
    assert.deepEqual(hooks.auth.methods.map((method) => method.label), ["导入配置", "更新API Key"]);
    const config = {};
    await hooks.config(config);
    assert.deepEqual(Object.keys(config.provider["aigateway-test"].models), ["a", "b"]);
    const custom = { provider: { "aigateway-test": { name: "Keep" } } };
    await hooks.config(custom);
    assert.equal(custom.provider["aigateway-test"].name, "Keep");
    const authorization = await hooks.auth.methods[0].authorize();
    assert.equal(authorization.url, "");
    assert.equal((await authorization.callback()).key, "private-key");
    assert.equal((await authorization.callback()).accountId, "Test subscription");
    await assert.rejects(hooks.auth.loader(async () => null), /subscription rejected/);
    const loader = await hooks.auth.loader(async () => ({ type: "api", key: "rotated-key" }));
    const calls = [];
    const reply = new Response("data: first\n\ndata: [DONE]\n", { headers: { "content-type": "text/event-stream" } });
    globalThis.fetch = async (input, init) => { calls.push({ input, init }); return reply; };
    const signal = new AbortController().signal;
    assert.equal(await loader.fetch("https://gateway.example.com/llm/responses", {
      signal, body: "payload", headers: { authorization: "agent-key", "x-api-key": "agent-key", "x-user-id": "forged" },
    }), reply);
    assert.equal(calls[0].init.signal, signal);
    assert.equal(calls[0].init.body, "payload");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[0].init.headers.get("ocp-apim-subscription-key"), "rotated-key");
    assert.equal(calls[0].init.headers.get("authorization"), null);
    assert.equal(calls[0].init.headers.get("x-api-key"), null);
    assert.equal(calls[0].init.headers.get("x-user-id"), null);
    await loader.fetch("https://gateway.example.com/llm/messages", {});
    assert.equal(String(calls[1].input), "https://gateway.example.com/llm/v1/messages");
    for (const url of ["https://attacker.example.com/llm/responses", "https://gateway.example.com/llm-other/responses",
      "https://gateway.example.com/llm/admin", "http://gateway.example.com/llm/responses"]) {
      await assert.rejects(loader.fetch(url), /outside the configured gateway/);
    }
    const provider = { models: config.provider["aigateway-test"].models };
    const auth = { type: "api", key: "private-key" };
    globalThis.fetch = async () => Response.json({ data: [{ id: "a" }, { id: "new-not-in-snapshot" }] });
    assert.deepEqual(Object.keys(await hooks.provider.models(provider, { auth })), ["a"]);
    globalThis.fetch = async () => Response.json({ data: [] });
    assert.deepEqual(await hooks.provider.models(provider, { auth }), {});
    globalThis.fetch = async () => { throw new Error("offline"); };
    const offline = await hooks.provider.models(provider, { auth });
    assert.deepEqual(Object.keys(offline), Object.keys(provider.models));
    assert.equal(offline[Symbol.for("magpie.fellBack")], true);
    globalThis.fetch = async () => new Response("denied", { status: 403 });
    await assert.rejects(hooks.provider.models(provider, { auth }), (error) => error.signIn === "expired");
  } finally {
    globalThis.fetch = nativeFetch;
    await rm(dir, { recursive: true, force: true });
  }
});

test("magpie automatically imports once, preserves accounts and respects logout", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-auto-import-"));
  try {
    await copyFile(new URL("../../../backend/services/client_plugin/index.mjs", import.meta.url), join(dir, "index.mjs"));
    await copyFile(new URL("../../../backend/services/client_plugin/pricing.mjs", import.meta.url), join(dir, "pricing.mjs"));
    const config = {
      version: 1, providerId: "aigateway-test", name: "Test", baseURL: "https://gateway.example.com/llm",
      subscriptionKey: "bundled-key", accountId: "Subscription", modelsPath: "/v1/models", models: {},
    };
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify(config));
    const { default: plugin } = await import(pathToFileURL(join(dir, "index.mjs")));
    const home = await mkdtemp(join(dir, "home-"));
    const input = { directory: home, project: { id: "magpie" } };
    const authPath = join(home, "plugin-auth.json");
    const original = JSON.stringify({ copilot: { type: "api", key: "existing-other-provider" } });
    await writeFile(authPath, original);
    await Promise.all([plugin(input), plugin(input)]);
    let auth = JSON.parse(await readFile(authPath, "utf8"));
    assert.equal(auth.copilot.key, "existing-other-provider");
    assert.equal(auth[config.providerId].key, "bundled-key");
    assert.equal(auth[config.providerId].accountId, "Subscription");
    assert.equal((await stat(authPath)).mode & 0o777, 0o600);
    assert.equal(await readFile(authPath + ".bak", "utf8"), original);
    const marker = join(home, "aigateway-proxy-imports", config.providerId + ".json");
    assert.equal((await stat(marker)).mode & 0o777, 0o600);
    assert.equal((await readFile(marker, "utf8")).includes("bundled-key"), false);
    auth[config.providerId].key = "rotated-key";
    await writeFile(authPath, JSON.stringify(auth));
    await plugin(input);
    assert.equal(JSON.parse(await readFile(authPath, "utf8"))[config.providerId].key, "rotated-key");
    delete auth[config.providerId];
    await writeFile(authPath, JSON.stringify(auth));
    await plugin(input);
    assert.equal(JSON.parse(await readFile(authPath, "utf8"))[config.providerId], undefined);
    for (const suffix of ["", "#other"]) {
      const existing = await mkdtemp(join(dir, "existing-"));
      const data = JSON.stringify({ [config.providerId + suffix]: { type: "api", key: "keep-key" } });
      await writeFile(join(existing, "plugin-auth.json"), data);
      await plugin({ directory: existing, project: { id: "magpie" } });
      assert.equal(await readFile(join(existing, "plugin-auth.json"), "utf8"), data);
    }
    const corrupt = await mkdtemp(join(dir, "corrupt-"));
    await writeFile(join(corrupt, "plugin-auth.json"), "invalid-auth");
    const hooks = await plugin({ directory: corrupt, project: { id: "magpie" } });
    assert.equal(await readFile(join(corrupt, "plugin-auth.json"), "utf8"), "invalid-auth");
    assert.equal((await (await hooks.auth.methods[0].authorize()).callback()).key, "bundled-key");
    const otherHost = await mkdtemp(join(dir, "opencode-"));
    await plugin({ directory: otherHost, project: { id: "opencode" } });
    await assert.rejects(readFile(join(otherHost, "plugin-auth.json")), { code: "ENOENT" });
    const empty = await mkdtemp(join(dir, "new-"));
    await plugin({ directory: empty, project: { id: "magpie" } });
    assert.equal(JSON.parse(await readFile(join(empty, "plugin-auth.json"), "utf8"))[config.providerId].key, "bundled-key");
    const locked = await mkdtemp(join(dir, "locked-"));
    await writeFile(join(locked, "plugin-auth.json.lock"), "keep-lock");
    await plugin({ directory: locked, project: { id: "magpie" } });
    assert.equal(await readFile(join(locked, "plugin-auth.json.lock"), "utf8"), "keep-lock");
    await assert.rejects(readFile(join(locked, "plugin-auth.json")), { code: "ENOENT" });
    const failedWrite = await mkdtemp(join(dir, "write-failure-"));
    await writeFile(join(failedWrite, "plugin-auth.json"), original);
    await writeFile(join(failedWrite, "aigateway-proxy-imports"), "not-a-directory");
    await plugin({ directory: failedWrite, project: { id: "magpie" } });
    assert.equal(await readFile(join(failedWrite, "plugin-auth.json"), "utf8"), original);
    await assert.rejects(readFile(join(failedWrite, "plugin-auth.json.lock")), { code: "ENOENT" });
  } finally { await rm(dir, { recursive: true, force: true }); }
});
