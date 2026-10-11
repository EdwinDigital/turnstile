import { readFile } from "node:fs/promises";

export default async function AIGatewayProxyPlugin() {
  const cfg = JSON.parse(await readFile(new URL("./aigateway.config.json", import.meta.url), "utf8"));
  const base = new URL(cfg.baseURL);
  if (cfg.version !== 1 || base.protocol !== "https:" || base.username || base.password || base.search || base.hash)
    throw new Error("Invalid AIGateway configuration");
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
          type: "oauth", label: "导入下载配置 / Import downloaded configuration",
          async authorize() {
            return { url: "", method: "auto", instructions: "Import the subscription bundled with this private download.",
              async callback() {
                return { type: "success", key: cfg.subscriptionKey, accountId: cfg.accountId };
              } };
          },
        },
        { type: "api", label: "APIM subscription key (after rotation)" },
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
