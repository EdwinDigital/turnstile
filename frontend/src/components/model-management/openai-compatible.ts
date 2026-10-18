export function isOpenAICompatibleBaseUrl(value: string) {
  try {
    const endpoint = new URL(value.trim())
    return endpoint.protocol === "https:"
      && Boolean(endpoint.hostname)
      && !endpoint.username
      && !endpoint.password
      && !endpoint.search
      && !endpoint.hash
  } catch {
    return false
  }
}