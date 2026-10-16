import { Database } from "lucide-react"

import type { ModelVendorKey } from "../../data-sources/apim/types"
import {
  ClaudeLogo,
  DeepSeekLogo,
  KimiLogo,
  OpenAiLogo,
} from "../brand-logos"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"

const MODEL_VENDOR_OPTIONS: Array<{ value: ModelVendorKey; label: string }> = [
  { value: "kimi", label: "Kimi" },
  { value: "deepseek", label: "DeepSeek" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "generic", label: "其他服务商" },
]

export function modelVendorLabel(value: ModelVendorKey) {
  return MODEL_VENDOR_OPTIONS.find((option) => option.value === value)?.label
    ?? "其他服务商"
}

export function ModelVendorLogo({ value, size = 16 }: { value: ModelVendorKey; size?: number }) {
  if (value === "kimi") return <KimiLogo size={size} />
  if (value === "deepseek") return <DeepSeekLogo size={size} />
  if (value === "openai") return <OpenAiLogo size={size} />
  if (value === "anthropic") return <ClaudeLogo size={size} />
  return <Database aria-hidden="true" size={size} />
}

export function modelVendorFromMetadata(config: Record<string, unknown> | undefined, identity = ""): ModelVendorKey {
  const configured = config?.model_vendor
  if (configured === "generic" || configured === "kimi" || configured === "deepseek" || configured === "openai" || configured === "anthropic") {
    return configured
  }
  const normalized = identity.toLowerCase().replaceAll("openai-compatible", "")
  if (normalized.includes("deepseek")) return "deepseek"
  if (normalized.includes("kimi") || normalized.includes("moonshot")) return "kimi"
  if (normalized.includes("anthropic") || normalized.includes("claude")) return "anthropic"
  if (normalized.includes("openai") || normalized.includes("gpt")) return "openai"
  return "generic"
}

export function ModelVendorSelect({
  value,
  disabled,
  onChange,
}: {
  value: ModelVendorKey
  disabled?: boolean
  onChange: (value: ModelVendorKey) => void
}) {
  return <Select value={value} onValueChange={(next) => next && onChange(next as ModelVendorKey)} disabled={disabled}>
    <SelectTrigger className="registry-select-trigger" aria-label="API 服务商">
      <SelectValue><span className="registry-option"><ModelVendorLogo value={value} size={15} /><span>{modelVendorLabel(value)}</span></span></SelectValue>
    </SelectTrigger>
    <SelectContent align="start" alignItemWithTrigger={false}>
      {MODEL_VENDOR_OPTIONS.map((option) => <SelectItem key={option.value} value={option.value}>
        <span className="registry-option"><ModelVendorLogo value={option.value} size={15} /><span>{option.label}</span></span>
      </SelectItem>)}
    </SelectContent>
  </Select>
}