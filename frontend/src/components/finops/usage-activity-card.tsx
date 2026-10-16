import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  Rectangle,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type BarShapeProps,
} from "recharts"

import { timezoneBucketKey } from "../../lib/timezone"
import { getIntlLocale } from "../../locales/index"
import { finopsQueries, withWindowDays } from "../../data-sources/apim/queries"
import { useTimezone } from "../../providers/timezone-provider"
import type { ManagedModel, TrendApiResponse, UsageFilters } from "../../data-sources/apim/types"
import { ActivityHeatmap, type ActivityDay, type ActivityMetric } from "./activity-heatmap"
import { ChartSeriesLegend } from "./chart-legend"
import { ChartZoomReset, useChartDragZoom } from "./chart-drag-zoom"
import { ChartModeToggle, type ChartMode } from "./chart-mode-toggle"
import { FinOpsChartTooltip } from "./chart-tooltip"
const compact = { format: (value: number) => new Intl.NumberFormat(getIntlLocale(), { notation: "compact", maximumFractionDigits: 1 }).format(value) }
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 })

type UsageSeriesKey = "input" | "output" | "cached" | "cacheWrite"

/* Cache Read takes the brand blue because it is the largest series by far -- around 90% of the
   tokens -- so it is the mass that sets the chart's overall colour, and the product's own
  colour is the right one for that. Input takes green, output orange, and Cache Write neutral gray.
  Keep this table and the runtime detail chart in model-management-page.tsx in agreement --
  "input" must not be green here and blue there. */
const usageSeries: Array<{ key: UsageSeriesKey; label: string; color: string }> = [
  { key: "input", label: "输入", color: "var(--chart-4)" },
  { key: "output", label: "输出", color: "var(--chart-2)" },
  { key: "cached", label: "缓存读取", color: "var(--chart-1)" },
  { key: "cacheWrite", label: "缓存写入", color: "var(--chart-cache-write)" },
]

