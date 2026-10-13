/** Brand marks shared by model management and the FinOps toolbar. */
import { useId } from "react"
import { Cloud, Database, Network } from "lucide-react"

import deepseekIcon from "../assets/models/deepseek.svg"
import kimiIcon from "../assets/models/kimi.svg"
import amazonBedrockIcon from "../assets/providers/amazon-bedrock.svg"
import azureGatewaysIcon from "../assets/providers/azure-gateways.svg"
import type { BrandKey } from "../data-sources/apim/types"
import type { RuntimeAccessPoint } from "../lib/access-points"

export function OpenAiLogo({ size }: { size: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M14.949 6.547a3.94 3.94 0 0 0-.348-3.273 4.11 4.11 0 0 0-4.4-1.934A4.1 4.1 0 0 0 8.423.2 4.15 4.15 0 0 0 6.305.086a4.1 4.1 0 0 0-1.891.948 4.04 4.04 0 0 0-1.158 1.753 4.1 4.1 0 0 0-1.563.679A4 4 0 0 0 .554 4.72a3.99 3.99 0 0 0 .502 4.731 3.94 3.94 0 0 0 .346 3.274 4.11 4.11 0 0 0 4.402 1.933c.382.425.852.764 1.377.995.526.231 1.095.35 1.67.346 1.78.002 3.358-1.132 3.901-2.804a4.1 4.1 0 0 0 1.563-.68 4 4 0 0 0 1.14-1.253 3.99 3.99 0 0 0-.506-4.716m-6.097 8.406a3.05 3.05 0 0 1-1.945-.694l.096-.054 3.23-1.838a.53.53 0 0 0 .265-.455v-4.49l1.366.778q.02.011.025.035v3.722c-.003 1.653-1.361 2.992-3.037 2.996m-6.53-2.75a2.95 2.95 0 0 1-.36-2.01l.095.057L5.29 12.09a.53.53 0 0 0 .527 0l3.949-2.246v1.555a.05.05 0 0 1-.022.041L6.473 13.3c-1.454.826-3.311.335-4.15-1.098m-.85-6.94A3.02 3.02 0 0 1 3.07 3.949v3.785a.51.51 0 0 0 .262.451l3.93 2.237-1.366.779a.05.05 0 0 1-.048 0L2.585 9.342a2.98 2.98 0 0 1-1.113-4.094zm11.216 2.571L8.747 5.576l1.362-.776a.05.05 0 0 1 .048 0l3.265 1.86a3 3 0 0 1 1.173 1.207 2.96 2.96 0 0 1-.27 3.2 3.05 3.05 0 0 1-1.36.997V8.279a.52.52 0 0 0-.276-.445m1.36-2.015-.097-.057-3.226-1.855a.53.53 0 0 0-.53 0L6.249 6.153V4.598a.04.04 0 0 1 .019-.04L9.533 2.7a3.07 3.07 0 0 1 3.257.139c.474.325.843.778 1.066 1.303.223.526.289 1.103.191 1.664zM5.503 8.575 4.139 7.8a.05.05 0 0 1-.026-.037V4.049c0-.57.166-1.127.476-1.607s.752-.864 1.275-1.105a3.08 3.08 0 0 1 3.234.41l-.096.054-3.23 1.838a.53.53 0 0 0-.265.455zm.742-1.577 1.758-1 1.762 1v2l-1.755 1-1.762-1z" />
    </svg>
  )
}

