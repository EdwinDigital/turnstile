import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, copyFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { adaptModelResponse } from "../../../backend/services/client_user_plugin/index.mjs";

test("SSE aliases preserve split UTF-8, tools, usage and real model differences", async () => {
  const original = 'event: response.created\ndata: {"response":{"model":"gpt-6-astra","id":"r","text":"中文"}}\n\ndata: {"model":"other","call_id":"c","usage":{"output_tokens":2}}\n\ndata: [DONE]\n\n';
  const bytes = new TextEncoder().encode(original);
  const body = new ReadableStream({ start(controller) { for (const b of bytes) controller.enqueue(new Uint8Array([b])); controller.close(); } });
  const response = adaptModelResponse(new Response(body, { headers: { "content-type": "text/event-stream" } }), "astra-platform", "gpt-6-astra");
  const text = await response.text();
  assert.ok(text.includes('"model":"astra-platform"'));
  assert.ok(text.includes('"text":"中文"'));
  assert.ok(text.includes('"model":"other"'));
  assert.ok(text.includes('"call_id":"c"'));
  assert.ok(text.includes('"output_tokens":2'));
  assert.ok(text.includes("data: [DONE]"));
});

test("user plugin has OAuth only, dynamically replaces models and isolates credentials", async () => {
  const folder = await mkdtemp(join(tmpdir(), "ts-user-plugin-"));
  const previous = globalThis.fetch;
  try {
    await copyFile(new URL("../../../backend/services/client_user_plugin/index.mjs", import.meta.url), join(folder, "index.mjs"));
    await copyFile(new URL("../../../backend/services/client_user_plugin/images.mjs", import.meta.url), join(folder, "images.mjs"));
    await writeFile(join(folder, "aigateway-user.config.json"), JSON.stringify({ version: 2, platformOrigin: "https://platform.example", providerId: "aigateway-user-platform-user", userId: "uid", binding: "b", name: "User" }));
    const factory = (await import(pathToFileURL(join(folder, "index.mjs")).href)).default;
    const plugin = await factory({ directory: folder });
    assert.deepEqual(plugin.auth.methods.map((x) => x.label), ["登录平台", "导入模型配置", "重新登录"]);
    assert.ok(plugin.auth.methods.every((x) => x.type === "oauth"));
    let current = { new: { id: "new", provider: { npm: "@ai-sdk/openai", api: "https://platform.example/api/v1/client-runtime" }, tool_call: true, reasoning: true, modalities: { input: ["text", "image"] } } };
    let last;
    globalThis.fetch = async (url, init) => {
      last = { url: String(url), init };
      if (String(url).endsWith("/catalog")) return Response.json({ models: current, aliases: { new: "upstream" }, images: [] });
      if (String(url).endsWith("/usage")) return Response.json({ windows: [{ name: "audit", used: 100, aside: true }] });
      return Response.json({ model: "upstream", id: "response" });
    };
    const auth = { type: "oauth", access: "private-user-token" };
    const discovered = await plugin.provider.models({ models: { old: {} } }, { auth });
    assert.deepEqual(Object.keys(discovered), ["new"]);
    assert.equal(discovered.new.api.npm, "@ai-sdk/openai");
    assert.equal(discovered.new.api.url, "https://platform.example/api/v1/client-runtime");
    assert.equal(discovered.new.api.id, "new");
    assert.equal(discovered.new.capabilities.toolcall, true);
    assert.equal(discovered.new.capabilities.reasoning, true);
    assert.equal(discovered.new.capabilities.input.image, true);
    current = {};
    assert.deepEqual(await plugin.provider.models({ models: { old: {} } }, { auth }), {});
    const data = await plugin.auth.usage(async () => auth);
    assert.equal(data.windows[0].aside, true);
    const workingFetch = globalThis.fetch;
    globalThis.fetch = async () => Response.json({ error: "invalid_grant" }, { status: 400 });
    await assert.rejects(plugin.auth.refresh({ refresh: "expired-refresh" }), (error) => error.signIn === "expired");
    globalThis.fetch = workingFetch;
    const loader = await plugin.auth.loader(async () => auth);
    await assert.rejects(loader.fetch("https://attacker.example/responses"), /outside/);
    await loader.fetch(loader.baseURL + "/responses", { body: '{"model":"new"}', headers: { "x-user-id": "forged", "Ocp-Apim-Subscription-Key": "wrong", Authorization: "agent-key" } });
    assert.equal(last.init.headers.get("authorization"), "Bearer private-user-token");
    assert.equal(last.init.headers.get("x-user-id"), null);
    assert.equal(last.init.headers.get("ocp-apim-subscription-key"), null);
    assert.equal(last.init.redirect, "manual");
    const cached = await readFile(join(folder, "aigateway-proxy-user/aigateway-user-platform-user/catalog.json"), "utf8");
    assert.ok(!cached.includes("private-user-token"));
    assert.deepEqual(await plugin.provider.models({}, {}), {});
  } finally { globalThis.fetch = previous; await rm(folder, { recursive: true, force: true }); }
});
