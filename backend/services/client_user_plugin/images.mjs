import { readFile, realpath, unlink, writeFile } from "node:fs/promises";
import { timingSafeEqual } from "node:crypto";
import { join } from "node:path";

export function localMagpieURL(address = "127.0.0.1:3425") {
  const url = new URL(address.includes("://") ? address : "http://" + address);
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/")
    throw new Error("Picture registration requires a loopback magpie gateway");
  return url.origin;
}

export async function ownLogin(settingsDirectory, providerId, userId) {
  const auth = JSON.parse(await readFile(join(settingsDirectory, "plugin-auth.json"), "utf8"));
  return Object.entries(auth).some(([id, value]) => (id === providerId || id.startsWith(providerId + "#"))
    && value?.type === "oauth" && value.uid === userId && value.access);
}

async function descriptor(path) {
  const data = JSON.parse(await readFile(path, "utf8"));
  if (!/^aigateway-user-[a-z0-9-]+$/.test(data.providerId) || !Array.isArray(data.images)
      || !data.capability?.startsWith("tsci_") || !data.nonce || !data.settingsDirectory)
    throw new Error("Invalid private picture bridge state");
  const origin = new URL(data.platformOrigin);
  if (origin.protocol !== "https:" && !(origin.protocol === "http:" && ["localhost", "127.0.0.1"].includes(origin.hostname)))
    throw new Error("Invalid picture platform origin");
  return data;
}

export async function pictureBridge(statePath, readyPath) {
  const initial = await descriptor(statePath);
  const http = localMagpieURL(initial.magpieAddress);
  const nonce = initial.nonce;
  const matchNonce = (value) => {
    const a = Buffer.from(value ?? ""); const b = Buffer.from("Bearer " + nonce);
    return a.length === b.length && timingSafeEqual(a, b);
  };
  const enabled = async (state) => {
    if (!await ownLogin(state.settingsDirectory, state.providerId, state.userId)) return false;
    const plugins = JSON.parse(await readFile(join(state.settingsDirectory, "plugins.json"), "utf8"));
    const canonical = await realpath(state.spec);
    for (const entry of plugins.plugins ?? []) {
      if (!entry.spec?.startsWith("/")) continue;
      if (await realpath(entry.spec).catch(() => "") === canonical) return !entry.off;
    }
    return false;
  };
  let stopping = false;
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, maxRequestBodySize: 4 * 1024 * 1024,
    async fetch(request) {
      if (!matchNonce(request.headers.get("authorization")) || request.headers.get("origin"))
        return Response.json({ error: "Unauthorized" }, { status: 401 });
      const path = new URL(request.url).pathname;
      if (path === "/health") return Response.json({ providerId: initial.providerId, bridgeVersion: 2, pid: process.pid });
      let state;
      try { state = await descriptor(statePath); if (!await enabled(state)) throw new Error(); }
      catch { return Response.json({ error: "The user plugin is disabled or signed out" }, { status: 403 }); }
      if (request.method === "POST" && path === "/mcp") {
        const rpc = await request.json().catch(() => null);
        if (!rpc || rpc.jsonrpc !== "2.0") return Response.json({ error: "Invalid JSON-RPC" }, { status: 400 });
        if (rpc.method.startsWith("notifications/")) return new Response(null, { status: 202 });
        const reply = (result) => Response.json({ jsonrpc: "2.0", id: rpc.id, result });
        if (rpc.method === "initialize") return reply({ protocolVersion: "2025-03-26", capabilities: { tools: { listChanged: false } }, serverInfo: { name: "aigateway-user-images", version: "0.2.0" } });
        if (rpc.method === "tools/list") return reply({ tools: [{ name: "generate_image", description: "Generate an image through the current AIGateway user subscription.", inputSchema: { type: "object", properties: { model: { type: "string", enum: state.images.map((m) => m.id) }, prompt: { type: "string" }, size: { type: "string" }, quality: { type: "string" } }, required: ["model", "prompt"], additionalProperties: false } }] });
        if (rpc.method !== "tools/call" || rpc.params?.name !== "generate_image") return Response.json({ jsonrpc: "2.0", id: rpc.id, error: { code: -32601, message: "Method not found" } });
        const args = rpc.params.arguments;
        if (!state.images.some((m) => m.id === args?.model) || typeof args?.prompt !== "string") return reply({ isError: true, content: [{ type: "text", text: "Image model and prompt are required" }] });
        const response = await fetch(state.platformOrigin + "/api/v1/client-runtime/images/generations", { method: "POST", redirect: "manual", headers: { Authorization: "Bearer " + state.capability, "Content-Type": "application/json" }, body: JSON.stringify(args), signal: request.signal });
        const data = await response.json().catch(() => null);
        if (!response.ok || !data?.data?.length) return reply({ isError: true, content: [{ type: "text", text: "Image generation failed; check platform access and quota" }] });
        return reply({ content: data.data.map((image) => image.b64_json ? { type: "image", data: image.b64_json, mimeType: "image/png" } : { type: "text", text: image.url ?? "Image data was unavailable" }), isError: false });
      }
      if (request.method === "GET" && path === "/v1/models") return Response.json({ object: "list", data: state.images.map((m) => ({ id: m.id, object: "model", owned_by: "aigateway" })) });
      if (request.method !== "POST" || path !== "/v1/images/generations") return Response.json({ error: "Images only" }, { status: 404 });
      let body;
      try { body = await request.json(); } catch { return Response.json({ error: "JSON required" }, { status: 400 }); }
      if (!state.images.some((m) => m.id === body?.model)) return Response.json({ error: "Image model not authorized" }, { status: 403 });
      try {
        const response = await fetch(state.platformOrigin + "/api/v1/client-runtime/images/generations", {
          method: "POST", redirect: "manual", headers: { Authorization: "Bearer " + state.capability, "Content-Type": "application/json" },
          body: JSON.stringify(body), signal: request.signal,
        });
        return new Response(response.body, { status: response.status, headers: { "content-type": response.headers.get("content-type") ?? "application/json", "cache-control": "no-store" } });
      } catch { return Response.json({ error: "The image gateway is unavailable" }, { status: 502 }); }
    },
  });
  await writeFile(readyPath, JSON.stringify({ port: server.port, pid: process.pid, nonce }), { mode: 0o600 });
  // The helper outlives an idle plugin host, but never an enabled, signed-in installation.
  const stop = async () => {
    if (stopping) return; stopping = true;
    try {
      const state = await descriptor(statePath);
      await fetch(state.platformOrigin + "/api/v1/client-auth/revoke", { method: "POST", headers: { Authorization: "Bearer " + state.capability }, signal: AbortSignal.timeout(2000) });
      await fetch(http + "/api/provider/remove", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: state.providerId + "-images" }), signal: AbortSignal.timeout(2000) });
    } catch { /* Never log private bridge state or credentials. */ }
    await unlink(readyPath).catch(() => {});
    server.stop(true); process.exit(0);
  };
  const timer = setInterval(async () => {
    try { if (!await enabled(await descriptor(statePath))) await stop(); } catch { await stop(); }
  }, 2000);
  timer.unref?.();
  process.on("SIGTERM", () => void stop());
  return server;
}

if (import.meta.main) await pictureBridge(process.argv[2], process.argv[3]);
