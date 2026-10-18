import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { DEFAULT_MIN_COLUMN_WIDTH, preserveColumnWidths } from "../../../frontend/src/components/ui/column-widths.ts"

const minimums = [180, 120, 160, 80, 48]

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/components/ui/resizable-table.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: id => id !== entry,
  transform: { jsx: { runtime: "automatic" } },
  plugins: [{ name: "expose-production-resize-hook", load: id => id === entry
    ? `${readFileSync(entry, "utf8")}\nexport { useResizableColumns }` : null }],
})
let compiled
try { compiled = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }

class Element {
  constructor(label = "", width = 200) {
    this.textContent = label
    this.width = width
    this.children = []
    this.style = { position: "" }
    this.attributes = new Map()
    this.listeners = new Map()
    this.classes = new Set()
    this.classList = { add: name => this.classes.add(name), remove: name => this.classes.delete(name) }
  }
  getBoundingClientRect() { return { width: this.width } }
  getAttribute(name) { return this.attributes.get(name) ?? null }
  setAttribute(name, value) { this.attributes.set(name, value) }
  removeAttribute(name) { this.attributes.delete(name) }
  appendChild(child) { this.children.push(child); child.parentElement = this }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this)
    this.parentElement = null
  }
  addEventListener(name, listener) { this.listeners.set(name, listener) }
  removeEventListener(name, listener) { if (this.listeners.get(name) === listener) this.listeners.delete(name) }
  emit(name, event) { this.listeners.get(name)?.(event) }
}

function resizeHarness() {
  const cells = [new Element("Model", 200), new Element("Price", 220)]
  const header = { children: cells }
  const props = { rootRef: { current: { querySelector: () => header } }, headerSelector: "header", minWidths: [180, 120], enabled: true }
  const slots = []
  const effectSlots = new Map()
  let cursor = 0
  let dirty = false
  let pending = []
  const window = new Element()
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
      return [slots[index], update => {
        const value = typeof update === "function" ? update(slots[index]) : update
        if (!Object.is(slots[index], value)) { slots[index] = value; dirty = true }
      }]
    },
    useRef(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = { current: initial }
      return slots[index]
    },
    useLayoutEffect(effect, dependencies) {
      const index = cursor++
      const previous = effectSlots.get(index)
      if (!dependencies || !previous || dependencies.some((value, position) => !Object.is(value, previous.dependencies[position]))) {
        pending.push(() => {
          previous?.cleanup?.()
          effectSlots.set(index, { dependencies, cleanup: effect() })
        })
      }
    },
  }
  const imports = { react: hooks, "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "react-dom": { createPortal: (element, container) => ({ element, container }) },
    "./column-widths": { DEFAULT_MIN_COLUMN_WIDTH, preserveColumnWidths } }
  const exports = {}
  runInNewContext(compiled, { exports, require: name => imports[name], window,
    HTMLElement: Element, document: { createElement: () => new Element() },
    getComputedStyle: cell => ({ position: cell.style.position || "static" }),
    ResizeObserver: class { observe() {} disconnect() {} },
  })
  const render = () => {
    for (let iteration = 0; iteration < 12; iteration++) {
      cursor = 0; dirty = false; pending = []
      const result = exports.useResizableColumns(props)
      pending.forEach(run => run())
      if (!dirty) return result
    }
    assert.fail("Resize hook did not settle")
  }
  const pointer = { clientX: 100, pointerId: 1, currentTarget: new Element(), preventDefault() {}, stopPropagation() {} }
  return { props, cells, header, render, pointer, window,
    close() { [...effectSlots.values()].reverse().forEach(effect => effect.cleanup?.()) } }
}

test("resize overlay starts only after the pointer crosses the drag threshold", () => {
  const harness = resizeHarness()
  try {
    harness.render().portals[0].element.props.onPointerDown(harness.pointer)
    assert.equal(harness.render().overlay, null)
    harness.window.emit("pointermove", { clientX: 103 })
    assert.equal(harness.render().activeColumn, null)
    harness.window.emit("pointermove", { clientX: 104 })
    assert.equal(harness.render().activeColumn, 0)
    assert.deepEqual(Array.from(harness.render().widths), [204, 220])
    harness.window.emit("pointerup", {})
    assert.equal(harness.render().overlay, null)
    assert.equal(harness.window.listeners.size, 0)
  } finally { harness.close() }
})

