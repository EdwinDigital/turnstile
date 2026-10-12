import { mkdir, open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { localMagpieURL } from "./images.mjs";

async function privateJSON(path, value) {
  const temporary = path + "." + randomUUID() + ".tmp";
  try {
    await writeFile(temporary, JSON.stringify(value), { mode: 0o600, flag: "wx" });
    await rename(temporary, path);
  } finally { await unlink(temporary).catch(() => {}); }
}

export function adaptModelResponse(response, alias, expected) {
  if (!alias || !expected || !response.ok || !response.body) return response;
  const replace = (value) => {
    if (value?.model === expected) value.model = alias;
    if (value?.response?.model === expected) value.response.model = alias;
    return value;
  };
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  const type = headers.get("content-type") ?? "";
  if (type.includes("text/event-stream")) {
    let pending = "";
    const decoder = new TextDecoder();
    const encoder = new TextEncoder();
    const consume = (controller, final = false) => {
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(pending))) {
        const frame = pending.slice(0, boundary.index);
        pending = pending.slice(boundary.index + boundary[0].length);
        const adapted = frame.split(/\r?\n/).map((line) => {
          if (!line.startsWith("data:")) return line;
          try { return "data: " + JSON.stringify(replace(JSON.parse(line.slice(5)))); }
          catch { return line; }
        }).join("\n");
        controller.enqueue(encoder.encode(adapted + "\n\n"));
      }
      if (pending.length > 4 * 1024 * 1024) throw new Error("Upstream SSE event exceeds the client limit");
      if (final && pending) controller.enqueue(encoder.encode(pending));
    };
    const stream = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) { pending += decoder.decode(chunk, { stream: true }); consume(controller); },
      flush(controller) { pending += decoder.decode(); consume(controller, true); },
    }));
    return new Response(stream, { status: response.status, statusText: response.statusText, headers });
  }
  if (!type.includes("application/json")) return response;
  const stream = new ReadableStream({
    async start(controller) {
      try {
        const reader = response.body.getReader();
        const chunks = []; let size = 0;
        for (;;) {
          const { value, done } = await reader.read(); if (done) break;
          size += value.length; if (size > 16 * 1024 * 1024) throw new Error("Upstream JSON exceeds the client limit");
          chunks.push(value);
        }
        const bytes = new Uint8Array(size); let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
        let result = bytes;
        try { result = new TextEncoder().encode(JSON.stringify(replace(JSON.parse(new TextDecoder().decode(bytes))))); }
        catch { /* Non-JSON errors keep their original bytes. */ }
        controller.enqueue(result); controller.close();
      } catch (error) { controller.error(error); }
    },
    cancel(reason) { return response.body.cancel(reason); },
  });
  return new Response(stream, { status: response.status, statusText: response.statusText, headers });
}

