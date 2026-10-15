import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import { usageWindow } from "../../data-sources/apim/api"
import { finopsQueries } from "../../data-sources/apim/queries"
import { getIntlLocale } from "../../locales"
import { useTimezone } from "../../providers/timezone-provider"
import { ActivityHeatmap } from "./activity-heatmap"
import { ChartSeriesLegend, useSeriesToggle } from "./chart-legend"
import { FinOpsChartTooltip } from "./chart-tooltip"
import { aggregateActivity } from "./usage-activity-card"

const usageSeries = [
  { key: "input", label: "输入", color: "var(--chart-4)" },
  { key: "cached", label: "缓存读取", color: "var(--chart-1)" },
  { key: "cacheWrite", label: "缓存写入", color: "var(--chart-cache-write)" },
  { key: "output", label: "输出", color: "var(--chart-2)" },
] as const

function metricLabel(value: string) {
  return usageSeries.find((series) => series.key === value)?.label
    ?? (value === "cost" ? "估算费用" : value)
}

function formatCompact(value: number) {
  return new Intl.NumberFormat(getIntlLocale(), {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value)
}

function formatFull(value: number) {
  return new Intl.NumberFormat(getIntlLocale()).format(value)
}

function formatCost(value: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 4,
  }).format(value)
}

export function ApplicationUsageActivityCard({ applicationId }: { applicationId: string }) {
  const { timezone } = useTimezone()
  const [interval, setInterval] = useState<"day" | "week">("day")
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [metric, setMetric] = useState<"cost" | "tokens" | "heatmap">("tokens")
  const { hiddenSeries, toggleSeries } = useSeriesToggle()
  const filters = useMemo(() => usageWindow(days), [days])
  const heatmapFilters = useMemo(() => usageWindow(182), [])
  const activity = useQuery(finopsQueries.gatewayApplicationUsageActivity(
    applicationId,
    filters,
    interval,
    timezone,
  ))
  const heatmapActivity = useQuery({
    ...finopsQueries.gatewayApplicationUsageActivity(
      applicationId,
      heatmapFilters,
      "day",
      timezone,
    ),
    enabled: metric === "heatmap",
  })
  const chartData = aggregateActivity(activity.data?.points ?? [], interval, timezone)
  const heatmapDays = aggregateActivity(
    heatmapActivity.data?.points ?? [],
    "day",
    timezone,
  ).map((row) => ({ date: row.date, value: row.total }))
  const visibleSeries = usageSeries.filter((series) => !hiddenSeries.includes(series.key))
  const hasVisibleValues = metric === "cost"
    ? chartData.some((row) => row.cost > 0)
    : chartData.some((row) => visibleSeries.some((series) => row[series.key] > 0))

  return <>
    <div className="application-usage-controls">
      <div><span>维度</span><div role="group" aria-label="图表维度"><button type="button" className={interval === "day" ? "active" : ""} onClick={() => setInterval("day")}>按天</button><button type="button" className={interval === "week" ? "active" : ""} onClick={() => setInterval("week")}>按周</button></div></div>
      <div><span>时间范围</span><div role="group" aria-label="图表时间范围">{([7, 30, 90] as const).map((value) => <button type="button" key={value} className={days === value ? "active" : ""} onClick={() => setDays(value)}>{value}d</button>)}</div></div>
    </div>
    <section className="application-card application-usage-activity-card">
      <header className="application-usage-chart-head">
        <div className="application-usage-chart-title">
          <h3>用量时段分布</h3>
          <div className="application-usage-metric-toggle" role="group" aria-label="图表指标"><button type="button" className={metric === "cost" ? "active" : ""} onClick={() => setMetric("cost")}>估算费用</button><button type="button" className={metric === "tokens" ? "active" : ""} onClick={() => setMetric("tokens")}>Token</button><button type="button" className={metric === "heatmap" ? "active" : ""} onClick={() => setMetric("heatmap")}>热力图</button></div>
        </div>
        {metric === "tokens" && <ChartSeriesLegend
          className="runtime-chart-legend application-usage-chart-legend"
          items={usageSeries.map((series) => ({ ...series }))}
          hiddenSeries={hiddenSeries}
          onToggle={toggleSeries}
        />}
      </header>
      {metric === "heatmap" ? heatmapActivity.isLoading ? <ChartState>正在加载用量数据</ChartState>
        : heatmapActivity.error ? <ChartState>用量数据加载失败</ChartState>
        : heatmapDays.length === 0 ? <ChartState>当前筛选范围暂无 Token 用量</ChartState>
        : <div className="application-usage-heatmap"><p>最近 26 周 · 每日 Token 强度（此处忽略上方时间范围）</p><ActivityHeatmap days={heatmapDays} metric="tokens" timezone={timezone} /></div>
        : activity.isLoading ? <ChartState>正在加载用量数据</ChartState>
        : activity.error ? <ChartState>用量数据加载失败</ChartState>
        : !hasVisibleValues ? <ChartState>{metric === "cost" ? "当前筛选范围暂无费用数据" : "当前筛选范围暂无 Token 用量"}</ChartState>
        : <div className="application-usage-chart-canvas finops-chart-surface">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--border)" />
              <XAxis dataKey="label" axisLine={false} tickLine={false} tickMargin={8} />
              <YAxis axisLine={false} tickLine={false} tickSize={0} tickMargin={8} width="auto" tickFormatter={(value) => metric === "cost" ? `$${Number(value).toFixed(2)}` : formatCompact(Number(value))} />
              <Tooltip cursor={false} content={<FinOpsChartTooltip nameFormatter={metricLabel} valueFormatter={(value) => metric === "cost" ? formatCost(Number(value)) : `${formatFull(Number(value))} tokens`} />} />
              {metric === "cost" ? <Bar dataKey="cost" fill="var(--chart-1)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
                : visibleSeries.map((series, index) => <Bar key={series.key} dataKey={series.key} stackId="tokens" fill={series.color} radius={index === visibleSeries.length - 1 ? [3, 3, 0, 0] : 0} isAnimationActive={false} />)}
            </BarChart>
          </ResponsiveContainer>
        </div>}
    </section>
  </>
}

function ChartState({ children }: { children: string }) {
  return <div className="application-usage-chart-state">{children}</div>
}