export function UsageActivityCard({ filters, costAvailable, windowMode = "rolling" }: {
  filters: UsageFilters
  costAvailable: boolean
  windowMode?: "rolling" | "fixed"
}) {
  const { timezone } = useTimezone()
  const [metric, setMetric] = useState<ActivityMetric>("tokens")
  const [grain, setGrain] = useState<"hour" | "day">("hour")
  const [chartMode, setChartMode] = useState<ChartMode>("bar")
  const showHeatmap = chartMode === "heatmap"
  const shape = chartMode === "line" ? "line" : "bar"
  const [hiddenSeries, setHiddenSeries] = useState<UsageSeriesKey[]>([])
  const toggleSeries = (key: string) =>
    setHiddenSeries((current) => current.includes(key as UsageSeriesKey)
      ? current.filter((item) => item !== key)
      : [...current, key as UsageSeriesKey])
  const activeSeries = usageSeries.filter((series) => !hiddenSeries.includes(series.key))
  // Hourly buckets answer both grains: the server already bucketed them in the viewer's zone, so
  // truncating an hourly bucket to its own date is exact and keeps the grain toggle free of a
  // second request.
  const activity = useQuery(finopsQueries.usageActivity(filters, "hour", timezone, windowMode))
  const heatmapFilters = useMemo<UsageFilters>(() => withWindowDays(filters, 182, windowMode), [filters, windowMode])
  const heatmapActivity = useQuery({
    ...finopsQueries.usageActivity(heatmapFilters, "day", timezone, windowMode),
    enabled: showHeatmap,
  })
  const points = activity.data?.points
  const chartRows = useMemo(
    () => aggregateActivity(points ?? [], grain, timezone),
    [grain, points, timezone],
  )
  const heatmapPoints = heatmapActivity.data?.points
  const heatmapDays = useMemo<ActivityDay[]>(
    () => aggregateActivity(heatmapPoints ?? [], "day", timezone)
      .map((row) => ({ date: row.date, value: metric === "cost" ? row.cost : row.total })),
    [heatmapPoints, metric, timezone],
  )
  const query = showHeatmap ? heatmapActivity : activity
  const visibleRows = chartRows.filter((row) => metric === "cost"
    ? row.cost > 0
    : activeSeries.reduce((sum, series) => sum + row[series.key], 0) > 0)
  const hasCost = chartRows.some((row) => row.cost > 0)
  const hasTokens = chartRows.some((row) => row.total > 0)

  return <section className="finops-panel span-3 usage-activity-card">
    <header className="usage-activity-head">
      <div className="usage-activity-title">
        <h2>用量时段分布</h2>
        <ChartModeToggle value={chartMode} onChange={setChartMode} />
        {showHeatmap && <span className="usage-activity-meta">{`最近 26 周 · 每日 ${metric === "cost" ? "$" : "Token"} 强度（此处忽略上方时间范围）`}</span>}
      </div>
      {!showHeatmap && <div className="usage-activity-controls">
        <div className="usage-metric-segment" aria-label="图表指标">
          <button type="button" className={metric === "cost" ? "active" : ""} onClick={() => setMetric("cost")}>费用</button>
          <button type="button" className={metric === "tokens" ? "active" : ""} onClick={() => setMetric("tokens")}>Token</button>
        </div>
        <div className="usage-metric-segment" aria-label="图表粒度">
          <button type="button" className={grain === "hour" ? "active" : ""} onClick={() => setGrain("hour")}>小时</button>
          <button type="button" className={grain === "day" ? "active" : ""} onClick={() => setGrain("day")}>每日</button>
        </div>
      </div>}
      {!showHeatmap && <ChartLegend metric={metric} hidden={hiddenSeries} onToggle={toggleSeries} />}
    </header>

    <div className="usage-activity-body">
      {query.isLoading ? <ChartState>正在加载用量数据</ChartState>
        : query.error ? <ChartState>用量数据加载失败</ChartState>
        : showHeatmap ? <ActivityHeatmap days={heatmapDays} metric={metric} timezone={timezone} />
        : metric === "cost" && !costAvailable ? <UnpricedState reason="missing-prices" />
        : metric === "cost" && hasTokens && !hasCost ? <UnpricedState reason="pending-repricing" />
        : metric === "cost" && !hasCost ? <ChartState>当前筛选范围暂无费用数据</ChartState>
        : metric === "tokens" && !hasTokens ? <ChartState>当前筛选范围暂无 Token 用量</ChartState>
        : <DailyUsageChart
            key={`${grain}:${metric}:${timezone}:${JSON.stringify(filters)}`}
            rows={visibleRows}
            metric={metric}
            series={activeSeries}
            shape={shape}
          />}
    </div>
  </section>
}

type DailyUsage = {
  date: string
  label: string
  input: number
  output: number
  cached: number
  cacheWrite: number
  total: number
  cost: number
}

function bucketLabel(bucketStart: string, grain: "hour" | "day" | "week", timezone: string) {
  const bucket = new Date(bucketStart)
  return grain === "hour"
    ? bucket.toLocaleString(getIntlLocale(), { month: "numeric", day: "numeric", hour: "2-digit", hour12: false, timeZone: timezone })
    : bucket.toLocaleDateString(getIntlLocale(), { month: "numeric", day: "numeric", timeZone: timezone })
}

/**
 * Folds the model-grouped trend buckets into one row per period. Cost comes from the bucket's
 * own `estimated_cost`, which the server sums from the per-row price snapshot, so a period is
 * priced the way its requests were priced when they landed and agrees with the executive KPI.
 */
