import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { cachePolicies } from "../../../frontend/src/api/cache-policies.ts"

const wait = ms => new Promise(resolve => setTimeout(resolve, ms))

test("the actual query observer pauses while hidden and refreshes when visible again", async t => {
  const originalWindow = Object.getOwnPropertyDescriptor(global, "window")
  Object.defineProperty(global, "window", {
    configurable: true,
    value: { addEventListener() {}, removeEventListener() {} },
  })
  const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
  const { QueryClient, QueryObserver, focusManager } = require("@tanstack/react-query")
  focusManager.setFocused(true)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.mount()
  let calls = 0
  const observer = new QueryObserver(client, {
    queryKey: ["usage-polling-test"],
    queryFn: async () => ++calls,
    ...cachePolicies.liveUsage,
    // Accelerate only the test's clock; production keeps its 10-second interval.
    refetchInterval: 20,
  })
  const unsubscribe = observer.subscribe(() => {})
  t.after(() => {
    unsubscribe()
    client.unmount()
    client.clear()
    focusManager.setFocused(undefined)
    if (originalWindow) Object.defineProperty(global, "window", originalWindow)
    else delete global.window
  })
  const deadline = Date.now() + 1000
  while (calls < 3 && Date.now() < deadline) await wait(10)
  assert.ok(calls >= 3, "Visible queries should poll")
  focusManager.setFocused(false)
  await wait(10)
  const hiddenCalls = calls
  await wait(80)
  assert.equal(calls, hiddenCalls, "Hidden queries should not poll")
  focusManager.setFocused(true)
  await wait(10)
  assert.ok(calls > hiddenCalls, "Returning to a visible tab should refresh immediately")
})