test("resized, reset and accessible widths respect updated minima", () => {
  const harness = resizeHarness()
  try {
    const key = value => ({ key: value, preventDefault() {}, stopPropagation() {} })
    harness.render().portals[0].element.props.onKeyDown(key("ArrowRight"))
    assert.equal(harness.render().widths[0], 208)
    harness.props.minWidths = [260, 120]
    assert.equal(harness.render().widths[0], 260)
    assert.equal(harness.render().portals[0].element.props["aria-valuenow"], 260)
    harness.render().portals[0].element.props.onKeyDown(key("Enter"))
    assert.equal(harness.render().widths[0], 260)
  } finally { harness.close() }
})

test("resize portals are isolated and retired headers release their containers", () => {
  const harness = resizeHarness()
  try {
    const container = harness.render().portals[0].container
    assert.notEqual(container, harness.cells[0])
    assert.equal(container.parentElement, harness.cells[0])
    assert.equal(container.style.display, "contents")
    harness.header.children = [new Element("Replacement", 240)]
    assert.equal(harness.render().portals[0].container.parentElement, harness.header.children[0])
    assert.equal(container.parentElement, null)
    assert.equal(harness.cells[0].classes.size, 0)
  } finally { harness.close() }
})

test("replaced header cells reset stale widths", () => {
  const harness = resizeHarness()
  try {
    harness.render().portals[0].element.props.onKeyDown({ key: "ArrowRight", preventDefault() {}, stopPropagation() {} })
    assert.ok(harness.render().widths)
    harness.header.children = [new Element("Changed column", 240), harness.cells[1]]
    assert.equal(harness.render().widths, null)
  } finally { harness.close() }
})

test("repeated narrow and wide viewports preserve hidden column widths", () => {
  const desktop = preserveColumnWidths([323.2, 272, 314.5, 100, 106.7], null, minimums)
  let remembered = desktop
  for (let iteration = 0; iteration < 5; iteration += 1) {
    remembered = preserveColumnWidths([191.3, 0, 0, 91.3, 59.3], remembered, minimums)
    assert.deepEqual(remembered, [191, 272, 315, 91, 59])
    remembered = preserveColumnWidths([215, 0, 0, 91, 59], remembered, minimums)
    assert.deepEqual(remembered.slice(1, 3), desktop.slice(1, 3))
  }
})

test("first mobile render falls back to each unseen column minimum", () => {
  assert.deepEqual(preserveColumnWidths([180, 0, 0, 90, 72], null, minimums), [180, 120, 160, 90, 72])
})

test("valid measurements replace history without clamping unrelated columns", () => {
  assert.deepEqual(preserveColumnWidths([200.4, 120.6, 160.5, 79.8, 40.4], [400, 300, 300, 100, 160], minimums), [200, 121, 161, 80, 40])
})

test("hiding an entire table retains its last visible widths", () => {
  const previous = [323, 252, 315, 100, 107]
  assert.deepEqual(preserveColumnWidths([0, 0, 0, 0, 0], previous, minimums), previous)
})

for (const observer of [null, [323, 252, 315, 100, 107]]) {
  test(`committed widths take priority over ${observer ? "stale" : "pending"} observer history`, () => {
    const widths = [323, 272, 315, 100, 107]
    assert.deepEqual(preserveColumnWidths([191, 0, 0, 91, 59], widths ?? observer, minimums), [191, 272, 315, 91, 59])
  })
}

for (const invalid of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
  test(`invalid width ${invalid} cannot erase history or produce zero`, () => {
    assert.deepEqual(preserveColumnWidths([invalid], [123], [80]), [123])
    assert.deepEqual(preserveColumnWidths([0], [invalid], [80]), [80])
    assert.deepEqual(preserveColumnWidths([0], null, [invalid]), [DEFAULT_MIN_COLUMN_WIDTH])
  })
}

test("missing values and subpixel measurements have finite positive fallbacks", () => {
  assert.deepEqual(preserveColumnWidths([0, 0, 0], [140], [80, 90]), [140, 90, 72])
  assert.deepEqual(preserveColumnWidths([], [140], minimums), [])
  assert.deepEqual(preserveColumnWidths([0.1, 0], [0, 0.1]), [1, 1])
})

test("measurement and history arrays are never mutated", () => {
  const measured = Object.freeze([190, 0, 0, 90, 72])
  const previous = Object.freeze([323, 252, 315, 100, 107])
  const result = preserveColumnWidths(measured, previous, Object.freeze(minimums))
  assert.deepEqual(result, [190, 252, 315, 90, 72])
  assert.notEqual(result, previous)
  assert.deepEqual(previous, [323, 252, 315, 100, 107])
})