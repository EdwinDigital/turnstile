import type { Edge, Node } from "@xyflow/react"
import type { ApimTargetTopologyList, ManagedModel, ModelProvider, ModelRegistry, ModelRuntime } from "../data-sources/apim/types"
import { apimGatewayEntries, apimTargetDisplayName, type GatewayEntry } from "./gateway-topology"
import { modelInventoryFamily, modelInventoryPlatform } from "./model-inventory-filters"

export type InventoryGraphGateway = {
  id: string
  name: string
  apimName: string
  entry: GatewayEntry
}

export type InventoryGraphConnection = {
  id: string
  gatewayId: string | null
  runtime: ModelRuntime | null
  provider: ModelProvider | null
  name: string
  tone: string
  models: ManagedModel[]
}

export type InventoryGraphProjection = {
  gateways: InventoryGraphGateway[]
  connections: InventoryGraphConnection[]
}

export type InventoryGraphNodeData = {
  kind: "gateway" | "connection" | "model"
  gatewayId: string | null
  connectionId: string | null
  modelId: string | null
  radius: number
  innerRadius: number
  tone: string
} & Record<string, unknown>

export type InventoryGraphNode = Node<InventoryGraphNodeData>
export type InventoryGraphEdge = Edge<{ tone: string; gateway: boolean }>
export type InventoryGraphLayout = { nodes: InventoryGraphNode[]; edges: InventoryGraphEdge[] }

export function projectModelInventoryGraph(registry: ModelRegistry, models: readonly ManagedModel[], topology: ApimTargetTopologyList | null): InventoryGraphProjection {
  const gateways: InventoryGraphGateway[] = []
  const connections = new Map<string, InventoryGraphConnection>()
  const modelsById = new Map(models.map(model => [model.id, model]))
  const runtimesById = new Map(registry.runtimes.map(runtime => [runtime.id, runtime]))
  const providersById = new Map(registry.providers.map(provider => [provider.id, provider]))
  const represented = new Set<string>()
    const add = (model: ManagedModel, gatewayId: string | null) => {
      const runtime = runtimesById.get(model.runtime_id) ?? null
    const provider = runtime ? providersById.get(runtime.provider_id) ?? null : null
    const id = `connection:${gatewayId ?? "unrecorded"}:${runtime?.id ?? "unrecorded"}`
    let connection = connections.get(id)
    if (!connection) {
      const platform = modelInventoryPlatform(runtime ?? undefined, provider ?? undefined)
      connection = {
        id, gatewayId, runtime, provider, name: runtime?.name ?? "连接未记录",
        tone: platform === "microsoft_foundry" ? "foundry" : platform === "azure_databricks" ? "databricks"
          : platform === "amazon_bedrock" ? "bedrock"
          : modelInventoryFamily(model) === "kimi" ? "kimi" : "neutral",
        models: [],
      }
      connections.set(id, connection)
    }
    if (!connection.models.some(item => item.id === model.id)) connection.models.push(model)
    represented.add(model.id)
  }
  for (const target of topology?.items ?? []) {
    for (const entry of apimGatewayEntries(target)) {
      if (entry.published_models_available === false) continue
      const seen = new Set<string>()
      const members = entry.published_models.flatMap(item => {
        const model = modelsById.get(item.id)
        if (!model || seen.has(model.id)) return []
        seen.add(model.id)
        return [model]
      })
      if (!members.length) continue
      const id = `gateway:${entry.deployment.id}`
      gateways.push({ id, name: entry.deployment.display_name.trim() || entry.deployment.native_gateway_id,
        apimName: apimTargetDisplayName(target, registry.gateways), entry })
      members.forEach(model => add(model, id))
    }
  }
  models.filter(model => !represented.has(model.id)).forEach(model => add(model, null))
  return { gateways, connections: [...connections.values()] }
}

type Member = { model: ManagedModel; width: number; x: number; y: number; tone: string }
type Cluster = { connection: InventoryGraphConnection; radius: number; innerRadius: number; members: Member[]; halfWidth: number; halfHeight: number }

