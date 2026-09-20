import { createContext, type Context, type ReactNode, useContext, useEffect, useState } from "react"

export type LocalePreference = "en" | "zh-CN" | "zh-TW" | "ko" | "ja"

type LocaleContextValue = {
  locale: LocalePreference
  setLocale: (locale: LocalePreference) => void
}

type OpenCcText = Text & { originalString?: string }
type OpenCcElement = HTMLElement & {
  shouldChangeLang?: boolean
  originalContent?: string
  originalAlt?: string
  originalValue?: string
  originalPlaceholder?: string
  originalAriaLabel?: string
}

const LocaleContext: Context<LocaleContextValue | null> =
  import.meta.hot?.data.localeContext ?? createContext<LocaleContextValue | null>(null)
if (import.meta.hot) import.meta.hot.data.localeContext = LocaleContext
const COOKIE_NAME = "finops_locale"
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365

function readLocale(): LocalePreference {
  const value = document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(`${COOKIE_NAME}=`))
    ?.split("=")[1]
  return value === "zh-CN" || value === "zh-TW" || value === "ko" || value === "ja"
    ? value
    : "en"
}

export function getIntlLocale() {
  const locale = readLocale()
  if (locale === "en") return "en-US"
  if (locale === "ko") return "ko-KR"
  if (locale === "ja") return "ja-JP"
  return locale
}

type TextTranslator = (input: string) => string

let traditionalTranslatorPromise: Promise<TextTranslator> | undefined

function loadTraditionalTranslator() {
  traditionalTranslatorPromise ??= Promise.all([
    import("opencc-js/core"),
    import("opencc-js/from/cn"),
    import("opencc-js/to/twp"),
  ]).then(([core, fromModule, toModule]) => {
    const convertScript = core.ConverterFactory(fromModule.default, toModule.default)
    const convertProductTerms = core.CustomConverter([
      ["賬號", "帳號"],
      ["工作臺", "工作台"],
      ["平臺", "平台"],
      ["執行時", "執行階段"],
      ["程式碼倉庫", "程式碼儲存庫"],
      ["儀表盤", "儀表板"],
      ["用戶", "使用者"],
      ["自定義", "自訂"],
      ["默認", "預設"],
      ["新建呼叫", "新增呼叫"],
      ["介面協議", "介面協定"],
    ])
    return (input: string) => convertProductTerms(convertScript(input))
  })
  return traditionalTranslatorPromise
}

export async function translateForLocale(input: string, locale: LocalePreference) {
  if (locale === "zh-CN") return input
  if (locale === "en") return (await import("./en")).translateToEnglish(input)
  if (locale === "ko") return (await import("./ko")).translateToKorean(input)
  if (locale === "ja") return (await import("./ja")).translateToJapanese(input)
  return (await loadTraditionalTranslator())(input)
}

function walkNodes(root: Node, visitor: (node: Node) => void) {
  visitor(root)
  for (const child of root.childNodes) walkNodes(child, visitor)
}

function clearOpenCcMetadata(root: Node) {
  walkNodes(root, (node) => {
    delete (node as OpenCcText).originalString
    const element = node as OpenCcElement
    delete element.shouldChangeLang
    delete element.originalContent
    delete element.originalAlt
    delete element.originalValue
    delete element.originalPlaceholder
    delete element.originalAriaLabel
  })
}

function markIgnoredElements(root: HTMLElement) {
  root.querySelectorAll("code, pre, [data-no-localize]").forEach((element) => element.classList.add("ignore-opencc"))
}

function clearMutationMetadata(mutation: MutationRecord, titleOriginals: Map<HTMLElement, string>) {
  if (mutation.type === "characterData") {
    delete (mutation.target as OpenCcText).originalString
    return
  }
  if (mutation.type !== "attributes") return
  const element = mutation.target as OpenCcElement
  if (mutation.attributeName === "placeholder") delete element.originalPlaceholder
  if (mutation.attributeName === "aria-label") delete element.originalAriaLabel
  if (mutation.attributeName === "alt") delete element.originalAlt
  if (mutation.attributeName === "value") delete element.originalValue
  if (mutation.attributeName === "title") {
    const title = element.getAttribute("title")
    if (title == null) titleOriginals.delete(element)
    else titleOriginals.set(element, title)
  }
}

