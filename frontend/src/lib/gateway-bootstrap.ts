import type { SelfHostedGatewayBootstrap } from "../data-sources/apim/types"

export type GatewayBootstrapMetadata = Omit<SelfHostedGatewayBootstrap, "environment_file">

export function consumeGatewayBootstrap(
  value: SelfHostedGatewayBootstrap, deploymentId: string,
  download: (name: string, content: string) => void,
): GatewayBootstrapMetadata {
  let environment = value.environment_file
  value.environment_file = ""
  try {
    const metadata = { deployment_id: value.deployment_id, native_gateway_id: value.native_gateway_id,
      configuration_endpoint: value.configuration_endpoint, expires_at: value.expires_at,
      image: value.image, environment_file_name: value.environment_file_name,
      docker_command: value.docker_command, renewal_command: value.renewal_command }
    if (value.deployment_id !== deploymentId || !/^turnstile-[a-f0-9]{32}$/.test(value.native_gateway_id)
      || value.environment_file_name !== `${value.native_gateway_id}.env`
      || !/^mcr\.microsoft\.com\/azure-api-management\/gateway(?::\d+\.\d+\.\d+|@sha256:[a-f0-9]{64})$/.test(value.image)
      || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now()
      || typeof environment !== "string" || !environment || environment.length > 16384) {
      throw new Error("启动配置无效")
    }
    const endpoint = new URL(metadata.configuration_endpoint)
    if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error("启动配置无效")
    const auth = environment.split("\n").find(line => line.startsWith("config.service.auth="))?.slice("config.service.auth=".length)
    const token = auth?.replace(/^GatewayKey /, "")
    if (!token || !token.startsWith(value.native_gateway_id + "&") || !/^[\x21-\x7e]+$/.test(token)
      || Object.values(metadata).some(field => typeof field !== "string" || field.includes(token))) throw new Error("启动配置无效")
    download(metadata.environment_file_name, environment)
    return metadata
  } finally {
    environment = ""
    value.environment_file = ""
  }
}

export function downloadGatewayEnvironment(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "application/octet-stream" }))
  const link = document.createElement("a")
  try {
    link.href = url
    link.download = name
    link.click()
  } finally {
    link.removeAttribute("href")
    link.remove()
    URL.revokeObjectURL(url)
  }
}