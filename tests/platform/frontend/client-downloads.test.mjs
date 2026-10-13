import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import vm from "node:vm"

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(require.resolve("rolldown"))

async function compile(path) {
  const entry = fileURLToPath(new URL(`../../../frontend/src/${path}`, import.meta.url))
  const bundle = await rolldown({ input: entry, external: id => id !== entry, treeshake: false,
    transform: { jsx: { runtime: "automatic" } },
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const apiCode = await compile("api/client-downloads.ts")
const pageCode = await compile("pages/client-downloads-page.tsx")
const item = { application_id: "app-uuid", subscription_id: "subscription-uuid",
  gateway_id: "gateway-uuid", application_name: "Other application", subscription_name: "Edwin-Agent", models: [] }

function load(code, require, globals = {}) {
  const module = { exports: {} }
  vm.runInNewContext(code, { exports: module.exports, module, require, AbortController, Error, ...globals })
  return module.exports
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
    assert.ok(nodes.some(node => node.type === "p" && node.props.children ===
      "解压后的文件夹名称与 ZIP 文件名一致（不含 .zip），与下方订阅版独立，可同时安装。"))
    assert.ok(nodes.some(node => node.type === "p" && node.props.children ===
      "指定订阅下载 · 保留订阅密钥认证方式，解压目录与 ZIP 文件名一致。"))
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