async function startPhraseConversion(
  root: HTMLElement,
  isCancelled: () => boolean,
  language: "en" | "ko" | "ja",
  load: () => Promise<{
    translate: (input: string) => string
    afterTranslate?: (root: HTMLElement) => void
  }>,
) {
  const { translate, afterTranslate } = await load()
  if (isCancelled()) return undefined

  const textOriginals = new Map<Text, string>()
  const attributeOriginals = new Map<HTMLElement, Map<string, string>>()
  const originalDocumentTitle = document.title
  const attributes = ["placeholder", "aria-label", "alt", "title", "value", "data-label"] as const
  let observer: MutationObserver

  const isIgnored = (element: Element | null) =>
    element?.closest("code, pre, [data-no-localize], .ignore-opencc") != null

  const translatePage = () => {
    observer.disconnect()
    markIgnoredElements(root)
    walkNodes(root, (node) => {
      if (node.nodeType !== Node.TEXT_NODE) return
      const textNode = node as Text
      if (isIgnored(textNode.parentElement)) return
      if (!textOriginals.has(textNode)) textOriginals.set(textNode, textNode.data)
      textNode.data = translate(textOriginals.get(textNode)!)
    })
    root.querySelectorAll<HTMLElement>("*").forEach((element) => {
      if (isIgnored(element)) return
      for (const attribute of attributes) {
        const value = element.getAttribute(attribute)
        if (value == null) continue
        let originals = attributeOriginals.get(element)
        if (!originals) {
          originals = new Map()
          attributeOriginals.set(element, originals)
        }
        if (!originals.has(attribute)) originals.set(attribute, value)
        element.setAttribute(attribute, translate(originals.get(attribute)!))
      }
    })
    afterTranslate?.(root)
    root.lang = language
    document.title = translate(originalDocumentTitle)
    observer.observe(root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: [...attributes],
    })
  }

  observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") textOriginals.delete(mutation.target as Text)
      if (mutation.type === "attributes") {
        const originals = attributeOriginals.get(mutation.target as HTMLElement)
        if (mutation.attributeName) originals?.delete(mutation.attributeName)
      }
    }
    translatePage()
  })
  translatePage()

  return () => {
    observer.disconnect()
    for (const [node, original] of textOriginals) {
      if (node.isConnected) node.data = original
    }
    for (const [element, originals] of attributeOriginals) {
      if (!element.isConnected) continue
      for (const [attribute, original] of originals) element.setAttribute(attribute, original)
    }
    document.title = originalDocumentTitle
    root.lang = "zh-CN"
  }
}

function startEnglishConversion(root: HTMLElement, isCancelled: () => boolean) {
  return startPhraseConversion(root, isCancelled, "en", async () => {
    const { correctEnglishPlurals, translateToEnglish } = await import("./en")
    return { translate: translateToEnglish, afterTranslate: correctEnglishPlurals }
  })
}

function startKoreanConversion(root: HTMLElement, isCancelled: () => boolean) {
  return startPhraseConversion(root, isCancelled, "ko", async () => {
    const { translateToKorean } = await import("./ko")
    return { translate: translateToKorean }
  })
}

function startJapaneseConversion(root: HTMLElement, isCancelled: () => boolean) {
  return startPhraseConversion(root, isCancelled, "ja", async () => {
    const { translateToJapanese } = await import("./ja")
    return { translate: translateToJapanese }
  })
}

async function startTraditionalConversion(root: HTMLElement, isCancelled: () => boolean) {
  const [toTraditional, core] = await Promise.all([
    loadTraditionalTranslator(),
    import("opencc-js/core"),
  ])
  if (isCancelled()) return undefined
  clearOpenCcMetadata(root)
  markIgnoredElements(root)
  const handler = core.HTMLConverter(toTraditional, root, "zh-CN", "zh-TW")
  const titleOriginals = new Map<HTMLElement, string>()
  const originalDocumentTitle = document.title
  let observer: MutationObserver

  const convertTitles = () => {
    root.querySelectorAll<HTMLElement>("[title]").forEach((element) => {
      if (element.closest(".ignore-opencc")) return
      if (!titleOriginals.has(element)) titleOriginals.set(element, element.title)
      element.title = toTraditional(titleOriginals.get(element)!)
    })
  }
  const observe = () => observer.observe(root, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["placeholder", "aria-label", "alt", "title", "value", "data-label"],
  })
  const convertPage = () => {
    observer.disconnect()
    markIgnoredElements(root)
    root.lang = "zh-CN"
    handler.convert()
    convertTitles()
    document.title = toTraditional(originalDocumentTitle)
    observe()
  }

  observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) clearMutationMetadata(mutation, titleOriginals)
    convertPage()
  })
  convertPage()

  return () => {
    observer.disconnect()
    handler.restore()
    for (const [element, title] of titleOriginals) {
      if (element.isConnected) element.title = title
    }
    document.title = originalDocumentTitle
    root.lang = "zh-CN"
    clearOpenCcMetadata(root)
  }
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<LocalePreference>(readLocale)

  useEffect(() => {
    document.documentElement.lang = locale
    document.documentElement.dataset.locale = locale
    const root = document.body
    root.lang = "zh-CN"
    if (locale === "zh-CN") return

    if (locale === "en" || locale === "ko" || locale === "ja") {
      let cancelled = false
      let stopConversion: (() => void) | undefined
      const startConversion = locale === "en"
        ? startEnglishConversion
        : locale === "ko"
          ? startKoreanConversion
          : startJapaneseConversion
      void startConversion(root, () => cancelled).then((stop) => {
        if (cancelled) stop?.()
        else stopConversion = stop
      })
      return () => {
        cancelled = true
        stopConversion?.()
        root.lang = "zh-CN"
      }
    }

    let cancelled = false
    let stopConversion: (() => void) | undefined
    void startTraditionalConversion(root, () => cancelled).then((stop) => {
      if (cancelled) stop?.()
      else stopConversion = stop
    })

    return () => {
      cancelled = true
      stopConversion?.()
      root.lang = "zh-CN"
    }
  }, [locale])

  const setLocale = (next: LocalePreference) => {
    document.cookie = `${COOKIE_NAME}=${next}; Max-Age=${COOKIE_MAX_AGE}; Path=/; SameSite=Lax`
    setLocaleState(next)
  }

  return <LocaleContext.Provider value={{ locale, setLocale }}>{children}</LocaleContext.Provider>
}

export function useLocale() {
  const context = useContext(LocaleContext)
  if (!context) throw new Error("useLocale must be used within LocaleProvider")
  return context
}
