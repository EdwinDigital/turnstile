import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, copyFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

test("private provider import, bounded dispatch and live model intersection", async () => {
  const dir = await mkdtemp(join(tmpdir(), "aigateway-plugin-test-"));
  const nativeFetch = globalThis.fetch;
  try {
    await copyFile(new URL("../../../backend/services/client_plugin/index.mjs", import.meta.url), join(dir, "index.mjs"));
    await writeFile(join(dir, "aigateway.config.json"), JSON.stringify({
      version: 1, providerId: "aigateway-test", name: "Test", baseURL: "https://gateway.example.com/llm",
      subscriptionKey: "private-key", accountId: "Test subscription", modelsPath: "/v1/models",
      models: { a: { name: "A" }, b: { name: "B" } },
    }));
    const { default: plugin } = await import(pathToFileURL(join(dir, "index.mjs")));
    const hooks = await plugin();
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
    assert.equal(await hooks.provider.models(provider, { auth }), provider.models);
    globalThis.fetch = async () => new Response("denied", { status: 403 });
    await assert.rejects(hooks.provider.models(provider, { auth }), (error) => error.signIn === "expired");
  } finally {
    globalThis.fetch = nativeFetch;
    await rm(dir, { recursive: true, force: true });
  }
});