export function CopilotLogo({ size }: { size: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 1C5.9225 1 1 5.9225 1 12C1 16.8675 4.14875 20.9787 8.52125 22.4362C9.07125 22.5325 9.2775 22.2025 9.2775 21.9137C9.2775 21.6525 9.26375 20.7862 9.26375 19.865C6.5 20.3737 5.785 19.1912 5.565 18.5725C5.44125 18.2562 4.905 17.28 4.4375 17.0187C4.0525 16.8125 3.5025 16.3037 4.42375 16.29C5.29 16.2762 5.90875 17.0875 6.115 17.4175C7.105 19.0812 8.68625 18.6137 9.31875 18.325C9.415 17.61 9.70375 17.1287 10.02 16.8537C7.5725 16.5787 5.015 15.63 5.015 11.4225C5.015 10.2262 5.44125 9.23625 6.1425 8.46625C6.0325 8.19125 5.6475 7.06375 6.2525 5.55125C6.2525 5.55125 7.17375 5.2625 9.2775 6.67875C10.1575 6.43125 11.0925 6.3075 12.0275 6.3075C12.9625 6.3075 13.8975 6.43125 14.7775 6.67875C16.8813 5.24875 17.8025 5.55125 17.8025 5.55125C18.4075 7.06375 18.0225 8.19125 17.9125 8.46625C18.6138 9.23625 19.04 10.2125 19.04 11.4225C19.04 15.6437 16.4688 16.5787 14.0213 16.8537C14.42 17.1975 14.7638 17.8575 14.7638 18.8887C14.7638 20.36 14.75 21.5425 14.75 21.9137C14.75 22.2025 14.9563 22.5462 15.5063 22.4362C19.8513 20.9787 23 16.8537 23 12C23 5.9225 18.0775 1 12 1Z" />
    </svg>
  )
}

export function ClaudeLogo({ size }: { size: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 16 16" fill="#D97757">
      <path d="m3.127 10.604 3.135-1.76.053-.153-.053-.085H6.11l-.525-.032-1.791-.048-1.554-.065-1.505-.08-.38-.081L0 7.832l.036-.234.32-.214.455.04 1.009.069 1.513.105 1.097.064 1.626.17h.259l.036-.105-.089-.065-.068-.064-1.566-1.062-1.695-1.121-.887-.646-.48-.327-.243-.306-.104-.67.435-.48.585.04.15.04.593.456 1.267.981 1.654 1.218.242.202.097-.068.012-.049-.109-.181-.9-1.626-.96-1.655-.428-.686-.113-.411a2 2 0 0 1-.068-.484l.496-.674L4.446 0l.662.089.279.242.411.94.666 1.48 1.033 2.014.302.597.162.553.06.17h.105v-.097l.085-1.134.157-1.392.154-1.792.052-.504.25-.605.497-.327.387.186.319.456-.045.294-.19 1.23-.37 1.93-.243 1.29h.142l.161-.16.654-.868 1.097-1.372.484-.545.565-.601.363-.287h.686l.505.751-.226.775-.707.895-.585.759-.839 1.13-.524.904.048.072.125-.012 1.897-.403 1.024-.186 1.223-.21.553.258.06.263-.218.536-1.307.323-1.533.307-2.284.54-.028.02.032.04 1.029.098.44.024h1.077l2.005.15.525.346.315.424-.053.323-.807.411-3.631-.863-.872-.218h-.12v.073l.726.71 1.331 1.202 1.667 1.55.084.383-.214.302-.226-.032-1.464-1.101-.565-.497-1.28-1.077h-.084v.113l.295.432 1.557 2.34.08.718-.112.234-.404.141-.444-.08-.911-1.28-.94-1.44-.759-1.291-.093.053-.448 4.821-.21.246-.484.186-.403-.307-.214-.496.214-.98.258-1.28.21-1.016.19-1.263.112-.42-.008-.028-.092.012-.953 1.307-1.448 1.957-1.146 1.227-.274.109-.477-.247.045-.44.266-.39 1.586-2.018.956-1.25.617-.723-.004-.105h-.036l-4.212 2.736-.75.096-.324-.302.04-.496.154-.162 1.267-.871z" />
    </svg>
  )
}

export function KimiLogo({ size }: { size: number }) {
  return <img aria-hidden="true" src={kimiIcon} width={size} height={size} alt="" />
}

export function DeepSeekLogo({ size }: { size: number }) {
  return <img aria-hidden="true" src={deepseekIcon} width={size} height={size} alt="" />
}

