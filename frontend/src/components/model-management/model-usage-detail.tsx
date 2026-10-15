import { useState, type MouseEvent, type ReactNode } from "react"
import { useQuery } from "@tanstack/react-query"
import { Activity, Bot, Building2, ChevronRight, Cpu, Folder, Network, PanelLeft, RefreshCw, Server, TriangleAlert, UserRound, type LucideIcon } from "lucide-react"
import { Bar, BarChart, CartesianGrid, LabelList, Rectangle, ResponsiveContainer, Tooltip, XAxis, YAxis, type BarShapeProps } from "recharts"

import { usageWindow } from "../../data-sources/apim/api"
import { finopsQueries } from "../../data-sources/apim/queries"
import type { DistributionDimension, DistributionItem, ManagedModel, ModelRegistry, UsageFilters } from "../../data-sources/apim/types"
import { FINOPS_NAVIGATE_EVENT } from "../../lib/navigation"
import { hasModelPrices } from "../../lib/pricing"
import { getIntlLocale } from "../../locales/index"
import { useTimezone } from "../../providers/timezone-provider"
import { FoundryLogo } from "../brand-logos"
import { CategoryAxisTick, categoryAxisWidth, categoryTickGutter, useChartWidthKey } from "../finops/category-axis"
import { FinOpsChartTooltip } from "../finops/chart-tooltip"
import { aggregateActivity, UsageActivityCard } from "../finops/usage-activity-card"
import { Button } from "../ui/button"
import { ButtonGroup } from "../ui/button-group"
import { ResizableTable } from "../ui/resizable-table"
import { ModelVendorLogo, modelVendorFromMetadata } from "./model-vendor-select"
import "../../styles/model-usage-detail.css"

export function modelDetailHref(modelId: string | null, address = window.location.href) {
  const url = new URL(address)
  url.searchParams.set("source", "apim")
  url.searchParams.set("page", "models")
  url.searchParams.set("tab", "models")
  for (const field of ["runtime", "publication", "release", "consumer", "application", "request"]) {
    url.searchParams.delete(field)
  }
  if (modelId) url.searchParams.set("model", modelId)
  else url.searchParams.delete("model")
  return `${url.pathname}${url.search}`
}

export function navigateToModelDetailHref(href: string) {
  window.history.pushState(null, "", href)
  window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
}

export function openModelDetailLink(event: MouseEvent<HTMLAnchorElement>) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  navigateToModelDetailHref(event.currentTarget.href)
}