export default async function AIGatewayUserPlugin(input) {
  const cfg = JSON.parse(await readFile(new URL("./aigateway-user.config.json", import.meta.url), "utf8"));
  const origin = new URL(cfg.platformOrigin);
  const local = origin.protocol === "http:" && ["localhost", "127.0.0.1"].includes(origin.hostname);
  if (cfg.version !== 2 || (origin.protocol !== "https:" && !local) || origin.username || origin.password
      || origin.search || origin.hash || origin.pathname !== "/" || !/^aigateway-user-[a-z0-9-]+$/.test(cfg.providerId))
    throw new Error("Invalid user plugin bootstrap");
  const base = origin.origin;
  const directory = join(input.directory ?? fileURLToPath(new URL(".", import.meta.url)), "aigateway-proxy-user", cfg.providerId);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const installPath = join(directory, "installation.json");
  let installation;
  try { installation = JSON.parse(await readFile(installPath, "utf8")).id; }
  catch (error) {
    if (error.code !== "ENOENT") throw new Error("User plugin installation state cannot be read");
    installation = randomUUID();
    try {
      const f = await open(installPath, "wx", 0o600);
      try { await f.writeFile(JSON.stringify({ id: installation })); } finally { await f.close(); }
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      installation = JSON.parse(await readFile(installPath, "utf8")).id;
    }
  }
  const runtime = base + "/api/v1/client-runtime";
  let cached = { models: {}, aliases: {}, images: [] };
  const cachePath = join(directory, "catalog.json");
  try { cached = JSON.parse(await readFile(cachePath, "utf8")); } catch { /* Display cache is optional. */ }
  const expired = () => Object.assign(new Error("请重新登录 AIGateway 平台"), { signIn: "expired" });
  const call = async (path, body, access) => {
    const response = await fetch(base + path, {
      method: body === undefined ? "GET" : "POST", redirect: "manual",
      headers: { "Content-Type": "application/json", ...(access ? { Authorization: "Bearer " + access } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(20_000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 || (path === "/api/v1/client-auth/token" && data.error === "invalid_grant")) throw expired();
      throw Object.assign(new Error(typeof data.error === "string" ? data.error : typeof data.detail === "string" ? data.detail : "平台请求未能完成"), { status: response.status });
    }
    return data;
  };
  const method = (label, purpose) => ({
    type: "oauth", label,
    async authorize() {
      const device = await call("/api/v1/client-auth/device-authorizations", {
        user_id: cfg.userId, binding: cfg.binding, installation_id: installation, purpose,
      });
      const verify = new URL(device.verification_uri);
      if (verify.origin !== base) throw new Error("Refusing an external platform sign-in URL");
      return { url: verify.href, method: "auto", instructions: `在平台确认当前账号和设备。Code: ${device.user_code}`,
        async callback() {
          const deadline = Date.now() + Math.min(device.expires_in, 300) * 1000;
          let wait = Math.max(5, device.interval);
          while (Date.now() < deadline) {
            await new Promise((resolve) => setTimeout(resolve, wait * 1000));
            try {
              const data = await call("/api/v1/client-auth/token", { grant_type: "device_code", device_code: device.device_code });
              return { type: "success", access: data.access_token, refresh: data.refresh_token,
                expires: Date.now() + data.expires_in * 1000, accountId: cfg.userId, uid: cfg.userId };
            } catch (error) {
              if (error.message === "authorization_pending") continue;
              if (error.message === "slow_down") { wait = Math.min(wait + 5, 30); continue; }
              throw error;
            }
          }
          return { type: "failed", error: "登录验证已超时，请重新开始" };
        },
      };
    },
  });
  const syncImages = async (images, access) => {
    if (input.project?.id !== "magpie" || typeof Bun === "undefined") return;
    const magpie = localMagpieURL(process.env.MAGPIE_ADDR);
    const imageId = cfg.providerId + "-images";
    const ownership = join(directory, "images-owned.json");
    const statePath = join(directory, "images-runtime.json");
    const readyPath = join(directory, "images-ready.json");
    const existing = await fetch(magpie + "/api/providers", { signal: AbortSignal.timeout(5000) })
      .then((r) => r.ok ? r.json() : null).catch(() => null);
    const present = existing?.providers?.some((p) => p.id === imageId);
    let owned = false;
    try { owned = JSON.parse(await readFile(ownership, "utf8")).providerId === imageId; } catch { /* Only our own manifest can grant replacement. */ }
    if (present && !owned) throw new Error("The picture provider ID is already owned by another configuration");
    if (!images.length) {
      if (owned) await fetch(magpie + "/api/provider/remove", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: imageId }) });
      await unlink(statePath).catch(() => {});
      return;
    }
    const capability = await call("/api/v1/client-runtime/image-capabilities", {}, access);
    let ready;
    try {
      ready = JSON.parse(await readFile(readyPath, "utf8"));
      if (!Number.isInteger(ready.port) || ready.port < 1 || ready.port > 65535) throw new Error();
      const live = await fetch(`http://127.0.0.1:${ready.port}/health`, { headers: { Authorization: "Bearer " + ready.nonce }, signal: AbortSignal.timeout(2000) }).then((r) => r.json());
      if (live.providerId !== cfg.providerId) throw new Error();
      if (live.bridgeVersion !== 2) {
        process.kill(ready.pid, "SIGTERM");
        for (let i = 0; i < 50; i++) {
          try { process.kill(ready.pid, 0); await new Promise((r) => setTimeout(r, 100)); }
          catch { break; }
        }
        ready = undefined;
      }
    } catch { ready = undefined; }
    const nonce = ready?.nonce ?? randomUUID() + randomUUID();
    await privateJSON(ownership, { providerId: imageId });
    await privateJSON(statePath, { providerId: cfg.providerId, userId: cfg.userId, images, capability: capability.key,
      nonce, platformOrigin: base, settingsDirectory: input.directory, magpieAddress: process.env.MAGPIE_ADDR ?? "127.0.0.1:3425",
      spec: fileURLToPath(new URL(".", import.meta.url)).replace(/\/$/, "") });
    if (!ready) {
      await unlink(readyPath).catch(() => {});
      const helper = Bun.spawn([process.execPath, "run", fileURLToPath(new URL("./images.mjs", import.meta.url)), statePath, readyPath], { stdin: "ignore", stdout: "ignore", stderr: "ignore" });
      helper.unref();
      for (let i = 0; i < 50; i++) {
        try { ready = JSON.parse(await readFile(readyPath, "utf8")); break; } catch { await new Promise((r) => setTimeout(r, 100)); }
      }
      if (!ready) throw new Error("The private image helper could not start");
    }
    const picture = { id: imageId, name: cfg.name.replace(/^AIGateway User/, "AIGateway User Images"),
      key: nonce, chat: `http://127.0.0.1:${ready.port}/v1`, models: images.map((m) => m.id), new: false };
    await privateJSON(join(directory, "images-provider.json"), picture);
    await privateJSON(join(directory, "images-mcp.json"), { mcpServers: { [imageId]: {
      url: `http://127.0.0.1:${ready.port}/mcp`, headers: { Authorization: "Bearer " + nonce },
    } } });
    // Desktop magpie does not expose GUI mutation APIs on its inference port.
    // A compatible GUI API can register directly; otherwise the owned MCP file is used.
    if (existing) {
      const saved = await fetch(magpie + "/api/provider/save", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(picture), signal: AbortSignal.timeout(10_000) });
      if (!saved.ok) throw new Error("The picture provider could not be registered");
    }
  };
  return {
    config: async (config) => {
      config.provider ??= {};
      config.provider[cfg.providerId] ??= { name: cfg.name, npm: "@ai-sdk/openai-compatible", api: runtime, models: cached.models };
    },
    auth: {
      provider: cfg.providerId,
      methods: [method("登录平台", "login"), method("导入模型配置", "import"), method("重新登录", "reauth")],
      refreshLead: 60_000,
      async refresh(auth) {
        const hex = createHash("sha256").update(auth.refresh).digest("hex").slice(0, 32);
        const requestId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
        const data = await call("/api/v1/client-auth/token", { grant_type: "refresh_token", refresh_token: auth.refresh, request_id: requestId });
        // Renew the image-only capability without ever sharing the main refresh token.
        // A local companion failure must not discard a successfully rotated OAuth token.
        if (cached.images.length) await syncImages(cached.images, data.access_token).catch(() => {});
        return { access: data.access_token, refresh: data.refresh_token, expires: Date.now() + data.expires_in * 1000 };
      },
      async usage(getAuth) {
        const auth = await getAuth(); if (auth?.type !== "oauth" || !auth.access) throw expired();
        return call("/api/v1/client-runtime/usage", undefined, auth.access);
      },
      async loader(getAuth) {
        const auth = await getAuth(); if (auth?.type !== "oauth" || !auth.access) throw expired();
        return { baseURL: runtime, async fetch(input, init = {}) {
          const url = new URL(input instanceof Request ? input.url : String(input));
          const allowed = ["/responses", "/chat/completions", "/messages", "/v1/messages", "/models", "/v1/models"];
          const path = url.pathname.slice(new URL(runtime).pathname.length);
          if (url.origin !== base || !url.pathname.startsWith(new URL(runtime).pathname + "/")
              || !allowed.includes(path) || url.username || url.password || url.search || url.hash)
            throw new Error("Refusing credentials outside the user runtime");
          const current = await getAuth(); if (current?.type !== "oauth" || !current.access) throw expired();
          const headers = new Headers(input instanceof Request ? input.headers : undefined);
          new Headers(init.headers).forEach((v, k) => headers.set(k, v));
          for (const k of ["authorization", "ocp-apim-subscription-key", "x-api-key", "x-user-id", "x-user-email", "x-turnstile-client-context"])
            headers.delete(k);
          headers.set("Authorization", "Bearer " + current.access);
          let model;
          try { model = input instanceof Request ? (await input.clone().json()).model : JSON.parse(init.body).model; } catch { /* Discovery has no body. */ }
          const response = await fetch(input, { ...init, headers, redirect: "manual" });
          return adaptModelResponse(response, model, cached.aliases?.[model]);
        } };
      },
    },
    provider: { id: cfg.providerId, async models(provider, { auth } = {}) {
      if (auth?.type !== "oauth" || !auth.access) return {};
      const data = await call("/api/v1/client-runtime/catalog", undefined, auth.access);
      cached = { models: data.models ?? {}, aliases: data.aliases ?? {}, images: data.images ?? [] };
      await privateJSON(cachePath, cached);
      await syncImages(cached.images, auth.access);
      return cached.models;
    } },
  };
}
