import { CircleDot, Cpu, Database, Filter, Plug, Shapes, X } from "lucide-react"

import type { ModelRuntime } from "../../data-sources/apim/types"
import {
  emptyModelFilters,
  matchesModelFilters,
  modelInventoryPlatform,
  toggleModelFilter,
  type ModelFilterDimension,
  type ModelFilterRecord,
  type ModelInventoryFilters,
} from "../../lib/model-inventory-filters"
import { ClaudeLogo, DeepSeekLogo, KimiLogo, OpenAiLogo, ProviderBrandLogo, type ProviderBrand } from "../brand-logos"
import { Button } from "../ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu"

const dimensions = [
  { key: "family", label: "模型家族", icon: Cpu },
  { key: "platform", label: "接入平台", icon: Database },
  { key: "capability", label: "模型类型", icon: Shapes },
  { key: "connection", label: "连接", icon: Plug },
  { key: "status", label: "状态", icon: CircleDot },
] as const

const modelTypes = { chat: "对话", image_generation: "文生图" }

const labels: Partial<Record<ModelFilterDimension, Record<string, string>>> = {
  family: { openai: "GPT / OpenAI", claude: "Claude", deepseek: "DeepSeek", kimi: "Kimi", generic: "未分类" },
  platform: {
    microsoft_foundry: "Microsoft Foundry", azure_databricks: "Azure Databricks",
    amazon_bedrock: "Amazon Bedrock", openai: "OpenAI", anthropic: "Anthropic",
    openai_compatible: "OpenAI-compatible", microsoft: "Microsoft", generic: "其他", unknown: "未识别",
  },
  capability: modelTypes,
  status: { enabled: "启用中", disabled: "已停用", pending: "发布中", failed: "发布失败" },
}

const familyIcons: Record<string, typeof OpenAiLogo> = {
  claude: ClaudeLogo, deepseek: DeepSeekLogo, openai: OpenAiLogo, kimi: KimiLogo,
}

const platformBrands: Record<string, ProviderBrand> = {
  amazon_bedrock: "bedrock", azure_databricks: "databricks", microsoft_foundry: "foundry",
  openai: "openai", anthropic: "claude", microsoft: "microsoft",
}

function ModelFamilyIcon({ family, size }: { family: string; size: number }) {
  const Icon = familyIcons[family] ?? Cpu
  return <span className="model-filter-family-icon" data-model-family={family} aria-hidden="true"><Icon size={size} /></span>
}

function ModelPlatformIcon({ platform, size }: { platform: string; size: number }) {
  return <span className="model-filter-platform-icon" data-model-platform={platform} aria-hidden="true"><ProviderBrandLogo brand={platformBrands[platform] ?? "generic"} size={size} /></span>
}

function optionLabel(dimension: ModelFilterDimension, value: string, connections: ModelRuntime[]): string {
  return dimension === "connection"
    ? connections.find(connection => connection.id === value)?.name ?? value
    : labels[dimension]?.[value] ?? value
}

type FilterProps = {
  filters: ModelInventoryFilters
  connections: ModelRuntime[]
  onChange: (filters: ModelInventoryFilters) => void
}

