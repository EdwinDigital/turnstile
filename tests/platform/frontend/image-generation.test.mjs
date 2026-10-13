import assert from "node:assert/strict"
import test from "node:test"

import { generatedImageBlob, imageDownloadFilename } from "../../../frontend/src/data-sources/apim/image-generation.ts"
import { compatibleNativeRouteRuntimes } from "../../../frontend/src/components/model-management/apim-native-route-eligibility.ts"

const result = {
  request_id: "request-image-1",
  output_format: "png",
  data: [{ media_type: "image/png", b64_json: Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).toString("base64") }],
}

test("image preview creates a typed Blob without external URLs", async () => {
  const blob = generatedImageBlob(result)
  assert.equal(blob.type, "image/png")
  assert.equal(blob.size, 8)
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [137, 80, 78, 71, 13, 10, 26, 10])
})

for (const data of [[], [...result.data, ...result.data], [{ media_type: "image/svg+xml", b64_json: "abcd" }], [{ media_type: "image/png", b64_json: "!invalid!" }], [{ media_type: "image/png", b64_json: "" }]]) {
  test(`invalid image payload ${JSON.stringify(data)} is rejected`, () => {
    assert.throws(() => generatedImageBlob({ ...result, data }))
  })
}

test("oversized Base64 is rejected before decoding", () => {
  assert.throws(() => generatedImageBlob({ ...result, data: [{ media_type: "image/png", b64_json: "A".repeat(16 * 1024 * 1024 + 1) }] }))
})

test("download name cannot contain paths or provider-controlled suffixes", () => {
  assert.equal(imageDownloadFilename(result), "turnstile-request-image-1.png")
  assert.equal(imageDownloadFilename({ ...result, request_id: "../../unsafe/name", output_format: "jpeg" }), "turnstile-unsafename.jpg")
  assert.equal(imageDownloadFilename({ ...result, request_id: "..." }), "turnstile-image.png")
})

test("image models cannot enter text backend pools", () => {
  assert.deepEqual(compatibleNativeRouteRuntimes({ capabilities: ["image_generation"] }, { runtimes: [] }), [])
})

for (const [format, suffix] of [["png", "png"], ["jpeg", "jpg"], ["webp", "webp"]]) {
  test(`preview and download use the returned ${format} format`, () => {
    const response = { ...result, output_format: format, data: [{ ...result.data[0], media_type: `image/${format}` }] }
    assert.equal(generatedImageBlob(response).type, `image/${format}`)
    assert.equal(imageDownloadFilename(response), `turnstile-request-image-1.${suffix}`)
  })
}