const fullCount = (value: number) => value.toLocaleString(getIntlLocale())
const compactCount = (value: number) => new Intl.NumberFormat(getIntlLocale(), { notation: "compact", maximumFractionDigits: 1 }).format(value)
const cost = (value: number) => new Intl.NumberFormat(getIntlLocale(), { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(value)

function ReadState({ failed, onRetry }: { failed?: boolean; onRetry?: () => void }) {
  return <div className={`application-card-empty model-usage-state${failed ? " error" : ""}`} role={failed ? "alert" : "status"}>
    {failed ? <TriangleAlert size={16} /> : <RefreshCw className="spin" size={16} />}
    <span>{failed ? "用量数据加载失败" : "正在加载用量数据"}</span>
    {failed && onRetry && <Button type="button" variant="ghost" size="icon-sm" aria-label="重试" title="重试" onClick={onRetry}><RefreshCw size={14} /></Button>}
  </div>
}

function Metric({ name, label, value }: { name: string; label: string; value: string }) {
  return <div className="application-metric"><span>{label}</span><b data-metric={name} data-no-localize title={value}>{value}</b></div>
}

const modelDimensions = [
  { dimension: "department", label: "部门", title: "部门 Token 排行", icon: Building2, color: "var(--chart-4)" },
  { dimension: "project", label: "项目", title: "项目 Token 排行", icon: Folder, color: "var(--chart-2)" },
  { dimension: "agent", label: "智能体", title: "智能体 Token 排行", icon: Bot, color: "var(--chart-3)" },
  { dimension: "organization", label: "组织", title: "组织 Token 排行", icon: Network, color: "var(--muted-foreground)" },
] as const

function ModelDimensionRanking({ filters, days }: { filters: UsageFilters & { model_id: string }; days: number }) {
  const [selected, setSelected] = useState<(typeof modelDimensions)[number]["dimension"]>("department")
  const dimension = modelDimensions.find(item => item.dimension === selected) ?? modelDimensions[0]
  return <ModelUsageRanking {...dimension} filters={filters} days={days} chartHeight={382} controls={
    <div className="model-dimension-controls">
      <ButtonGroup className="trend-segment model-dimension-switch" aria-label="维度">
        {modelDimensions.map(item => <button key={item.dimension} type="button" data-dimension-option={item.dimension}
          className={selected === item.dimension ? "active" : ""} aria-pressed={selected === item.dimension}
          onClick={() => setSelected(item.dimension)}>{item.label}</button>)}
      </ButtonGroup>
    </div>
  } />
}

export function modelUsageRankingRows(items: DistributionItem[]) {
  return [...items].sort((left, right) => right.total_tokens - left.total_tokens || left.id.localeCompare(right.id)).slice(0, 10)
}

function ModelUsageRanking({ filters, days, dimension, title, icon: Icon, color, controls, chartHeight }: {
  filters: UsageFilters & { model_id: string }
  days: number
  dimension: DistributionDimension
  title: string
  icon: LucideIcon
  color: string
  controls?: ReactNode
  chartHeight?: number
}) {
  const ranking = useQuery(finopsQueries.distribution(filters, dimension, undefined, "fixed"))
  const rows = modelUsageRankingRows(ranking.error ? [] : ranking.data?.items ?? [])
  return <section className={`application-card model-usage-ranking${controls ? " model-dimension-ranking" : ""}`} data-dimension={dimension}>
    <header className="application-card-head"><div><Icon size={14} /><h2>{title}</h2></div>{controls}<span data-no-localize>Top 10 · {days}d</span></header>
    {ranking.isLoading ? <ReadState /> : ranking.error ? <ReadState failed onRetry={() => { void ranking.refetch() }} />
      : rows.length === 0 ? <div className="application-card-empty">所选范围暂无调用</div>
      : <ModelUsageRankingChart rows={rows} title={title} color={color} height={chartHeight} />}
  </section>
}

function ModelUsageRankingChart({ rows, title, color, height }: { rows: DistributionItem[]; title: string; color: string; height?: number }) {
  const [chartRef, chartWidth] = useChartWidthKey()
  const labelWidth = categoryAxisWidth(rows.map(row => row.name), chartWidth)
  return <div ref={chartRef} className="model-ranking-chart finops-chart-surface" style={{ height: height ?? Math.max(220, rows.length * 34 + 42) }} aria-label={title}>
        <ResponsiveContainer key={chartWidth} width="100%" height="100%">
          <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 16, bottom: 0, left: 0 }} barCategoryGap="24%">
            <CartesianGrid horizontal={false} stroke="var(--border)" />
            <XAxis type="number" axisLine={false} tickLine={false} tickMargin={8} allowDecimals={false}
              domain={[0, (maximum: number) => Math.max(1, maximum * 1.4)]} tickFormatter={value => compactCount(Number(value))} />
            <YAxis type="category" dataKey="name" interval={0} allowDuplicatedCategory axisLine={false} tickLine={false} tickMargin={8}
              width={labelWidth} tick={<CategoryAxisTick maxWidth={labelWidth - categoryTickGutter} />} />
            <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={() => "Token"}
              valueFormatter={(value, _name, payload) => `${fullCount(Number(value))} tokens · ${Number((payload as DistributionItem | undefined)?.share_percent ?? 0).toLocaleString(getIntlLocale(), { maximumFractionDigits: 1 })}%`} />} />
            <Bar dataKey="total_tokens" fill={color} radius={[0, 3, 3, 0]} maxBarSize={22} isAnimationActive={false}
              shape={(props: BarShapeProps) => <Rectangle {...props} data-ranking-id={props.payload.id} data-tokens={props.payload.total_tokens}>
                <title>{`${props.payload.name}: ${fullCount(props.payload.total_tokens)} tokens`}</title>
              </Rectangle>}>
              <LabelList dataKey="total_tokens" position="right" formatter={value => compactCount(Number(value))} fill="var(--muted-foreground)" fontSize={10} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
}

