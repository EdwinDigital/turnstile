import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath, pathToFileURL } from "node:url"
import vm from "node:vm"

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(pathToFileURL(require.resolve("rolldown")).href)

async function compile(path) {
  const entry = fileURLToPath(new URL(`../../../frontend/src/${path}`, import.meta.url))
  const bundle = await rolldown({ input: entry, external: id => id !== entry, treeshake: false,
    transform: { jsx: { runtime: "automatic" } },
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const authCode = await compile("api/auth.ts")
const apiCode = await compile("api/client-downloads.ts")
const pageCode = await compile("pages/client-downloads-page.tsx")
const item = { application_id: "app-uuid", subscription_id: "subscription-uuid",
  gateway_id: "gateway-uuid", application_name: "Other application", subscription_name: "Edwin-Agent", models: [] }

function load(code, require, globals = {}) {
  const module = { exports: {} }
  const resolve = name => name === "../components/magpie-client-download"
    ? { MagpieClientDownload: "magpie-client-download" }
    : name === "../components/ui/badge" ? { Badge: "badge" } : require(name)
  vm.runInNewContext(code, { exports: module.exports, module, require: resolve, AbortController, Error, ...globals })
  return module.exports
}

const authHelpers = load(authCode, () => ({ apiUrl: path => path, ApiError: Error }))

for (const locale of ["en", "ja", "ko"]) {
  test(`download layout and action labels are localized in ${locale}`, async () => {
    const { CLIENT_USER_PHRASES: phrases } = await import(new URL(`../../../frontend/src/locales/${locale}/phrases-client-user.ts`, import.meta.url))
    for (const label of ["当前用户", "平台默认订阅", "安装与使用", "文生图接入", "下载订阅版插件", "下载 macOS 客户端", "其他版本"]) {
      assert.ok(phrases[label], label)
      assert.notEqual(phrases[label], label)
    }
  })
}

function api(disposition) {
  const calls = []
  const exported = load(apiCode, () => ({ apiUrl: path => path, request: () => {}, ApiError: Error }), {
    fetch: async (url, init) => {
      calls.push({ url, init })
      return new Response("zip bytes", { headers: { "content-type": "application/zip",
        ...(disposition ? { "content-disposition": disposition } : {}),
      } })
    },
  })
  return { downloads: exported.clientDownloadsApi, calls }
}

for (const [userMode, prefix, label] of [
  [true, "aigateway-proxy-user", "Edwin"], [false, "aigateway-proxy", "Edwin-Agent"],
]) {
  const filename = `${prefix}-${label}.zip`
  test(`${prefix} API preserves the display-name header and authenticated POST`, async () => {
    const view = api(`attachment; filename="${filename}"; filename*=UTF-8''${filename}`)
    const signal = new AbortController().signal
    const result = userMode ? await view.downloads.downloadUser(signal)
      : await view.downloads.download(item, "secondary", signal)
    assert.equal(result.filename, filename)
    assert.equal(await result.blob.text(), "zip bytes")
    assert.equal(view.calls[0].init.signal, signal)
    assert.equal(view.calls[0].init.credentials, "include")
    assert.equal(view.calls[0].init.cache, "no-store")
    assert.equal(view.calls[0].init.headers["Content-Type"], "application/json")
    assert.deepEqual(JSON.parse(view.calls[0].init.body), userMode ? { acknowledged: true }
      : { application_id: item.application_id, subscription_id: item.subscription_id,
        gateway_id: item.gateway_id, key_kind: "secondary" })
  })

  test(`${prefix} page saves the API filename instead of any internal ID`, async () => {
    const view = api(`attachment; filename="${filename}"`)
    const anchors = [], revoked = []
    let stateIndex = 0
    const jsx = (type, props) => ({ type, props })
    const page = load(pageCode, name => {
      if (name === "react") return { useEffect: () => {}, useRef: value => ({ current: value }),
        useState: value => [([2, 3].includes(stateIndex++) ? true : value), () => {}] }
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" }
      if (name === "@tanstack/react-query") return { useQuery: options => ({ data:
        options.queryKey[0] === "client-downloads" ? { items: [item], key_management_available: true }
          : { enabled: true, user_name: "Edwin" },
      }) }
      if (name === "../api/client-downloads") return { clientDownloadsApi: view.downloads }
      if (name === "../api/auth") return authHelpers
      if (name === "../providers/auth-provider") return { useAuth: () => ({ user: { id: "user-uuid", role: "owner" } }) }
      if (name === "../components/ui/button") return { Button: "button" }
      if (name === "lucide-react") return { Download: "icon", PanelLeft: "icon", RefreshCw: "icon", ShieldCheck: "icon" }
      throw new Error(`Unexpected import ${name}`)
    }, {
      document: { body: { append: node => anchors.push(node) }, createElement: () => ({ click() {}, remove() {} }) },
      URL: { createObjectURL: () => "blob:zip", revokeObjectURL: url => revoked.push(url) },
      window: { setTimeout: callback => callback() },
    })
    const nodes = []
    function visit(node) {
      if (Array.isArray(node)) { node.forEach(visit); return }
      if (!node || typeof node !== "object") return
      nodes.push(node); visit(node.props?.children)
    }
    visit(page.ClientDownloadsPage({ onToggleSidebar: () => {} }))
    assert.ok(nodes.some(node => node.type === "magpie-client-download"))
    assert.ok(nodes.some(node => node.type === "p" && node.props.children ===
      "解压目录与 ZIP 文件名一致（不含 .zip）。"))
    assert.ok(nodes.some(node => node.type === "p" && node.props.children ===
      "使用指定应用订阅与密钥，导入已授权模型。"))
    assert.equal(nodes.filter(node => node.type === "h1").length, 1)
    assert.equal(nodes.some(node => node.type === "h2" && node.props.children === "AIGateway-Proxy"), false)
    assert.ok(nodes.some(node => node.type === "dl" && node.props.className === "client-downloads-facts"))
    assert.equal(nodes.filter(node => node.props?.className === "client-downloads-footer").length, 2)
    if (userMode) {
      nodes.find(node => node.type === "button" && Array.isArray(node.props.children)
        && node.props.children.includes("下载用户版插件")).props.onClick()
    } else {
      nodes.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
    }
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(anchors.length, 1)
    assert.equal(anchors[0].download, filename)
    assert.deepEqual(revoked, ["blob:zip"])
  })
}

test("RFC 5987 UTF-8 filenames take precedence over ASCII", async () => {
  const filename = "aigateway-proxy-研发团队.zip"
  const view = api(`attachment; filename="aigateway-proxy-download.zip"; filename*=UTF-8''${encodeURIComponent(filename)}`)
  assert.equal((await view.downloads.download(item, "primary")).filename, filename)
})

test("malformed UTF-8 falls back to a safe ASCII header", async () => {
  const view = api('attachment; filename="aigateway-proxy-Edwin-Agent.zip"; filename*=UTF-8\'\'%broken')
  assert.equal((await view.downloads.download(item, "primary")).filename, "aigateway-proxy-Edwin-Agent.zip")
})

for (const header of [undefined, 'attachment; filename="../../danger.zip"',
  'attachment; filename="aigateway-proxy-../danger.zip"', 'attachment; filename="aigateway-proxy-file.exe"',
  `attachment; filename*=UTF-8''${encodeURIComponent("aigateway-proxy-a\u202eb.zip")}`,
]) {
  test(`missing or unsafe filename uses a generic non-ID fallback: ${header}`, async () => {
    const view = api(header)
    assert.equal((await view.downloads.download(item, "primary")).filename, "aigateway-proxy-subscription.zip")
    assert.equal((await view.downloads.downloadUser()).filename, "aigateway-proxy-user-user.zip")
  })
}

for (const permissions of [[], ["user-plugin"], ["subscription-plugin"], ["user-plugin", "subscription-plugin"]]) {
  test(`download cards and queries follow independent permissions: ${permissions.join(",") || "none"}`, () => {
    const user = { id: "member", role: "member", download_permissions: permissions }
    const queries = [], jsx = (type, props) => ({ type, props })
    const page = load(pageCode, name => {
      if (name === "react") return { useEffect: () => {}, useRef: value => ({ current: value }),
        useState: value => [value, () => {}] }
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" }
      if (name === "@tanstack/react-query") return { useQuery: options => {
        queries.push(options)
        return { data: options.queryKey[0] === "client-downloads"
          ? { items: [item], key_management_available: true } : { enabled: true, user_name: "Member" } }
      } }
      if (name === "../api/client-downloads") return { clientDownloadsApi: {} }
      if (name === "../api/auth") return authHelpers
      if (name === "../providers/auth-provider") return { useAuth: () => ({ user }) }
      if (name === "../components/ui/button") return { Button: "button" }
      if (name === "lucide-react") return { Download: "icon", PanelLeft: "icon", RefreshCw: "icon", ShieldCheck: "icon" }
      throw new Error(`Unexpected import ${name}`)
    })
    const nodes = []
    function visit(node) {
      if (Array.isArray(node)) { node.forEach(visit); return }
      if (!node || typeof node !== "object") return
      nodes.push(node); visit(node.props?.children)
    }
    visit(page.ClientDownloadsPage({ onToggleSidebar: () => {} }))
    const titles = nodes.filter(n => n.type === "h2").map(n => n.props.children)
    assert.equal(titles.includes("AIGateway-Proxy 用户版"), permissions.includes("user-plugin"))
    assert.equal(titles.includes("AIGateway-Proxy 订阅版"), permissions.includes("subscription-plugin"))
    assert.equal(queries.find(q => q.queryKey[0] === "client-user-options").enabled, permissions.includes("user-plugin"))
    assert.equal(queries.find(q => q.queryKey[0] === "client-downloads").enabled, permissions.includes("subscription-plugin"))
    assert.equal(titles.includes("订阅版导入 magpie"), false)
  })
}

test("download permission defaults preserve legacy behavior and never restrict Owner", () => {
  assert.equal(authHelpers.canDownloadPlugin({ role: "member" }, "user-plugin"), true)
  assert.equal(authHelpers.canDownloadPlugin({ role: "member" }, "subscription-plugin"), false)
  assert.equal(authHelpers.canDownloadPlugin({ role: "member", download_permissions: [] }, "user-plugin"), false)
  assert.equal(authHelpers.canDownloadPlugin({ role: "owner", download_permissions: [] }, "subscription-plugin"), true)
  assert.equal(authHelpers.canDownloadPlugin(null, "user-plugin"), false)
})

for (const [userConsent, subscriptionConsent] of [[false, false], [true, false], [false, true], [true, true]]) {
  test(`both download buttons require their own consent: user=${userConsent}, subscription=${subscriptionConsent}`, async () => {
    const view = api('attachment; filename="aigateway-proxy-Test.zip"')
    let stateIndex = 0
    const jsx = (type, props) => ({ type, props })
    const page = load(pageCode, name => {
      if (name === "react") return { useEffect: () => {}, useRef: value => ({ current: value }),
        useState: value => {
          const index = stateIndex++
          return [index === 2 ? subscriptionConsent : index === 3 ? userConsent : value, () => {}]
        } }
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" }
      if (name === "@tanstack/react-query") return { useQuery: options => ({ data:
        options.queryKey[0] === "client-downloads"
          ? { items: [{ ...item, models: [{ id: "text-model" }] }], key_management_available: true }
          : { enabled: true, user_name: "Edwin" } }) }
      if (name === "../api/client-downloads") return { clientDownloadsApi: view.downloads }
      if (name === "../api/auth") return authHelpers
      if (name === "../providers/auth-provider") return { useAuth: () => ({ user: { id: "owner", role: "owner" } }) }
      if (name === "../components/ui/button") return { Button: "button" }
      if (name === "lucide-react") return { Download: "icon", PanelLeft: "icon", RefreshCw: "icon", ShieldCheck: "icon" }
      throw new Error(`Unexpected import ${name}`)
    }, {
      document: { body: { append() {} }, createElement: () => ({ click() {}, remove() {} }) },
      URL: { createObjectURL: () => "blob:zip", revokeObjectURL() {} },
      window: { setTimeout: callback => callback() },
    })
    const nodes = []
    function visit(node) {
      if (Array.isArray(node)) { node.forEach(visit); return }
      if (!node || typeof node !== "object") return
      nodes.push(node); visit(node.props?.children)
    }
    visit(page.ClientDownloadsPage({ onToggleSidebar: () => {} }))
    const userButton = nodes.find(n => n.type === "button" && Array.isArray(n.props.children) && n.props.children.includes("下载用户版插件"))
    const subscriptionButton = nodes.find(n => n.type === "button" && Array.isArray(n.props.children) && n.props.children.includes("下载订阅版插件"))
    assert.equal(userButton.props.disabled, !userConsent)
    assert.equal(subscriptionButton.props.disabled, !subscriptionConsent)
    assert.equal(userButton.props.variant, subscriptionButton.props.variant)
    assert.equal(userButton.props.className, "client-downloads-download-button")
    assert.equal(subscriptionButton.props.className, userButton.props.className)
    // Exercise handlers directly, bypassing native form validation and disabled-button behavior.
    await nodes.find(n => n.type === "form").props.onSubmit({ preventDefault() {} })
    await new Promise(resolve => setImmediate(resolve))
    userButton.props.onClick()
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(view.calls.length, Number(userConsent) + Number(subscriptionConsent))
  })
}

for (const [userReady, subscriptionReady] of [[false, false], [true, false], [false, true], [true, true]]) {
  test(`catalogs replace loading together after this visit's reads: user=${userReady}, subscription=${subscriptionReady}`, () => {
    const queries = [], jsx = (type, props) => ({ type, props })
    const page = load(pageCode, name => {
      if (name === "react") return { useEffect: () => {}, useRef: value => ({ current: value }), useState: value => [value, () => {}] }
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" }
      if (name === "@tanstack/react-query") return { useQuery: options => {
        queries.push(options)
        const subscription = options.queryKey[0] === "client-downloads"
        return { isPending: false, isFetchedAfterMount: subscription ? subscriptionReady : userReady,
          data: subscription ? { items: [item], key_management_available: true } : { enabled: true, user_name: "Edwin" } }
      } }
      if (name === "../api/client-downloads") return { clientDownloadsApi: {} }
      if (name === "../api/auth") return authHelpers
      if (name === "../providers/auth-provider") return { useAuth: () => ({ user: { id: "owner", role: "owner", name: "Edwin" } }) }
      if (name === "../components/ui/button") return { Button: "button" }
      if (name === "lucide-react") return { Download: "icon", PanelLeft: "icon", RefreshCw: "icon", ShieldCheck: "icon" }
      throw new Error(`Unexpected import ${name}`)
    })
    const nodes = []
    function visit(node) {
      if (Array.isArray(node)) { node.forEach(visit); return }
      if (!node || typeof node !== "object") return
      nodes.push(node); visit(node.props?.children)
    }
    visit(page.ClientDownloadsPage({ onToggleSidebar: () => {} }))
    const ready = userReady && subscriptionReady
    assert.equal(nodes.filter(n => n.type === "button" && n.props.className === "client-downloads-download-button").length, ready ? 2 : 0)
    assert.equal(nodes.some(n => n.props?.className === "client-downloads-card client-downloads-loading"), !ready)
    assert.equal(nodes.find(n => n.props?.className === "client-downloads-panels").props["aria-busy"], !ready)
    assert.ok(queries.every(q => q.staleTime === 60_000 && q.refetchOnMount === "always"))
  })
}