export function ModelInventoryFilterMenu({
  filters, connections, onChange, records, search, compact,
}: FilterProps & { records: ModelFilterRecord[]; search: string; compact: boolean }) {
  const activeCount = dimensions.filter(({ key }) => filters[key].length > 0).length

  return <DropdownMenu>
    <DropdownMenuTrigger render={<Button
      type="button"
      variant={activeCount ? "default" : "outline"}
      size="sm"
      className="model-filter-trigger"
      data-active={activeCount > 0}
      aria-label="筛选模型"
      title="筛选模型"
    />}>
      <Filter size={14} aria-hidden="true" />
      <span>{activeCount ? `${activeCount} 个筛选` : "筛选"}</span>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end" className="model-filter-menu">
      {dimensions.map(({ key, label, icon: Icon }) => {
        const values = key === "capability"
          ? Object.keys(modelTypes)
          : [...new Set([...records.flatMap(record => record[key]), ...filters[key]])]
        const otherFilters = { ...filters, [key]: [] }
        const candidates = records.filter(record => matchesModelFilters(record, otherFilters, search))
        const options = values.map(value => ({
          value, label: optionLabel(key, value, connections),
          count: candidates.filter(record => record[key].includes(value)).length,
        })).sort((left, right) => left.label.localeCompare(right.label))

        return <DropdownMenuSub key={key}>
          <DropdownMenuSubTrigger className="model-filter-category" disabled={!options.length}>
            <Icon size={16} aria-hidden="true" />
            <span className="model-filter-label">{label}</span>
            {filters[key].length > 0 && <span className="model-filter-selected-count">{filters[key].length}</span>}
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent
            className="model-filter-options"
            side={compact ? "bottom" : "right"}
            align={compact ? "end" : "start"}
            alignOffset={compact ? 0 : -3}
            sideOffset={compact ? 4 : 0}
          >
            {options.map(option => <DropdownMenuCheckboxItem
              key={option.value}
              checked={filters[key].includes(option.value)}
              closeOnClick={false}
              onCheckedChange={() => onChange(toggleModelFilter(filters, key, option.value))}
              className="model-filter-option"
              title={option.label}
            >
              {key === "family" && <ModelFamilyIcon family={option.value} size={16} />}
              {key === "platform" && <ModelPlatformIcon platform={option.value} size={16} />}
              {key === "connection" && <ModelPlatformIcon platform={modelInventoryPlatform(connections.find(connection => connection.id === option.value))} size={16} />}
              <span className="model-filter-option-label" data-no-localize={key === "connection" || undefined}>{option.label}</span>
              <span className="model-filter-option-count" aria-hidden="true">{option.count}</span>
            </DropdownMenuCheckboxItem>)}
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      })}
      {activeCount > 0 && <>
        <DropdownMenuSeparator />
        <DropdownMenuItem className="model-filter-clear" onClick={() => onChange(emptyModelFilters())}>
          <X size={16} aria-hidden="true" /><span>清除筛选</span>
        </DropdownMenuItem>
      </>}
    </DropdownMenuContent>
  </DropdownMenu>
}

export function ModelInventoryFilterChips({ filters, connections, onChange }: FilterProps) {
  if (!dimensions.some(({ key }) => filters[key].length > 0)) return null
  return <div className="model-filter-chips" role="group" aria-label="已选筛选条件">
    <div className="model-filter-chip-list">
      {dimensions.filter(({ key }) => filters[key].length > 0).map(({ key, label, icon: Icon }) => {
        const selected = filters[key]
        const selectedLabels = selected.map(value => optionLabel(key, value, connections))
        return <div key={key} className="model-filter-chip" data-filter-dimension={key}>
          <Icon size={14} aria-hidden="true" />
          <span className="model-filter-chip-dimension">{label}</span>
          {key === "family" && <span className="model-filter-chip-icons">
            {selected.slice(0, 3).map(value => <ModelFamilyIcon key={value} family={value} size={14} />)}
          </span>}
          {(key === "platform" || key === "connection") && <span className="model-filter-chip-icons">
            {selected.slice(0, 3).map(value => <ModelPlatformIcon key={value} platform={key === "platform" ? value : modelInventoryPlatform(connections.find(connection => connection.id === value))} size={14} />)}
          </span>}
          <span className="model-filter-chip-value" title={selectedLabels.join(", ")} data-no-localize={key === "connection" && selected.length === 1 || undefined}>
            {selected.length === 1 ? selectedLabels[0] : `${selected.length} 项`}
          </span>
          <button
            type="button"
            className="model-filter-chip-remove"
            aria-label={`移除筛选：${label}`}
            title={`移除筛选：${label}`}
            onClick={() => onChange({ ...filters, [key]: [] })}
          ><X size={12} aria-hidden="true" /></button>
        </div>
      })}
    </div>
    <button type="button" className="model-filter-reset" onClick={() => onChange(emptyModelFilters())}>清除筛选</button>
  </div>
}