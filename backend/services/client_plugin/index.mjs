import { mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { join } from "node:path";

async function durableWrite(path, content) {
  const temporary = `${path}.aigateway-${process.pid}-${crypto.randomUUID()}`;
  const file = await open(temporary, "wx", 0o600);
  try {
    await file.writeFile(content);
    await file.sync();
    await file.close();
    await rename(temporary, path);
  } finally {
    await file.close().catch(() => {});
    await unlink(temporary).catch(() => {});
  }
}

async function importOnce(input, cfg) {
  // Only magpie's documented config directory is eligible; OpenCode stays manual.
  if (input?.project?.id !== "magpie" || !input.directory) return;
  const authPath = join(input.directory, "plugin-auth.json");
  const markerDirectory = join(input.directory, "aigateway-proxy-imports");
  const marker = join(markerDirectory, `${cfg.providerId}.json`);
  let lock;
  try {
    try { await readFile(marker); return; }
    catch (error) { if (error.code !== "ENOENT") return; }
    await mkdir(input.directory, { recursive: true, mode: 0o700 });
    // Share magpie's auth lock, but never break an existing lock or replace unreadable auth.
    const deadline = Date.now() + 2000;
    while (!lock) {
      try { lock = await open(authPath + ".lock", "wx", 0o600); }
      catch (error) {
        if (error.code !== "EEXIST" || Date.now() >= deadline) return;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    }
    let previous;
    try { previous = await readFile(authPath, "utf8"); }
    catch (error) { if (error.code !== "ENOENT") return; }
    const accounts = previous === undefined ? {} : JSON.parse(previous);
    if (!accounts || typeof accounts !== "object" || Array.isArray(accounts)) return;
    const exists = Object.keys(accounts).some((id) => id === cfg.providerId || id.startsWith(cfg.providerId + "#"));
    await mkdir(markerDirectory, { recursive: true, mode: 0o700 });
    // Mark before importing: logout, reload, and a concurrent host must not resurrect the key.
    const mark = await open(marker, "wx", 0o600).catch((error) => {
      if (error.code === "EEXIST") return null;
      throw error;
    });
    if (!mark) return;
    try { await mark.writeFile(JSON.stringify({ version: 1, providerId: cfg.providerId })); await mark.sync(); }
    finally { await mark.close(); }
    if (exists) return;
    accounts[cfg.providerId] = { type: "api", key: cfg.subscriptionKey, accountId: cfg.accountId };
    if (previous !== undefined) await durableWrite(authPath + ".bak", previous);
    await durableWrite(authPath, JSON.stringify(accounts, null, 2) + "\n");
  } catch {
    // Auto-import is best effort; the manual import remains available without logging secrets.
  } finally {
    if (lock) { await lock.close(); await unlink(authPath + ".lock").catch(() => {}); }
  }
}

export default async function AIGatewayProxyPlugin(input) {
  const cfg = JSON.parse(await readFile(new URL("./aigateway.config.json", import.meta.url), "utf8"));
  const base = new URL(cfg.baseURL);
  if (cfg.version !== 1 || base.protocol !== "https:" || base.username || base.password || base.search || base.hash)
    throw new Error("Invalid AIGateway configuration");
  if (!/^aigateway-[a-z0-9-]+$/.test(cfg.providerId) || typeof cfg.subscriptionKey !== "string" || !cfg.subscriptionKey)
    throw new Error("Invalid AIGateway subscription configuration");
  await importOnce(input, cfg);
  const root = cfg.baseURL.replace(/\/$/, "");
  const endpoints = new Set(["/chat/completions", "/responses", "/messages", cfg.modelsPath]);
  const expired = () => Object.assign(new Error("AIGateway subscription rejected; check access or rotate the key"), { signIn: "expired" });
  const dispatch = async (key, input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin !== base.origin || !endpoints.has(url.pathname.slice(base.pathname.replace(/\/$/, "").length))
        || !url.pathname.startsWith(base.pathname.replace(/\/$/, "") + "/") || url.username || url.password)
      throw new Error("Refusing to send subscription credentials outside the configured gateway");
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers).forEach((value, name) => headers.set(name, value));
    for (const name of ["authorization", "x-api-key", "x-goog-api-key", "x-user-id", "x-user-email"])
      headers.delete(name);
    headers.set("Ocp-Apim-Subscription-Key", key);
    // The SDK appends /messages; Turnstile publishes Anthropic at /v1/messages.
    if (url.pathname === base.pathname.replace(/\/$/, "") + "/messages")
      url.pathname = base.pathname.replace(/\/$/, "") + "/v1/messages";
    const request = input instanceof Request ? new Request(url, input) : url;
    return fetch(request, { ...init, headers, redirect: "manual" });
  };
  return {
    config: async (config) => {
      config.provider ??= {};
      config.provider[cfg.providerId] ??= {
        name: cfg.name, npm: "@ai-sdk/openai-compatible", api: root, models: cfg.models,
      };
    },
    auth: {
      provider: cfg.providerId,
      methods: [
        {
          type: "oauth", label: "导入配置",
          async authorize() {
            return { url: "", method: "auto", instructions: "Import the subscription bundled with this private download.",
              async callback() {
                return { type: "success", key: cfg.subscriptionKey, accountId: cfg.accountId };
              } };
          },
        },
        { type: "api", label: "更新API Key" },
      ],
      async loader(getAuth) {
        const auth = await getAuth();
        if (auth?.type !== "api" || !auth.key) throw expired();
        return { baseURL: root, fetch: (input, init) => dispatch(auth.key, input, init) };
      },
    },
    provider: {
      id: cfg.providerId,
      async models(provider, { auth } = {}) {
        if (auth?.type !== "api") return provider.models;
        let response;
        try { response = await dispatch(auth.key, root + cfg.modelsPath); }
        catch { return provider.models; }
        if (response.status === 401 || response.status === 403) throw expired();
        if (!response.ok) return provider.models;
        try {
          const body = await response.json();
          if (!Array.isArray(body.data)) return provider.models;
          const allowed = new Set(body.data.map((model) => model.id));
          return Object.fromEntries(Object.entries(provider.models).filter(([id]) => allowed.has(id)));
        } catch { return provider.models; }
      },
    },
  };
}
