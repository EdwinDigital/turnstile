import { CircleDot, Cpu, Database, Filter, Network, Plug, Server, Shapes, X } from "lucide-react"

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
  { key: "apim", label: "APIM 实例", icon: Server },
  { key: "status", label: "状态", icon: CircleDot },
] as const

const modelTypes = { chat: "对话", image_generation: "文生图", decision: "Decision" }

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

function optionLabel(dimension: ModelFilterDimension, value: string, connections: ModelRuntime[], gateways: FilterProps["gateways"], apimInstances: FilterProps["apimInstances"]): string {
  if (dimension === "apim" && value.includes(":")) {
    const gateway = gateways.find(item => `${item.targetId}:${item.id}` === value)
    const parent = apimInstances.find(instance => instance.id === gateway?.targetId)
    return gateway ? `${parent?.name ?? gateway.targetId} / ${gateway.name}` : value
  }
  const options = dimension === "connection" ? connections : dimension === "apim" ? apimInstances : null
  return options?.find(option => option.id === value)?.name ?? labels[dimension]?.[value] ?? value
}

type FilterProps = {
  filters: ModelInventoryFilters
  connections: ModelRuntime[]
  gateways: readonly { id: string; name: string; targetId: string }[]
  apimInstances: readonly { id: string; name: string }[]
  onChange: (filters: ModelInventoryFilters) => void
}

export function ModelInventoryFilterMenu({
  filters, connections, gateways, apimInstances, onChange, records, search, compact,
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
          : key === "apim"
            ? apimInstances.filter(instance => records.some(record => record.apim.includes(instance.id))
              || filters.apim.some(value => value === instance.id || value.startsWith(`${instance.id}:`))).map(instance => instance.id)
            : [...new Set([...records.flatMap(record => record[key]), ...filters[key]])]
        const otherFilters = { ...filters, [key]: [] }
        const candidates = records.filter(record => matchesModelFilters(record, otherFilters, search))
        const options = values.map(value => ({
          value, label: optionLabel(key, value, connections, gateways, apimInstances),
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
            {options.map(option => key === "apim" ? <DropdownMenuSub key={option.value}>
              <DropdownMenuSubTrigger className="model-filter-option" title={option.label}>
                <Server size={16} aria-hidden="true" />
                <span className="model-filter-option-label" data-no-localize>{option.label}</span>
                <span className="model-filter-option-count" aria-hidden="true">{option.count}</span>
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent
                className="model-filter-options"
                side={compact ? "bottom" : "right"}
                align={compact ? "end" : "start"}
                alignOffset={compact ? 0 : -3}
                sideOffset={compact ? 4 : 0}
              >
                <DropdownMenuCheckboxItem
                  checked={filters.apim.includes(option.value)}
                  closeOnClick={false}
                  onCheckedChange={() => onChange(toggleModelFilter(filters, "apim", option.value))}
                  className="model-filter-option"
                  title={option.label}
                >
                  <span className="model-filter-option-label">全部</span>
                  <span className="model-filter-option-count" aria-hidden="true">{option.count}</span>
                </DropdownMenuCheckboxItem>
                <DropdownMenuSeparator />
                {gateways.filter(gateway => gateway.targetId === option.value).map(gateway => {
                  const value = `${gateway.targetId}:${gateway.id}`
                  const count = candidates.filter(record => record.apim.includes(value)).length
                  return <DropdownMenuCheckboxItem
                    key={value}
                    checked={filters.apim.includes(value)}
                    closeOnClick={false}
                    onCheckedChange={() => onChange(toggleModelFilter(filters, "apim", value))}
                    className="model-filter-option"
                    title={`${option.label} / ${gateway.name}`}
                  >
                    <Network size={16} aria-hidden="true" />
                    <span className="model-filter-option-label" data-no-localize>{gateway.name}</span>
                    <span className="model-filter-option-count" aria-hidden="true">{count}</span>
                  </DropdownMenuCheckboxItem>
                })}
              </DropdownMenuSubContent>
            </DropdownMenuSub> : <DropdownMenuCheckboxItem
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

export function ModelInventoryFilterChips({ filters, connections, gateways, apimInstances, onChange }: FilterProps) {
  if (!dimensions.some(({ key }) => filters[key].length > 0)) return null
  return <div className="model-filter-chips" role="group" aria-label="已选筛选条件">
    <div className="model-filter-chip-list">
      {dimensions.filter(({ key }) => filters[key].length > 0).map(({ key, label, icon: Icon }) => {
        const selected = filters[key]
        const selectedLabels = selected.map(value => optionLabel(key, value, connections, gateways, apimInstances))
        return <div key={key} className="model-filter-chip" data-filter-dimension={key}>
          <Icon size={14} aria-hidden="true" />
          <span className="model-filter-chip-dimension">{label}</span>
          {key === "family" && <span className="model-filter-chip-icons">
            {selected.slice(0, 3).map(value => <ModelFamilyIcon key={value} family={value} size={14} />)}
          </span>}
          {(key === "platform" || key === "connection") && <span className="model-filter-chip-icons">
            {selected.slice(0, 3).map(value => <ModelPlatformIcon key={value} platform={key === "platform" ? value : modelInventoryPlatform(connections.find(connection => connection.id === value))} size={14} />)}
          </span>}
          <span className="model-filter-chip-value" title={selectedLabels.join(", ")} data-no-localize={(key === "connection" || key === "apim") && selected.length === 1 || undefined}>
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