// Microsoft — official four-square mark, geometry from Bootstrap Icons (bi-microsoft)
export function MicrosoftLogo({ size }: { size: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 16 16">
      <path d="M7.462 0H0v7.19h7.462z" fill="#F25022" />
      <path d="M16 0H8.538v7.19H16z" fill="#7FBA00" />
      <path d="M7.462 8.211H0V16h7.462z" fill="#00A4EF" />
      <path d="M16 8.211H8.538V16H16z" fill="#FFB900" />
    </svg>
  )
}

// Databricks — official logomark, geometry from Simple Icons (databricks)
export function DatabricksLogo({ size }: { size: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="#FF3621">
      <path d="M.95 14.184L12 20.403l9.919-5.55v2.21L12 22.662l-10.484-5.96-.565.308v.77L12 24l11.05-6.218v-4.317l-.515-.309L12 19.118l-9.867-5.653v-2.21L12 16.805l11.05-6.218V6.32l-.515-.308L12 11.974 2.647 6.681 12 1.388l7.76 4.368.668-.411v-.566L12 0 .95 6.27v.72L12 13.207l9.919-5.55v2.26L12 15.52 1.516 9.56l-.565.308Z" />
    </svg>
  )
}

// Amazon Bedrock - official AWS Architecture Service Icon package, 2026-04-30,
// Arch_Artificial-Intelligence/32/Arch_Amazon-Bedrock_32.svg.
export function AmazonBedrockLogo({ size }: { size: number }) {
  return <img aria-hidden="true" src={amazonBedrockIcon} width={size} height={size} alt="" />
}

// Microsoft Foundry — official service icon from Microsoft's Azure architecture
// icon set (Azure_Public_Service_Icons_V24, ai + machine learning /
// icon-service-AI-Foundry.svg). Gradient ids are per-instance so several rows
// on one page cannot collide on a shared id.
export function FoundryLogo({ size }: { size: number }) {
  const uid = `foundry-${useId().replace(/:/g, "")}`
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 18 18">
      <defs>
        <linearGradient id={`${uid}-a`} x1="10.91" y1="13.66" x2="13.62" y2="13.66" gradientTransform="translate(0 20) scale(1 -1)" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#302ec9" /><stop offset=".12" stopColor="#151698" /><stop offset=".25" stopColor="#0a0b7e" /><stop offset=".43" stopColor="#090c7f" /><stop offset=".51" stopColor="#141698" /><stop offset=".61" stopColor="#282ac4" /><stop offset=".77" stopColor="#8e8ff4" /><stop offset=".85" stopColor="#a7a6f7" /><stop offset=".91" stopColor="#cacafb" /><stop offset="1" stopColor="#fff" />
        </linearGradient>
        <linearGradient id={`${uid}-b`} x1="12.26" y1="8.32" x2="12.26" y2="18.23" gradientTransform="translate(0 20) scale(1 -1)" gradientUnits="userSpaceOnUse">
          <stop offset=".02" stopColor="#201ba6" /><stop offset=".64" stopColor="#2d29c7" /><stop offset="1" stopColor="#201ba6" />
        </linearGradient>
        <linearGradient id={`${uid}-c`} x1="14.19" y1="8.27" x2="14.19" y2="14.47" gradientTransform="translate(0 20) scale(1 -1)" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#3530c3" /><stop offset=".5" stopColor="#6d71d1" /><stop offset=".97" stopColor="#c0bff5" /><stop offset="1" stopColor="#e3e4ff" />
        </linearGradient>
        <linearGradient id={`${uid}-d`} x1="6.51" y1="3" x2="6.51" y2="19.15" gradientTransform="translate(0 20) scale(1 -1)" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#302ec9" /><stop offset=".45" stopColor="#302ec9" /><stop offset=".95" stopColor="#cacafb" /><stop offset="1" stopColor="#7f7eaf" />
        </linearGradient>
      </defs>
      <path d="M11.96,11.68s.07-.1.07-.24v-3.87c0-.85.66-1.76,1.59-2.03-.06-.19-.64-2.11-.81-2.65-.17-.56-.67-1.9-1.29-1.9,0,0,0,0-.01,0h0s-.09.05-.13.13c-.32.58-.46,3.02-.46,4.84v2.26c.27.97.91,3.22.96,3.35,0,0,.05.1.09.1h0Z" fill={`url(#${uid}-a)`} />
      <path d="M11.96,11.68s.07-.1.07-.24v-3.87c0-.85.66-1.76,1.59-2.03-.06-.19-.64-2.11-.81-2.65-.17-.56-.67-1.9-1.29-1.9,0,0,0,0-.01,0h0s-.09.05-.13.13c-.32.58-.46,3.02-.46,4.84v2.26c.27.97.91,3.22.96,3.35,0,0,.05.1.09.1h0Z" fill={`url(#${uid}-b)`} />
      <path d="M16.01,5.46h-1.81c-1.21,0-2.17,1.09-2.17,2.11v3.87c0,.14-.04.24-.07.24s-.09-.1-.09-.1c0,0,.07.21.17.21h1.98c.62,0,2.48-.61,2.48-2.52v-3.25c0-.41-.24-.55-.49-.55Z" fill={`url(#${uid}-c)`} />
      <path d="M2.34,17h5.38c2.35,0,3.2-2.03,3.2-3.81v-7.23c0-2.08.19-4.97.61-4.97h-3.23c-.77,0-1.43.82-2.18,2.48-.75,1.66-4.36,11.87-4.52,12.36-.26.79-.02,1.16.74,1.16Z" fill={`url(#${uid}-d)`} />
    </svg>
  )
}