function ringRadius(widths: number[], innerWidths: number[] = [], innerRadius = 0) {
  const angles = widths.map((_width, index) => -Math.PI / 2 + (innerWidths.length ? Math.PI / widths.length : 0) + index * 2 * Math.PI / widths.length)
  let radius = 100
  angles.forEach((angle, index) => {
    radius = Math.max(radius, Math.min((widths[index] / 2 + 42) / Math.abs(Math.cos(angle)), 61 / Math.abs(Math.sin(angle))))
    angles.slice(index + 1).forEach((other, offset) => {
      radius = Math.max(radius, Math.min(((widths[index] + widths[index + offset + 1]) / 2 + 10) / Math.abs(Math.cos(angle) - Math.cos(other)), 48 / Math.abs(Math.sin(angle) - Math.sin(other))))
    })
  })
  radius = Math.ceil(Math.max(radius, innerWidths.length ? innerRadius + 48 : 0) / 10) * 10
  const innerAngles = innerWidths.map((_width, index) => -Math.PI / 2 + index * 2 * Math.PI / innerWidths.length)
  while (innerWidths.length && widths.some((width, index) => innerWidths.some((inner, innerIndex) =>
    Math.abs(radius * Math.cos(angles[index]) - innerRadius * Math.cos(innerAngles[innerIndex])) < (width + inner) / 2 + 10
    && Math.abs(radius * Math.sin(angles[index]) - innerRadius * Math.sin(innerAngles[innerIndex])) < 48))) radius += 10
  return radius
}

