import type { ImageInvocationResponse } from "./types"

export function generatedImageBlob(result: ImageInvocationResponse): Blob {
  if (result.data.length !== 1) throw new Error("Expected one generated image")
  const image = result.data[0]
  if (!["image/png", "image/jpeg", "image/webp"].includes(image.media_type)) throw new Error("Unsupported image format")
  if (!image.b64_json || image.b64_json.length > 16 * 1024 * 1024) throw new Error("Invalid image size")
  const decoded = atob(image.b64_json)
  const bytes = Uint8Array.from(decoded, character => character.charCodeAt(0))
  return new Blob([bytes], { type: image.media_type })
}

export function imageDownloadFilename(result: ImageInvocationResponse): string {
  const identifier = result.request_id.replace(/[^a-zA-Z0-9-]/g, "").slice(0, 80) || "image"
  return `turnstile-${identifier}.${result.output_format === "jpeg" ? "jpg" : result.output_format === "webp" ? "webp" : "png"}`
}