// Azure API Management — official service icon from Microsoft's Azure
// architecture icon set (Azure_Public_Service_Icons_V24, integration /
// icon-service-API-Management-Services.svg). Gradient ids are per-instance.
export function ApimLogo({ size }: { size: number }) {
  const uid = `apim-${useId().replace(/:/g, "")}`
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 18 18">
      <defs>
        <linearGradient id={`${uid}-a`} x1="9" y1="16.82" x2="9" y2="1.18" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#198ab3" /><stop offset=".09" stopColor="#1f9dc4" /><stop offset=".24" stopColor="#28b5d9" /><stop offset=".4" stopColor="#2dc6e9" /><stop offset=".57" stopColor="#31d1f2" /><stop offset=".78" stopColor="#32d4f5" />
        </linearGradient>
        <linearGradient id={`${uid}-b`} x1="8.36" y1="11.35" x2="8.36" y2="14.46" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#c69aeb" /><stop offset="1" stopColor="#6f4bb2" />
        </linearGradient>
      </defs>
      <path d="M14.18,5.89A4.85,4.85,0,0,0,9.23,1.18,5,5,0,0,0,4.48,4.47,4.61,4.61,0,0,0,.5,9,4.67,4.67,0,0,0,5.29,13.5a3,3,0,0,0,.42,0h1.2a1.47,1.47,0,0,1-.11-.56v0A1.51,1.51,0,0,1,7,12.21H5.6l-.31,0A3.41,3.41,0,0,1,1.77,9,3.33,3.33,0,0,1,4.68,5.73l.76-.12.25-.73A3.73,3.73,0,0,1,9.23,2.45,3.6,3.6,0,0,1,12.91,5.9V7L14,7.15a2.59,2.59,0,0,1,2.26,2.49,2.63,2.63,0,0,1-2.62,2.54h-.15l-.08,0h-1A3.92,3.92,0,0,0,8.54,9a.64.64,0,1,0,0,1.27,2.65,2.65,0,0,1,0,5.29.64.64,0,1,0,0,1.27,3.92,3.92,0,0,0,3.87-3.34h1.05a.64.64,0,0,0,.2,0A3.91,3.91,0,0,0,17.5,9.64,3.86,3.86,0,0,0,14.18,5.89Z" fill={`url(#${uid}-a)`} />
      <rect x="6.8" y="11.35" width="3.12" height="3.12" rx="1.54" fill={`url(#${uid}-b)`} />
    </svg>
  )
}