export function ModelUsageDetail({ modelId, registry, onToggleSidebar }: {
  modelId: string
  registry: ModelRegistry
  onToggleSidebar: () => void
}) {
  const model = registry.models.find(item => item.id === modelId)
  return <div className="application-detail-workspace model-detail-workspace" data-model-id={modelId}>
    <header className="smh-page-header application-detail-breadcrumb">
      <div>
        <Button type="button" variant="ghost" size="icon-sm" className="model-mobile-sidebar-toggle" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button>
        <span className="smh-header-icon"><Cpu size={17} aria-hidden="true" /></span>
        <nav className="application-breadcrumb-trail" aria-label="模型">
          <a data-model-back href={modelDetailHref(null)} onClick={openModelDetailLink}>模型</a>
          <ChevronRight size={13} aria-hidden="true" />
          <code aria-current="page" data-no-localize title={model?.display_name ?? modelId}>{model?.display_name ?? modelId}</code>
        </nav>
      </div>
    </header>
    {model ? <ModelUsageContent key={model.id} model={model} registry={registry} />
      : <div className="smh-empty application-detail-error"><Cpu size={28} /><b>模型不存在</b><a className="ghost-button" href={modelDetailHref(null)} onClick={openModelDetailLink}>返回模型列表</a></div>}
  </div>
}