export function aggregateActivity(
  points: TrendApiResponse["points"],
  grain: "hour" | "day" | "week",
  timezone: string,
) {
  const rows = new Map<string, DailyUsage>()
  for (const point of points) {
    // Weekly buckets are already truncated to their Monday by the server, so the day key is
    // unique per week; only the hourly grain needs the finer key.
    const date = timezoneBucketKey(point.bucket_start, grain === "hour" ? "hour" : "day", timezone)
    const row = rows.get(date) ?? {
      date,
      label: bucketLabel(point.bucket_start, grain, timezone),
      input: 0,
      output: 0,
      cached: 0,
      cacheWrite: 0,
      total: 0,
      cost: 0,
    }
    row.input += point.totals.input_tokens
    row.output += point.totals.output_tokens
    // The deployed migration-044 API has no explicit read-only field. Its historical streamed
    // cache bucket came from Prompt Cached Tokens and is Cache Read, so preserve those bars during
    // a staged rollout; migration 045 responses always win with the explicit value.
    const explicitCacheRead = point.totals.cache_read_tokens
    const cacheWrite = point.totals.cache_write_tokens
      ?? (explicitCacheRead == null
        ? 0
        : Math.max(point.totals.cached_tokens - explicitCacheRead, 0))
    const cacheRead = explicitCacheRead
      ?? Math.max(point.totals.cached_tokens - cacheWrite, 0)
    row.cached += cacheRead
    row.cacheWrite += cacheWrite
    row.total += point.totals.input_tokens + cacheRead + cacheWrite + point.totals.output_tokens
    row.cost += point.totals.estimated_cost
    rows.set(date, row)
  }
  return [...rows.values()].sort((left, right) => left.date.localeCompare(right.date))
}

/**
 * One chart, two shapes, sharing every axis, grid and tooltip so switching shape changes
 * nothing but the marks.
 *
 * The token series stack as bars and overlay as lines, and that is a real difference rather
 * than an oversight: Recharts' Line has no stackId, and stacked lines would be misread
 * anyway. So bars answer "how much in total per period" -- the column height is the total --
 * while lines answer "how does each series move", which a stack makes hard to see because
 * every series above the first rides on the ones below it. Cost is a single series and
 * reads the same either way.
 *
 * Do not "fix" that asymmetry by making this an Area with a stackId. It was built that way
 * once, precisely because stacked areas make both shapes agree, and the overlaid line was
 * preferred on sight: agreeing with the bar chart also means saying nothing the bar chart
 * does not already say.
 *
 * The y axis is not a reliable signal of that difference and must not be used as one. One
 * series dominates -- cache runs around 90% of the tokens -- so the largest single series
 * sits close to the stacked total, and the two shapes measured the same axis both with all
 * three series (2M) and with cache hidden (260K). What differs is what a given height
 * means, not the scale it is drawn against.
 */
