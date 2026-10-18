import assert from "node:assert/strict"
import { once } from "node:events"
import { createServer } from "node:http"
import { createRequire } from "node:module"
import test from "node:test"

import viteConfig from "../../../frontend/vite.config.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { createServer: createViteServer } = await import(frontendRequire.resolve("vite"))

async function listen(server) {
  server.listen(0, "127.0.0.1")
  await once(server, "listening")
  return `http://127.0.0.1:${server.address().port}`
}

async function close(server) {
  server.closeAllConnections()
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
}

test("development proxy preserves the write-origin security boundary", async suite => {
  const upstream = createServer((request, response) => {
    response.setHeader("Content-Type", "application/json")
    response.end(JSON.stringify({
      method: request.method,
      host: request.headers.host,
      origin: request.headers.origin ?? null,
    }))
  })
  const apiTarget = await listen(upstream)
  suite.after(() => close(upstream))

  const previousTarget = process.env.API_PROXY_TARGET
  let configuration
  try {
    process.env.API_PROXY_TARGET = apiTarget
    configuration = viteConfig({ command: "serve", mode: "test" })
  } finally {
    if (previousTarget === undefined) delete process.env.API_PROXY_TARGET
    else process.env.API_PROXY_TARGET = previousTarget
  }

  const vite = await createViteServer({
    configFile: false,
    cacheDir: "frontend/node_modules/.vite-proxy-test",
    appType: "custom",
    logLevel: "silent",
    server: {
      middlewareMode: true,
      watch: null,
      hmr: false,
      ws: false,
      proxy: configuration.server.proxy,
    },
  })
  suite.after(() => vite.close())
  const frontend = createServer(vite.middlewares)
  const frontendOrigin = await listen(frontend)
  suite.after(() => close(frontend))

  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    await suite.test(`${method} from the same local origin uses the upstream origin`, async () => {
      const response = await fetch(`${frontendOrigin}/api/proxy-check`, {
        method,
        headers: { Origin: frontendOrigin, "Sec-Fetch-Site": "same-origin" },
      })
      assert.equal(response.status, 200)
      assert.deepEqual(await response.json(), {
        method, host: new URL(apiTarget).host, origin: apiTarget,
      })
    })
  }

  const foreignOrigins = [
    "https://untrusted.example",
    "http://127.0.0.1:1",
    frontendOrigin.replace("http:", "https:"),
    `${frontendOrigin}/not-an-origin`,
    "null",
  ]
  for (const origin of foreignOrigins) {
    await suite.test(`does not trust ${origin}`, async () => {
      const response = await fetch(`${frontendOrigin}/api/proxy-check`, {
        method: "PUT", headers: { Origin: origin },
      })
      assert.equal(response.status, 200)
      assert.equal((await response.json()).origin, origin)
    })
  }

  for (const fetchSite of ["cross-site", "same-site"]) {
    await suite.test(`does not rewrite a ${fetchSite} request`, async () => {
      const response = await fetch(`${frontendOrigin}/api/proxy-check`, {
        method: "PUT", headers: { Origin: frontendOrigin, "Sec-Fetch-Site": fetchSite },
      })
      assert.equal((await response.json()).origin, frontendOrigin)
    })
  }

  await suite.test("does not add an absent Origin or rewrite read requests", async () => {
    const write = await fetch(`${frontendOrigin}/api/proxy-check`, { method: "PUT" })
    assert.equal((await write.json()).origin, null)
    const read = await fetch(`${frontendOrigin}/api/proxy-check`, {
      headers: { Origin: frontendOrigin },
    })
    assert.equal((await read.json()).origin, frontendOrigin)
  })
})