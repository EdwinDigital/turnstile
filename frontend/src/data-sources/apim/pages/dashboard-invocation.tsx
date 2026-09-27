import { useEffect, useMemo, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Image as ImageIcon, MessageSquare, RefreshCw, Send } from "lucide-react"

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select"
import { useAuth } from "../../../providers/auth-provider"
import { ButtonGroup } from "../../../components/ui/button-group"
import { Input } from "../../../components/ui/input"
import { gatewayInvocationChoices, resolveInvocationModel } from "../../../lib/gateway-topology"
import { dataSource } from "../api"
import { finopsQueries, invalidateFinOps } from "../queries"
import type { EnterpriseEntityCatalog, ImageGenerationOptions, ImageInvocationResponse, ModelInvocationResponse } from "../types"
import { ImageGenerationResult, ImageOptions } from "./image-generation-result"
import {
  compact,
  currency,
  EmptyState,
  ErrorState,
  FilterSelect,
  formatLatency,
  LoadingState,
  PanelTitle,
  queryError,
} from "./dashboard-shared"

const DEFAULT_MODEL_OPTION = "__default__"
const MODEL_TARGET_PREFIX = "model:"
const INVOCATION_MAX_OUTPUT_TOKENS = 1_024

export function AgentInvocation({ entities }: { entities: EnterpriseEntityCatalog }) {
  const queryClient = useQueryClient();
  const { user: sessionUser } = useAuth();
  const registry = useQuery(finopsQueries.registry());
  const [departmentId, setDepartmentId] = useState(
    entities.departments[0]?.id ?? "",
  );
  const projects = entities.projects.filter(
    (item) => item.parent_id === departmentId,
  );
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const agents = entities.agents.filter((item) => item.parent_id === projectId);
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const users = useMemo(() => {
    if (!sessionUser) return [];
    if (sessionUser.role === "owner" && entities.users.some((person) => person.id === sessionUser.email)) {
      return entities.users;
    }
    const sessionIdentity = {
      id: sessionUser.email,
      name: sessionUser.name ?? sessionUser.email,
      parent_id: null,
    };
    if (sessionUser.role === "owner") return [sessionIdentity, ...entities.users];
    return [
      sessionIdentity,
      ...(entities.invocation_testers ?? []).filter(
        (person) => person.parent_id === departmentId && person.id !== sessionUser.email,
      ),
    ];
  }, [departmentId, entities.invocation_testers, entities.users, sessionUser]);
  const [userId, setUserId] = useState(sessionUser?.email ?? users[0]?.id ?? "");
  useEffect(() => {
    if (users.some((person) => person.id === userId)) return;
    setUserId(sessionUser?.email ?? users[0]?.id ?? "");
  }, [sessionUser?.email, userId, users]);
  const [invocationTarget, setInvocationTarget] = useState(DEFAULT_MODEL_OPTION);
  const [imageMode, setImageMode] = useState(false);
  const [gatewaySelection, setGatewaySelection] = useState<{ targetId: string | null; deploymentId: string | null }>({ targetId: null, deploymentId: null });
  const selectedDirectModelId = invocationTarget.startsWith(MODEL_TARGET_PREFIX)
    ? invocationTarget.slice(MODEL_TARGET_PREFIX.length) : "";
  const explicitGatewayAvailable = registry.data?.explicit_gateway_selection_supported === true;
  const topology = useQuery({ ...finopsQueries.apimTargets(), enabled: explicitGatewayAvailable && Boolean(selectedDirectModelId) });
  const invocationGateways = registry.data && selectedDirectModelId
    ? gatewayInvocationChoices(registry.data.gateways, topology.data ?? null, selectedDirectModelId)
      .filter(choice => choice.kind === "managed" || registry.data?.self_hosted_gateway_enabled === true)
    : [];
  const selectedGateway = invocationGateways.find(choice => choice.targetId === gatewaySelection.targetId && choice.deploymentId === gatewaySelection.deploymentId);
  const explicitGateway = gatewaySelection.targetId !== null;
  const observedGatewayStream = topology.data?.items.some(item => item.target.gateway_profile_id === gatewaySelection.targetId
    && item.target.capabilities.response_observer === "supported") === true;
  const gatewayReady = !explicitGateway || (explicitGatewayAvailable && !topology.isLoading && !topology.error && Boolean(selectedGateway?.selectable));
  const invocationModel = resolveInvocationModel(registry.data?.models ?? [], invocationTarget === DEFAULT_MODEL_OPTION ? null : selectedDirectModelId, imageMode);
  const selectedModelUnavailable = Boolean(selectedDirectModelId) && !invocationModel;
  const [imageOptions, setImageOptions] = useState<ImageGenerationOptions>({});
  const [imageResult, setImageResult] = useState<ImageInvocationResponse | null>(null);
  const imageProfile = imageMode && invocationTarget.startsWith(MODEL_TARGET_PREFIX)
    ? registry.data?.models.find(model => model.id === invocationTarget.slice(MODEL_TARGET_PREFIX.length))?.image_profile
    : null;
  const [prompt, setPrompt] = useState(
    "Return one short recommendation for reducing model cost.",
  );
  const [outputLimit, setOutputLimit] = useState(String(INVOCATION_MAX_OUTPUT_TOKENS));
  const maxOutputTokens = Number(outputLimit);
  const validOutputLimit = Number.isSafeInteger(maxOutputTokens) && maxOutputTokens >= 1 && maxOutputTokens <= 1_000_000;
  const [result, setResult] = useState<(ModelInvocationResponse & { requested_max_output_tokens?: number }) | null>(null);
  const [isInvoking, setIsInvoking] = useState(false);
  const [invokeError, setInvokeError] = useState<string | null>(null);
  const selectUser = (value: string | undefined) => {
    setUserId(value ?? "");
  };
  const selectDepartment = (value: string | undefined) => {
    const nextDepartmentId = value ?? "";
    const nextProjects = entities.projects.filter(
      (item) => item.parent_id === nextDepartmentId,
    );
    const nextProjectId = nextProjects[0]?.id ?? "";
    const nextAgentId =
      entities.agents.find((item) => item.parent_id === nextProjectId)?.id ??
      "";
    setDepartmentId(nextDepartmentId);
    setProjectId(nextProjectId);
    setAgentId(nextAgentId);
  };
  const selectProject = (value: string | undefined) => {
    const nextProjectId = value ?? "";
    setProjectId(nextProjectId);
    setAgentId(
      entities.agents.find((item) => item.parent_id === nextProjectId)?.id ??
        "",
    );
  };
  const invoke = async () => {
    if (!registry.data || !gatewayReady || isInvoking || (!imageMode && !validOutputLimit)) return;
    const model = invocationModel;
    if (!model) return;
    if (imageMode && (registry.data.image_generation_supported !== true
      || registry.data.image_configuration_schema_version !== 4 || !imageProfile)) return;
    const runtime = registry.data.runtimes.find(
      (item) => item.id === model?.runtime_id,
    );
    const project = entities.projects.find((item) => item.id === projectId)!;
    const department = entities.departments.find(
      (item) => item.id === departmentId,
    )!;
    const agent = entities.agents.find((item) => item.id === agentId)!;
    const invocationUser = users.find((item) => item.id === userId)!;
    setIsInvoking(true);
    setInvokeError(null);
    setResult(null);
    setImageResult(null);
    try {
      const invocation = {
        runtime_id: explicitGateway ? undefined : model.runtime_id,
        model_id: model.id,
        ...(explicitGateway ? { gateway_profile_id: gatewaySelection.targetId, gateway_deployment_id: gatewaySelection.deploymentId } : {}),
        metadata: {
          organization_id: entities.organizations[0].id,
          organization: entities.organizations[0].name,
          department_id: department.id,
          department: department.name,
          project_id: project.id,
          project: project.name,
          agent_id: agent.id,
          agent: agent.name,
          user_id: invocationUser.id,
          user: invocationUser.name,
          workflow: imageMode ? "image-generation" : `${project.id}-interactive`,
          model_id: model.id,
          // These two are display values, so they must carry names. Sending the registry
          // UUID here put a bare UUID in the model column of every denial trace; the
          // ingestion path hid it by resolving the identity server-side, but a caller
          // should not be labelling a model with its primary key in the first place.
          // The runtime had the same defect, and its fallback still reintroduced it, so a
          // missing registry entry now degrades to unattributed rather than to a UUID.
          model: model.model_key,
          runtime: runtime?.name ?? "unattributed",
          request_source: imageMode ? "image-invocation-module" : "agent-invocation-module",
          run_id: crypto.randomUUID(),
          turn_index: 1,
        },
        messages: [{ role: "user", content: prompt }],
        max_output_tokens: maxOutputTokens,
        stream: !explicitGateway || observedGatewayStream,
      };
      if (imageMode) {
        const response = await dataSource.generateImage({
          ...imageOptions, model_id: model.id, runtime_id: explicitGateway ? undefined : model.runtime_id,
          ...(explicitGateway ? { gateway_profile_id: gatewaySelection.targetId, gateway_deployment_id: gatewaySelection.deploymentId } : {}),
          metadata: invocation.metadata, prompt, n: 1, stream: false,
        });
        setImageResult(response);
      } else {
        const response = await dataSource.invokeModel(invocation);
        setResult({ ...response, requested_max_output_tokens: maxOutputTokens });
      }
      void invalidateFinOps(queryClient);
    } catch (error) {
      setInvokeError(queryError(error));
      void invalidateFinOps(queryClient);
    } finally {
      setIsInvoking(false);
    }
  };
  if (registry.isLoading) return <LoadingState label="正在加载调用配置" />;
  if (registry.error) return <ErrorState error={registry.error} />;
  const models = registry.data?.models.filter(item => item.enabled && item.capabilities.includes("image_generation") === imageMode) ?? [];
  const selectedModel = invocationTarget.startsWith(MODEL_TARGET_PREFIX)
    ? models.find((model) => model.id === invocationTarget.slice(MODEL_TARGET_PREFIX.length))
    : null;
  const totalTokens = result?.usage
    ? result.usage.input_tokens +
      result.usage.cached_tokens +
      result.usage.output_tokens
    : null;
  const cacheWriteTokens = result?.usage?.cache_write_tokens ?? 0;
  const cacheReadTokens = result?.usage
    ? Math.max(result.usage.cached_tokens - cacheWriteTokens, 0)
    : null;
  const exhaustedOutputBudget = !!result?.usage && result.usage.output_tokens >= (result.requested_max_output_tokens ?? 64);
  const imageAvailable = registry.data?.image_generation_supported === true
    && registry.data.image_configuration_schema_version === 4;
  const canInvoke = !isInvoking && Boolean(prompt.trim() && userId && projectId && agentId)
    && gatewayReady && Boolean(invocationModel) && (imageMode || validOutputLimit)
    && (!imageMode || (imageAvailable && Boolean(selectedModel && imageProfile)));
  const selectMode = (images: boolean) => {
    if (isInvoking || imageMode === images) return;
    setImageMode(images);
    setImageOptions({});
    setInvocationTarget(DEFAULT_MODEL_OPTION);
    setGatewaySelection({ targetId: null, deploymentId: null });
    setResult(null);
    setImageResult(null);
    setInvokeError(null);
    setPrompt("");
  };
  return (
    <div className="invoke-layout">
      <section className="finops-panel">
        <PanelTitle title="调用参数" meta="在线模型" />
        <div className="invoke-modebar"><ButtonGroup className="usage-metric-segment" aria-label="调用模式">
          <button type="button" aria-pressed={!imageMode} className={!imageMode ? "active" : ""}
            disabled={isInvoking} onClick={() => selectMode(false)}><MessageSquare size={14} />对话</button>
          <button type="button" aria-pressed={imageMode} className={imageMode ? "active" : ""}
            disabled={isInvoking} onClick={() => selectMode(true)}><ImageIcon size={14} />文生图</button>
        </ButtonGroup></div>
        <div className="invoke-form">
          <FilterSelect
            label="部门"
            value={departmentId}
            items={entities.departments}
            onChange={selectDepartment}
          />
          <FilterSelect
            label="项目"
            value={projectId}
            items={projects}
            onChange={selectProject}
          />
          <FilterSelect
            label="智能体"
            value={agentId}
            items={agents}
            onChange={(value) => setAgentId(value ?? "")}
          />
          <FilterSelect
            label="用户"
            value={userId}
            items={users}
            onChange={selectUser}
            allowAll={false}
          />
          <div className="invoke-select">
            <span>调用目标</span>
            <Select
              value={invocationTarget}
              disabled={isInvoking}
              onValueChange={(next) => {
                setInvocationTarget(next ?? DEFAULT_MODEL_OPTION);
                setGatewaySelection({ targetId: null, deploymentId: null });
                setImageOptions({});
                setImageResult(null);
                setResult(null);
                setInvokeError(null);
              }}
            >
              <SelectTrigger aria-label="调用目标" title={selectedModelUnavailable ? "所选模型不可用" : selectedModel?.display_name ?? (imageMode ? "选择图片模型" : "默认可用模型")}>
                <SelectValue>
                  {selectedModelUnavailable ? "所选模型不可用" : selectedModel?.display_name ?? (imageMode ? "选择图片模型" : "默认可用模型")}
                </SelectValue>
              </SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>
                <SelectItem value={DEFAULT_MODEL_OPTION}>
                  {imageMode ? "选择图片模型" : "默认可用模型"}
                </SelectItem>
                {models.map((model) => (
                  <SelectItem key={model.id} value={`${MODEL_TARGET_PREFIX}${model.id}`}>
                    {model.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selectedModelUnavailable && <div className="invoke-error" role="alert">所选模型不可用</div>}
          </div>
          {explicitGatewayAvailable && selectedDirectModelId && <>
            <div className="invoke-select">
              <span>APIM 实例</span>
              <Select value={gatewaySelection.targetId ?? DEFAULT_MODEL_OPTION} disabled={isInvoking || topology.isLoading || Boolean(topology.error)} onValueChange={value => {
                const targetId = value && value !== DEFAULT_MODEL_OPTION ? value : null;
                const first = invocationGateways.find(choice => choice.targetId === targetId && choice.selectable);
                setGatewaySelection({ targetId, deploymentId: first?.deploymentId ?? null });
                setInvokeError(null);
              }}>
                <SelectTrigger aria-label="APIM 实例"><SelectValue>{explicitGateway ? registry.data?.gateways.find(item => item.id === gatewaySelection.targetId)?.name ?? "选择 APIM 实例" : "默认路由"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  <SelectItem value={DEFAULT_MODEL_OPTION}>默认路由</SelectItem>
                  {registry.data?.gateways.filter(target => invocationGateways.some(choice => choice.targetId === target.id)).map(target => <SelectItem value={target.id} key={target.id}>{target.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            {explicitGateway && <div className="invoke-select">
              <span>网关</span>
              <Select value={gatewaySelection.deploymentId} disabled={isInvoking || topology.isLoading || Boolean(topology.error)} onValueChange={value => setGatewaySelection(current => ({ ...current, deploymentId: value }))}>
                <SelectTrigger aria-label="网关"><SelectValue>{selectedGateway?.name ?? "选择网关"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {invocationGateways.filter(choice => choice.targetId === gatewaySelection.targetId).map(choice => <SelectItem key={choice.value} value={choice.deploymentId!} disabled={!choice.selectable} title={choice.reason ?? choice.endpoint ?? undefined}>{choice.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>}
            {topology.error && <div className="invoke-error" role="alert">{queryError(topology.error)}</div>}
            {explicitGateway && !topology.isLoading && !gatewayReady && <div className="invoke-error" role="alert">请选择可用的网关。</div>}
          </>}
          {imageMode && imageProfile && <ImageOptions value={imageOptions} onChange={setImageOptions} disabled={isInvoking} />}
          {imageMode && registry.data?.image_configuration_schema_version !== 4 && <div className="invoke-error" role="alert">后端尚未支持图像参数透传</div>}
          {!imageMode && <label className="invoke-select"><span>最大输出 Tokens</span><Input type="number" aria-label="最大输出 Tokens" min={1} max={1_000_000} step={1} value={outputLimit} disabled={isInvoking} aria-invalid={!validOutputLimit} onChange={event => setOutputLimit(event.target.value)} /></label>}
          <div className="invoke-prompt">
            <span>Prompt</span>
            <div className="invoke-composer">
              <textarea
                aria-label="Prompt"
                value={prompt}
                disabled={isInvoking}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Enter" || (!event.metaKey && !event.ctrlKey) || event.nativeEvent.isComposing) return
                  event.preventDefault()
                  if (canInvoke) void invoke()
                }}
                rows={7}
              />
            </div>
          </div>
          {invokeError && (
            <div className="invoke-error" role="alert">
              {invokeError}
            </div>
          )}
          <button
            type="button"
            className="primary-button"
            title={isInvoking ? "调用中..." : "发起调用 · ⌘↵"}
            disabled={!canInvoke}
            onClick={() => void invoke()}
          >
            {isInvoking ? <RefreshCw className="spin" size={15} /> : <Send size={15} />}
            {isInvoking ? "调用中..." : imageMode ? "生成图片" : "发起调用"}
          </button>
        </div>
      </section>
      <section className="finops-panel invoke-result">
        {imageMode ? <ImageGenerationResult result={imageResult} pending={isInvoking} available={imageAvailable} /> : <>
        <PanelTitle title="调用结果" />
        {result ? (
          <div className="invoke-response">
            <dl className="invoke-result-facts">
              <div>
                <dt>网关</dt>
                <dd>{result.gateway.toUpperCase()}</dd>
              </div>
              <div>
                <dt>模型</dt>
                <dd>{result.model}</dd>
              </div>
              <div>
                <dt>运行时</dt>
                <dd>{result.runtime}</dd>
              </div>
              <div>
                <dt>延迟</dt>
                <dd>{formatLatency(result.latency_ms)}</dd>
              </div>
              <div>
                <dt>Token{result.usage?.estimated ? "（估算）" : ""}</dt>
                <dd>
                  {totalTokens == null ? "—" : compact.format(totalTokens)}
                </dd>
              </div>
              <div>
                <dt>Cache Read</dt>
                <dd>{cacheReadTokens == null ? "—" : compact.format(cacheReadTokens)}</dd>
              </div>
              <div>
                <dt>Cache Write</dt>
                <dd>{result.usage == null ? "—" : compact.format(cacheWriteTokens)}</dd>
              </div>
              <div>
                <dt>估算成本</dt>
                <dd>
                  {result.estimated_cost == null
                    ? "未计价"
                    : currency.format(result.estimated_cost)}
                </dd>
              </div>
            </dl>
            <dl className="invoke-result-ids">
              <div>
                <dt>Request ID</dt>
                <dd>{result.request_id}</dd>
              </div>
              <div>
                <dt>Correlation ID</dt>
                <dd>{result.correlation_id ?? "未知"}</dd>
              </div>
              {result.upstream_request_id && <div><dt>Upstream Request ID</dt><dd data-no-localize>{result.upstream_request_id}</dd></div>}
            </dl>
            <div className="invoke-result-content">
              <span>响应内容</span>
              {result.content.trim()
                ? <pre>{result.content}</pre>
                : <div className="invoke-empty-content"><AlertTriangle size={16} /><div><b>模型未返回可见文本</b><span>{exhaustedOutputBudget ? `本次输出使用了 ${result.usage?.output_tokens ?? 0} Token，已达到请求上限；模型可能在生成可见回答前耗尽了输出预算。后续调用已提高上限。` : "模型返回了 Token 用量，但响应内容为空，请重试或缩短提示词。"}</span></div></div>}
            </div>
          </div>
        ) : (
          <EmptyState
            title="尚未调用"
            detail="选择业务元数据和模型后发起请求。"
          />
        )}
        </>}
      </section>
    </div>
  );
}