// LiteLLM — the project's brand mark is the 🚅 bullet train used in its own
// favicon and README title; vector artwork from Twemoji (CC-BY 4.0, U+1F685).
export function LiteLlmLogo({ size }: { size: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 36 36">
      <path fill="#939598" d="M0 34h36v2H0z" />
      <path fill="#D1D3D4" d="M3 35h33V6H21c-4 0-5 2-5 2l-4 6S0 19 0 24c0 4 6 6 6 6l-3 5z" />
      <path fill="#231F20" d="M14 35l2-3h20v3z" />
      <path fill="#3B88C3" d="M0 23.999c0 4 6 6 6 6V17.125C3 19 0 21.499 0 23.999zM6 30v-.001V30z" />
      <path fill="#269" d="M6 30l-3 5h33v-5z" />
      <path fill="#3B88C3" d="M20 30l4-6h12v6z" />
      <path fill="#55ACEE" d="M26 8H16l-4 6h-.001 10.843c.477 0 1.108-.448 1.412-1l2.197-4c.303-.552.102-1-.451-1z" />
      <path fill="#3B88C3" d="M25.902 10l.549-1c.303-.552.102-1-.451-1H16l-1.333 2h11.235z" />
    </svg>
  )
}

export function ManagedGatewayLogo({ size }: { size: number }) {
  return <img aria-hidden="true" src={azureGatewaysIcon} width={size} height={size} alt="" />
}

export type GatewayBrand = "apim" | "litellm" | "generic"

export function gatewayBrandFromIdentity(identity: string): GatewayBrand {
  const text = identity.toLowerCase()
  if (text.includes("apim") || text.includes("api management")) return "apim"
  if (text.includes("litellm")) return "litellm"
  return "generic"
}

export function gatewayBrandLabel(brand: GatewayBrand) {
  return { apim: "Azure API Management", litellm: "LiteLLM", generic: "Gateway" }[brand]
}

export function GatewayBrandLogo({ brand, size = 16 }: { brand: GatewayBrand; size?: number }) {
  if (brand === "apim") return <ApimLogo size={size + 1} />
  if (brand === "litellm") return <LiteLlmLogo size={size} />
  return <Network size={size} />
}

export type ProviderBrand = "bedrock" | "foundry" | "microsoft" | "databricks" | "copilot" | "claude" | "openai" | "kimi" | "deepseek" | "generic"

export function providerBrandFromKey(key: BrandKey | undefined): ProviderBrand {
  return {
    amazon_bedrock: "bedrock",
    anthropic: "claude",
    azure_databricks: "databricks",
    github: "copilot",
    microsoft: "microsoft",
    microsoft_foundry: "foundry",
    openai: "openai",
    generic: "generic",
  }[key ?? "generic"] as ProviderBrand
}

export function providerBrandFromIdentity(identity: string): ProviderBrand {
  const text = identity.toLowerCase()
  if (text.includes("deepseek")) return "deepseek"
  if (text.includes("kimi") || text.includes("moonshot")) return "kimi"
  if (text.includes("bedrock") || text.includes("amazon")) return "bedrock"
  if (text.includes("databricks")) return "databricks"
  if (text.includes("copilot") || text.includes("github")) return "copilot"
  if (text.includes("anthropic") || text.includes("claude")) return "claude"
  if (text.includes("foundry")) return "foundry"
  if (text.includes("microsoft") || text.includes("azure")) return "microsoft"
  if (text.includes("openai") || text.includes("gpt")) return "openai"
  return "generic"
}

export function providerBrandFromMetadata(
  key: BrandKey | undefined,
  identity: string,
): ProviderBrand {
  const explicit = providerBrandFromKey(key)
  return explicit !== "generic" ? explicit : providerBrandFromIdentity(identity)
}

