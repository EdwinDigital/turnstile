import { mkdir, open, readFile, unlink } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { localMagpieURL } from "./images.mjs";
import { adaptModelResponse, importBootstrap, privateJSON, syncAccountProfile } from "./helpers.mjs";

export default async function AIGatewayUserPlugin(input) {
  const cfg = JSON.parse(await readFile(new URL("./aigateway-user.config.json", import.meta.url), "utf8"));
  const origin = new URL(cfg.platformOrigin);
  const local = origin.protocol === "http:" && ["localhost", "127.0.0.1"].includes(origin.hostname);
  if (![2, 3, 4].includes(cfg.version) || (origin.protocol !== "https:" && !local) || origin.username || origin.password
      || origin.search || origin.hash || origin.pathname !== "/" || !/^aigateway-user-[a-z0-9-]+$/.test(cfg.providerId))
    throw new Error("Invalid user plugin bootstrap");
  const base = origin.origin;
  const directory = join(input.directory ?? fileURLToPath(new URL(".", import.meta.url)), "aigateway-proxy-user", cfg.providerId);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const bootstrapped = await importBootstrap(input, cfg);
  const installPath = join(directory, "installation.json");
  if (bootstrapped) await privateJSON(installPath, { id: cfg.installationId });
  let installation;
  try { installation = JSON.parse(await readFile(installPath, "utf8")).id; }
  catch (error) {
    if (error.code !== "ENOENT") throw new Error("User plugin installation state cannot be read");
    installation = bootstrapped ? cfg.installationId : randomUUID();
    try {
      const f = await open(installPath, "wx", 0o600);
      try { await f.writeFile(JSON.stringify({ id: installation })); } finally { await f.close(); }
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      installation = JSON.parse(await readFile(installPath, "utf8")).id;
    }
  }
  const runtime = base + "/api/v1/client-runtime";
  let cached = (bootstrapped ? cfg.catalog : null) ?? { models: {}, aliases: {}, images: [] };
  let hasCatalog = bootstrapped && Boolean(cfg.catalog);
  const cachePath = join(directory, "catalog.json");
  if (!bootstrapped) {
    try { cached = JSON.parse(await readFile(cachePath, "utf8")); hasCatalog = true; } catch { /* Display cache is optional. */ }
  }
  if (bootstrapped) await privateJSON(cachePath, cached);
  let userName = cached.user?.name ?? cfg.userName;
  if (cached.user) await syncAccountProfile(input, cfg, cached.user);
  else if (cfg.userName) await syncAccountProfile(input, cfg, { id: cfg.userId, name: cfg.userName });
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
              const latest = await updateCatalog(data.access_token, true);
              await privateJSON(join(directory, "profile.json"), { userId: cfg.userId, selection: latest.selection });
              return { type: "success", access: data.access_token, refresh: data.refresh_token,
                expires: Date.now() + data.expires_in * 1000, accountId: userName ?? cfg.userId, uid: cfg.userId,
                installationId: installation };
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
      if (owned) await fetch(magpie + "/api/provider/remove", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: imageId }), signal: AbortSignal.timeout(2000) }).catch(() => {});
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
  const updateCatalog = async (access, fresh = false) => {
    const data = await call(fresh ? "/api/v1/client-runtime/refresh-models" : "/api/v1/client-runtime/catalog", fresh ? {} : undefined, access);
    if (data.user) {
      await syncAccountProfile(input, cfg, data.user);
      userName = data.user.name;
    }
    const next = { user: data.user, models: data.models ?? {}, aliases: data.aliases ?? {}, images: data.images ?? [], selection: data.selection ?? null };
    // Keep a usable previous catalog if fetching or persistence fails.
    await privateJSON(cachePath, next);
    cached = next;
    hasCatalog = true;
    await syncImages(cached.images, access).catch(() => {});
    return data;
  };
  const refreshAuthorization = async (auth) => {
    const hex = createHash("sha256").update(auth.refresh).digest("hex").slice(0, 32);
    const requestId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    const data = await call("/api/v1/client-auth/token", { grant_type: "refresh_token", refresh_token: auth.refresh, request_id: requestId });
    return { ...auth, access: data.access_token, refresh: data.refresh_token, expires: Date.now() + data.expires_in * 1000 };
  };
  const refreshModels = {
    type: "oauth", label: "刷新模型",
    async authorize() {
      return { url: "", method: "auto", instructions: "从平台读取当前绑定订阅的最新模型，不重新登录或切换订阅。",
        async callback() {
          if (input.project?.id !== "magpie" || !input.directory) throw expired();
          const accounts = JSON.parse(await readFile(join(input.directory, "plugin-auth.json"), "utf8"));
          let auth = Object.entries(accounts).find(([id, value]) =>
            (id === cfg.providerId || id.startsWith(cfg.providerId + "#")) && value.type === "oauth"
            && (value.uid === cfg.userId || value.accountId === cfg.userId))?.[1];
          if (!auth?.access || !auth.refresh) throw expired();
          if (auth.expires <= Date.now() + 60_000) {
            auth = await refreshAuthorization(auth);
            // Commit rotation before a catalog failure can leave the host with an invalid token.
            await input.client.auth.set({ path: { id: cfg.providerId }, body: auth });
          }
          await updateCatalog(auth.access, true);
          return { ...auth, type: "success", accountId: userName ?? cfg.userId, uid: cfg.userId };
        },
      };
    },
  };
  return {
    config: async (config) => {
      config.provider ??= {};
      config.provider[cfg.providerId] ??= { name: cfg.name, npm: "@ai-sdk/openai-compatible", api: runtime, models: cached.models };
    },
    auth: {
      provider: cfg.providerId,
      methods: [refreshModels, method("重新登录", "reauth")],
      refreshLead: 60_000,
      async refresh(auth) {
        const next = await refreshAuthorization(auth);
        // Renew the image-only capability without ever sharing the main refresh token.
        // A local companion failure must not discard a successfully rotated OAuth token.
        if (cached.images.length) await syncImages(cached.images, next.access).catch(() => {});
        return { access: next.access, refresh: next.refresh, expires: next.expires };
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
      if (!hasCatalog) await updateCatalog(auth.access);
      await syncImages(cached.images, auth.access);
      // config uses Config.Model, but provider.models returns normalized Model.Info.
      return Object.fromEntries(Object.entries(cached.models).map(([id, model]) => [id, {
        id, providerID: cfg.providerId, name: model.name ?? id,
        api: { id, url: model.provider?.api ?? runtime, npm: model.provider?.npm ?? "@ai-sdk/openai-compatible" },
        limit: model.limit ?? { context: 32768, output: 8192 },
        options: {}, headers: {}, variants: {},
        capabilities: {
          temperature: true, toolcall: model.tool_call === true, reasoning: model.reasoning === true,
          input: { text: true, image: model.modalities?.input?.includes("image") === true },
          output: { text: true },
        },
      }]));
    } },
  };
}