export function layoutModelInventoryGraph(projection: InventoryGraphProjection, width: number, height: number, measure: (name: string) => number): InventoryGraphLayout {
  const clusters: Cluster[] = projection.connections.map(connection => {
    const widths = connection.models.map(model => Math.max(120, Math.min(240, Math.ceil(measure(model.display_name) + 64))))
    const innerCount = widths.length > 12 ? Math.ceil(widths.length / 2) : 0
    const innerRadius = innerCount ? Math.max(174, ringRadius(widths.slice(0, innerCount))) : 0
    const compact = innerCount ? ringRadius(widths.slice(innerCount), widths.slice(0, innerCount), innerRadius)
      : Math.max(widths.length > 4 ? 174 : 100, ringRadius(widths))
    const radius = innerCount ? innerRadius + (compact - innerRadius) * 1.15 : compact
    const members = connection.models.map((model, index) => {
      const inner = innerCount > 0 && index < innerCount
      const count = inner ? innerCount : widths.length - innerCount
      const ringIndex = inner ? index : index - innerCount
      const angle = -Math.PI / 2 + (innerCount && !inner ? Math.PI / count : 0) + ringIndex * 2 * Math.PI / count
      const distance = inner ? innerRadius : radius
      const tone = modelInventoryFamily(model) === "claude" ? "claude" : connection.tone
      return { model, width: widths[index], x: distance * Math.cos(angle), y: distance * Math.sin(angle), tone }
    })
    return { connection, radius, innerRadius, members, halfWidth: radius + Math.max(...widths) / 2, halfHeight: radius + 19 }
  })
  const direction = width >= height ? Math.PI / 12 : -Math.PI / 2 + Math.PI / 12
  const trees = [
    ...projection.gateways.map(gateway => ({ gateway, clusters: clusters.filter(cluster => cluster.connection.gatewayId === gateway.id) })),
    ...clusters.filter(cluster => !cluster.connection.gatewayId).map(cluster => ({ gateway: null, clusters: [cluster] })),
  ].map(tree => {
    const members = tree.clusters.map((cluster, index) => {
      const angle = direction + index * 2 * Math.PI / tree.clusters.length
      const distance = tree.gateway ? cluster.radius + 46 + (150 + Math.hypot(cluster.halfWidth, cluster.halfHeight) - cluster.radius - 46) * .6 : 0
      return { ...cluster, x: distance * Math.cos(angle), y: distance * Math.sin(angle) }
    })
    const left = Math.min(...members.map(member => member.x - member.halfWidth), ...(tree.gateway ? [-110] : []))
    const right = Math.max(...members.map(member => member.x + member.halfWidth), ...(tree.gateway ? [110] : []))
    const top = Math.min(...members.map(member => member.y - member.halfHeight), ...(tree.gateway ? [-46] : []))
    const bottom = Math.max(...members.map(member => member.y + member.halfHeight), ...(tree.gateway ? [106] : []))
    return { ...tree, members, left, right, top, bottom, width: right - left, height: bottom - top }
  })
  let best = { columns: 1, widths: [0], heights: [0], score: -1 }
  for (let columns = 1; columns <= trees.length; columns++) {
    const widths = Array<number>(columns).fill(0)
    const heights = Array<number>(Math.ceil(trees.length / columns)).fill(0)
    trees.forEach((tree, index) => {
      widths[index % columns] = Math.max(widths[index % columns], tree.width)
      heights[Math.floor(index / columns)] = Math.max(heights[Math.floor(index / columns)], tree.height)
    })
    const score = Math.min((width - 48) / (widths.reduce((total, value) => total + value, 0) + (columns - 1) * 80),
      (height - 80) / (heights.reduce((total, value) => total + value, 0) + (heights.length - 1) * 80))
    if (score > best.score) best = { columns, widths, heights, score }
  }
  const nodes: InventoryGraphNode[] = []
  const edges: InventoryGraphEdge[] = []
  trees.forEach((tree, index) => {
    const column = index % best.columns
    const row = Math.floor(index / best.columns)
    const x = best.widths.slice(0, column).reduce((total, value) => total + value, 0) + column * 80 + best.widths[column] / 2 - (tree.left + tree.right) / 2
    const y = best.heights.slice(0, row).reduce((total, value) => total + value, 0) + row * 80 + best.heights[row] / 2 - (tree.top + tree.bottom) / 2
    if (tree.gateway) nodes.push({ id: tree.gateway.id, type: "gateway", position: { x: x - 110, y: y - 46 },
      data: { kind: "gateway", gatewayId: tree.gateway.id, connectionId: null, modelId: null, radius: 46, innerRadius: 0, tone: "gateway" },
      style: { width: 220, height: 152 }, dragHandle: ".model-graph-gateway", zIndex: 3 })
    tree.members.forEach(cluster => {
      const position = tree.gateway ? { x: cluster.x + 110 - cluster.radius, y: cluster.y + 46 - cluster.radius }
        : { x: x + cluster.x - cluster.radius, y: y + cluster.y - cluster.radius }
      nodes.push({ id: cluster.connection.id, type: "connection", parentId: tree.gateway?.id, position,
        data: { kind: "connection", connectionId: cluster.connection.id, gatewayId: cluster.connection.gatewayId, modelId: null,
          radius: cluster.radius, innerRadius: cluster.innerRadius, tone: cluster.connection.tone },
        style: { width: cluster.radius * 2, height: cluster.radius * 2 }, dragHandle: ".model-graph-ring,.model-graph-connection-center", zIndex: 0 })
      if (tree.gateway) edges.push({ id: `${tree.gateway.id}:${cluster.connection.id}`, source: tree.gateway.id, target: cluster.connection.id,
        sourceHandle: "source", targetHandle: "target", type: "inventory", zIndex: 1, data: { tone: "gateway", gateway: true } })
      cluster.members.forEach(member => {
        const id = `${cluster.connection.id}:model:${member.model.id}`
        nodes.push({ id, type: "model", parentId: cluster.connection.id,
          position: { x: cluster.radius + member.x - member.width / 2, y: cluster.radius + member.y - 19 }, draggable: false, zIndex: 3,
          style: { width: member.width, height: 38 },
          data: { kind: "model", connectionId: cluster.connection.id, gatewayId: cluster.connection.gatewayId,
            modelId: member.model.id, radius: 0, innerRadius: 0, tone: member.tone } })
        edges.push({ id: `${cluster.connection.id}:${id}`, source: cluster.connection.id, target: id,
          sourceHandle: "source", targetHandle: "target", type: "inventory", zIndex: 1,
          data: { tone: member.tone, gateway: false } })
      })
    })
  })
  return { nodes, edges }
}