function DailyUsageChart({ rows, metric, series, shape }: {
  rows: DailyUsage[]
  metric: ActivityMetric
  series: Array<{ key: UsageSeriesKey; label: string; color: string }>
  shape: Exclude<ChartMode, "heatmap">
}) {
  const {
    finishSelection,
    isZoomed,
    moveSelection,
    resetZoom,
    selectionArea,
    startSelection,
    zoomedRows,
  } = useChartDragZoom(rows)
  const labelByDate = useMemo(
    () => new Map(zoomedRows.map((row) => [row.date, row.label])),
    [zoomedRows],
  )
  const formatDateLabel = (value: unknown) =>
    labelByDate.get(String(value)) ?? String(value)
  const axes = <>
    <CartesianGrid vertical={false} stroke="var(--border)" />
    <XAxis dataKey="date" tickFormatter={formatDateLabel} tickLine={false} axisLine={false} tickMargin={8} interval="preserveStartEnd" />
    <YAxis tickLine={false} axisLine={false} tickMargin={8} width={48} tickFormatter={(value) => metric === "cost" ? `$${Number(value)}` : compact.format(Number(value))} />
    <Tooltip
      cursor={false}
      content={<FinOpsChartTooltip
        labelFormatter={formatDateLabel}
        nameFormatter={legendLabel}
        valueFormatter={(value) => metric === "cost" ? currency.format(Number(value)) : `${Number(value).toLocaleString(getIntlLocale())} tokens`}
      />}
    />
  </>
  return <div className="usage-daily-chart finops-chart-surface" data-zoomed={isZoomed || undefined}>
    {isZoomed && <ChartZoomReset onClick={resetZoom} />}
    <ResponsiveContainer width="100%" height="100%">
      {shape === "line"
        ? <LineChart
            data={zoomedRows}
            margin={{ left: 0, right: 0, top: 4, bottom: 0 }}
            onMouseDown={startSelection}
            onMouseMove={moveSelection}
            onMouseUp={finishSelection}
            onMouseLeave={() => finishSelection()}
          >
            {axes}
            {selectionArea}
            {(metric === "cost" ? [{ key: "cost", color: "var(--chart-1)" }] : series).map((item) => <Line
              key={item.key}
              // Straight segments, never a curve. Each point is one bucket's measured total,
              // and a cubic through them draws a shape between buckets that was never
              // sampled -- a rise that starts before the hour it happened in, a crest at no
              // hour at all. The kink at a data point is the honest rendering of a series
              // that only exists at data points.
              type="linear"
              dataKey={item.key}
              stroke={item.color}
              strokeWidth={2}
              // A single period would draw an invisible zero-length line, so keep the dot in
              // that case and drop it once there is a stroke to follow.
              dot={zoomedRows.length === 1}
              activeDot={{ r: 3 }}
              isAnimationActive={false}
            />)}
          </LineChart>
        : <BarChart
            data={zoomedRows}
            margin={{ left: 0, right: 0, top: 4, bottom: 0 }}
            onMouseDown={startSelection}
            onMouseMove={moveSelection}
            onMouseUp={finishSelection}
            onMouseLeave={() => finishSelection()}
          >
            {axes}
            {selectionArea}
            {metric === "cost"
              ? <Bar dataKey="cost" fill="var(--chart-1)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
              : series.map((item, index) => <Bar
                  key={item.key}
                  dataKey={item.key}
                  stackId="tokens"
                  fill={item.color}
                  shape={(props: BarShapeProps) => {
                    const row = props.payload as DailyUsage
                    const isTopSegment = row[item.key] > 0
                      && series.slice(index + 1).every((next) => row[next.key] <= 0)
                    return <Rectangle
                      {...props}
                      radius={isTopSegment ? [3, 3, 0, 0] : 0}
                    />
                  }}
                  isAnimationActive={false}
                />)}
          </BarChart>}
    </ResponsiveContainer>
  </div>
}

function ChartLegend({ metric, hidden, onToggle }: {
  metric: ActivityMetric
  hidden: UsageSeriesKey[]
  onToggle: (key: string) => void
}) {
  if (metric === "cost") return <div className="usage-chart-legend"><span><i style={{ background: "var(--chart-1)" }} />费用</span></div>
  return <ChartSeriesLegend className="usage-chart-legend" items={usageSeries} hiddenSeries={hidden} onToggle={onToggle} />
}

function legendLabel(key: string) {
  return { cost: "费用", input: "输入", output: "输出", cached: "缓存读取", cacheWrite: "缓存写入" }[key] ?? key
}

function ChartState({ children }: { children: string }) {
  return <div className="usage-chart-state">{children}</div>
}

function UnpricedState({ reason }: { reason: "missing-prices" | "pending-repricing" }) {
  return reason === "missing-prices"
    ? <div className="usage-chart-state unpriced"><b>成本尚未计价</b><span>当前模型缺少输入或输出单价。切换到 Token 可查看用量明细。</span></div>
    : <div className="usage-chart-state unpriced"><b>成本待回算</b><span>当前范围的 Token 已记录，但 APIM 遥测尚未按注册表价格回算。</span></div>
}