function ModelUsageContent({ model, registry }: { model: ManagedModel; registry: ModelRegistry }) {
  const { timezone } = useTimezone()
  const [range, setRange] = useState(() => ({ days: 30, window: usageWindow(30) }))
  const filters: UsageFilters & { model_id: string } = { ...range.window, model_id: model.id }
  const overview = useQuery(finopsQueries.executiveOverview(filters, "fixed"))
  const activity = useQuery(finopsQueries.usageActivity(filters, "hour", timezone, "fixed"))
  const requests = useQuery(finopsQueries.requests(filters, "fixed", 10))
  const runtime = registry.runtimes.find(item => item.id === model.runtime_id)
  const totals = overview.error ? undefined : overview.data?.totals
  const bucketTotals = activity.error || !activity.data ? undefined : aggregateActivity(activity.data.points, "day", timezone)
    .reduce((total, row) => ({ input: total.input + row.input, output: total.output + row.output,
      cached: total.cached + row.cached, cacheWrite: total.cacheWrite + row.cacheWrite }), { input: 0, output: 0, cached: 0, cacheWrite: 0 })
  const recent = requests.error ? [] : (requests.data ?? []).slice(0, 10)
  const vendor = modelVendorFromMetadata(runtime?.config ?? {}, `${model.model_key} ${model.display_name}`)
  const foundryRouter = runtime?.runtime_kind === "foundry" && model.upstream_model_id === "model-router"
  const timestamp = (value: string) => new Date(value).toLocaleString(getIntlLocale(), {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: timezone,
  })
  const retrySummary = () => { void overview.refetch(); void activity.refetch() }

  return <div className="application-detail-scroll">
    <div className="application-detail-grid">
      <div className="application-detail-main">
        <section className="application-summary-card">
          <header className="application-detail-head">
            <span className="application-detail-icon">{foundryRouter ? <FoundryLogo size={22} /> : <ModelVendorLogo value={vendor} size={24} />}</span>
            <div className="application-identity">
              <div><h2 title={model.display_name} data-no-localize>{model.display_name}</h2></div>
              <p><code data-no-localize title={model.model_key}>{model.model_key}</code></p>
            </div>
          </header>
          <dl className="application-facts">
            <div><dt>提供方</dt><dd data-no-localize title={model.provider_name}>{model.provider_name}</dd></div>
            <div><dt>连接</dt><dd data-no-localize title={model.runtime_name}>{model.runtime_name}</dd></div>
            <div><dt>上下文窗口</dt><dd data-no-localize>{model.context_window == null ? "—" : fullCount(model.context_window)}</dd></div>
            <div><dt>启用状态</dt><dd>{model.enabled ? "已启用" : "已停用"}</dd></div>
          </dl>
        </section>

        <div className="application-usage-controls model-usage-controls">
          <div><span>时间范围</span><div role="group" aria-label="时间范围">{[7, 30, 90].map(days =>
            <button key={days} type="button" className={range.days === days ? "active" : ""} aria-pressed={range.days === days}
              onClick={() => { if (range.days !== days) setRange({ days, window: usageWindow(days) }) }}>{days}d</button>,
          )}</div></div>
        </div>

        <section className="application-card application-usage-card" aria-busy={overview.isLoading || activity.isLoading}>
          <header className="application-card-head"><div><Activity size={14} /><h2>用量</h2></div><span data-no-localize>{range.days}d</span></header>
          {overview.isLoading ? <ReadState /> : overview.error || !totals ? <ReadState failed onRetry={retrySummary} /> : <>
            <div className="application-metric-grid">
              <Metric name="total_tokens" label="总 Tokens" value={fullCount(totals.total_tokens)} />
              <Metric name="input_tokens" label="输入" value={bucketTotals ? fullCount(bucketTotals.input) : "—"} />
              <Metric name="cache_read_tokens" label="缓存读取" value={bucketTotals ? fullCount(bucketTotals.cached) : "—"} />
              <Metric name="cache_write_tokens" label="缓存写入" value={bucketTotals ? fullCount(bucketTotals.cacheWrite) : "—"} />
              <Metric name="output_tokens" label="输出" value={bucketTotals ? fullCount(bucketTotals.output) : "—"} />
              <Metric name="total_requests" label="请求数" value={fullCount(totals.total_requests)} />
              <Metric name="estimated_cost" label="估算费用" value={cost(totals.estimated_cost)} />
              <Metric name="failed_requests" label="失败请求" value={fullCount(totals.client_error_requests + totals.server_error_requests)} />
              <Metric name="average_latency_ms" label="平均时延" value={`${Math.round(totals.average_latency_ms).toLocaleString(getIntlLocale())} ms`} />
            </div>
            {activity.error && <ReadState failed onRetry={() => { void activity.refetch() }} />}
          </>}
        </section>

        <UsageActivityCard filters={filters} costAvailable={hasModelPrices(model) || (totals?.estimated_cost ?? 0) > 0} windowMode="fixed" />

        <ModelDimensionRanking filters={filters} days={range.days} />

        <section className="application-card model-recent-requests">
          <header className="application-card-head"><div><Activity size={14} /><h2>最近请求列表</h2></div><span data-no-localize>{recent.length} / {totals ? fullCount(totals.total_requests) : "—"}</span></header>
          {requests.isLoading ? <ReadState /> : requests.error ? <ReadState failed onRetry={() => { void requests.refetch() }} />
            : recent.length === 0 ? <div className="application-card-empty">所选范围暂无调用</div>
            : <div className="model-request-table-scroll"><ResizableTable minWidths={[110, 110, 60, 95, 95]}>
              <thead><tr><th>时间</th><th>使用者</th><th>状态</th><th>总 Tokens</th><th>估算费用</th></tr></thead>
              <tbody>{recent.map(request => {
                const traceId = request.trace_id ?? request.correlation_id ?? request.request_id
                const address = `/?source=apim&page=finops-requests&request=${encodeURIComponent(traceId)}`
                return <tr key={traceId}>
                  <td><a href={address} onClick={openModelDetailLink} title={traceId}><time dateTime={request.timestamp} data-no-localize>{timestamp(request.timestamp)}</time></a></td>
                  <td><span data-no-localize title={request.user_name}>{request.user_name}</span></td>
                  <td><span className={(request.status_code ?? 0) >= 400 ? "model-request-failed" : ""} data-no-localize>{request.status_code ?? "—"}</span></td>
                  <td data-no-localize>{request.usage_available === false ? "—" : fullCount(request.total_tokens)}</td>
                  <td>{request.cost_available === false || request.usage_available === false ? "未计价" : <span data-no-localize>{cost(request.estimated_cost)}</span>}</td>
                </tr>
              })}</tbody>
            </ResizableTable></div>}
        </section>
      </div>

      <aside className="application-detail-rail">
        <ModelUsageRanking filters={filters} days={range.days} dimension="user" title="使用者 Token 排行" icon={UserRound} color="var(--chart-1)" />

        <section className="application-card application-governance-card model-configuration-card">
          <header className="application-card-head"><div><Server size={14} /><h2>模型配置</h2></div></header>
          <dl>
            <div><dt>提供方</dt><dd data-no-localize>{model.provider_name}</dd></div>
            <div><dt>连接</dt><dd data-no-localize>{model.runtime_name}</dd></div>
            <div><dt>网关</dt><dd data-no-localize>{runtime?.gateway_name ?? "—"}</dd></div>
            <div><dt>默认模型</dt><dd>{model.is_default ? "是默认模型" : "非默认模型"}</dd></div>
            <div><dt>模型 ID</dt><dd><code data-no-localize>{model.id}</code></dd></div>
          </dl>
        </section>
      </aside>
    </div>
  </div>
}