export function providerBrandLabel(brand: ProviderBrand) {
  return { bedrock: "Amazon Bedrock", foundry: "Microsoft Foundry", microsoft: "Microsoft", databricks: "Databricks", copilot: "GitHub Copilot", claude: "Anthropic", openai: "OpenAI", kimi: "Kimi", deepseek: "DeepSeek", generic: "Provider" }[brand]
}

export function ProviderBrandLogo({ brand, size = 16 }: { brand: ProviderBrand; size?: number }) {
  if (brand === "bedrock") return <AmazonBedrockLogo size={size} />
  if (brand === "foundry") return <FoundryLogo size={size + 1} />
  if (brand === "microsoft") return <MicrosoftLogo size={size} />
  if (brand === "databricks") return <DatabricksLogo size={size} />
  if (brand === "copilot") return <CopilotLogo size={size} />
  if (brand === "claude") return <ClaudeLogo size={size} />
  if (brand === "openai") return <OpenAiLogo size={size} />
  if (brand === "kimi") return <KimiLogo size={size} />
  if (brand === "deepseek") return <DeepSeekLogo size={size} />
  return <Database size={size} />
}

export function ModelBrandLogo({
  familyKey,
  identity,
  providerName,
  size = 16,
}: {
  familyKey?: string | null
  identity: string
  providerName: string
  size?: number
}) {
  const normalized = `${familyKey ?? ""} ${identity}`.toLowerCase()
  if (normalized.includes("deepseek")) return <DeepSeekLogo size={size} />
  if (normalized.includes("kimi")) return <KimiLogo size={size} />
  if (familyKey === "claude" || normalized.includes("claude")) return <ClaudeLogo size={size} />
  if (familyKey === "openai" || normalized.includes("gpt") || normalized.includes("openai")) return <OpenAiLogo size={size} />
  return <ProviderBrandLogo brand={providerBrandFromIdentity(providerName)} size={size} />
}

export function AccessPointLogo({ point, size = 14 }: { point: RuntimeAccessPoint; size?: number }) {
  const gateway = gatewayBrandFromIdentity(point.title)
  if (gateway !== "generic") return <GatewayBrandLogo brand={gateway} size={size} />
  const provider = providerBrandFromIdentity(point.title)
  if (provider !== "generic") return <ProviderBrandLogo brand={provider} size={size} />
  return point.section === "gateway" ? <Network size={size} /> : <Cloud size={size} />
}

const SHORT_GATEWAY_LABELS: Record<GatewayBrand, string | null> = {
  apim: "APIM",
  litellm: "LiteLLM",
  generic: null,
}

const SHORT_PROVIDER_LABELS: Record<ProviderBrand, string | null> = {
  bedrock: "Bedrock",
  foundry: "Foundry",
  microsoft: "Microsoft",
  databricks: "Databricks",
  // Not "GitHub", and not the bare product name either: today this channel carries exactly
  // one runtime, the CLI, because VS Code Copilot Chat writes no usage anywhere local. A
  // broader label reads as "all my Copilot spend" and understates by an unknown multiple.
  // Widen this back to "Copilot" only when a second runtime actually reports into it.
  copilot: "Copilot CLI",
  claude: "Anthropic",
  openai: "OpenAI",
  kimi: "Kimi",
  deepseek: "DeepSeek",
  generic: null,
}

/**
 * The common short name for an access point, for controls that must fit on one row.
 *
 * These are the vendors' own abbreviations, not invented ones, and an unrecognised channel
 * keeps its full registry title rather than being truncated into something meaningless. The
 * caller is expected to carry the full title in a tooltip.
 */
export function accessPointShortLabel(point: RuntimeAccessPoint): string {
  const gateway = SHORT_GATEWAY_LABELS[gatewayBrandFromIdentity(point.title)]
  if (gateway) return gateway
  const provider = SHORT_PROVIDER_LABELS[providerBrandFromIdentity(point.title)]
  return provider ?? point.title
}
