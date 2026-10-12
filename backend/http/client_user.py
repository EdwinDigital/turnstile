from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from turnstile_core.config import get_settings
from turnstile_core.domain.models import StrictModel

from ..services.auth_service import hash_session_token
from ..services.client_user import USER_PLUGIN_VERSION, ClientUserService
from ..services.client_user_store import ClientUserStore
from .dependencies import Repository
from .service_dependencies import ApplicationAccessServiceDependency
from .session import CurrentSession, Store, require_allowed_write_origin

router = APIRouter(tags=["Client User"], dependencies=[Depends(require_allowed_write_origin)])
PRIVATE = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


@lru_cache(maxsize=1)
def client_store() -> ClientUserStore:
    settings = get_settings()
    if not settings.database_url:
        raise HTTPException(503, "Client authentication requires PostgreSQL")
    return ClientUserStore(settings.database_url, pool_min_size=0, pool_max_size=2)


def client_user_service(
    repository: Repository,
    applications: ApplicationAccessServiceDependency,
) -> ClientUserService:
    return _service(repository, applications)


_services: dict[int, ClientUserService] = {}


def close_client_store() -> None:
    if client_store.cache_info().currsize:
        client_store().close()
        client_store.cache_clear()
    _services.clear()


def _service(repository: Any, applications: Any) -> ClientUserService:
    key = id(repository)
    if key not in _services:
        _services[key] = ClientUserService(client_store(), repository, applications, get_settings())
    return _services[key]


Service = Annotated[ClientUserService, Depends(client_user_service)]


class DeviceRequest(StrictModel):
    user_id: UUID
    binding: str = Field(min_length=64, max_length=64)
    installation_id: UUID
    purpose: Literal["login", "import", "reauth"] = "login"


class TokenRequest(StrictModel):
    grant_type: Literal["device_code", "refresh_token"]
    device_code: str = Field(default="", max_length=100)
    refresh_token: str = Field(default="", max_length=100)
    request_id: UUID | None = None


class Decision(StrictModel):
    user_code: str = Field(pattern=r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
    approve: bool
    application_id: UUID | None = None
    subscription_id: UUID | None = None
    gateway_id: UUID | None = None


class UserDownload(StrictModel):
    acknowledged: Literal[True]


def bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or not 20 <= len(value) <= 110:
        raise HTTPException(401, "A user access token is required", headers=PRIVATE)
    return value[7:]


def authorized(request: Request, service: ClientUserService) -> tuple[dict[str, Any], Any]:
    service.require_enabled()
    token = bearer(request)
    if not token.startswith("tsca_"):
        raise HTTPException(401, "A user access token is required", headers=PRIVATE)
    grant = service.store.grant(token)
    return grant, service.identity(grant)


@router.get("/api/v1/client-downloads/user-options")
def user_options(identity: CurrentSession, response: Response, service: Service) -> dict[str, Any]:
    response.headers.update(PRIVATE)
    result: dict[str, Any] = {
        "enabled": os.getenv("CLIENT_USER_ENABLED", "false").lower() == "true"
        and bool(os.getenv("CLIENT_PLATFORM_ORIGIN", "")),
        "plugin_version": USER_PLUGIN_VERSION,
        "name": "AIGateway-Proxy 用户版",
        "user_name": identity.name or identity.email,
    }
    if result["enabled"]:
        try:
            selected = service.default_route(identity)
            result["personal_route"] = {k: v for k, v in selected.items() if k != "base_url"}
        except HTTPException as error:
            result.update(enabled=False, unavailable_reason=error.detail)
    return result


@router.post("/api/v1/client-downloads/magpie-user")
def download(body: UserDownload, identity: CurrentSession, service: Service) -> Response:
    return Response(
        service.package(identity),
        media_type="application/zip",
        headers={
            **PRIVATE,
            "Content-Disposition": (
                f'attachment; filename="aigateway-proxy-user-{UUID(identity.id).hex}.zip"'
            ),
        },
    )


@router.post("/api/v1/client-auth/device-authorizations")
def device(body: DeviceRequest, service: Service) -> JSONResponse:
    return JSONResponse(
        service.create_device(str(body.user_id), body.binding, body.installation_id, body.purpose),
        headers=PRIVATE,
    )


@router.get("/api/v1/client-auth/verification")
def verification(
    user_code: str, identity: CurrentSession, service: Service, request: Request, store: Store
) -> JSONResponse:
    service.require_enabled()
    row = service.store.device(user_code, identity.id)
    owner = store.session_owner(
        hash_session_token(request.cookies.get(service.settings.session_cookie_name, ""))
    )
    return JSONResponse(
        {
            "user_name": identity.name or identity.email,
            "purpose": row["purpose"],
            "state": row["state"],
            "expires_at": row["expires_at"].isoformat(),
            "reauth_required": row["purpose"] == "reauth"
            and (not owner or owner.get("created_at", row["created_at"]) < row["created_at"]),
            "personal_route": {
                k: v for k, v in service.default_route(identity).items() if k != "base_url"
            },
        },
        headers=PRIVATE,
    )


@router.post("/api/v1/client-auth/decision")
def decision(
    body: Decision, identity: CurrentSession, service: Service, request: Request, store: Store
) -> JSONResponse:
    service.require_enabled()
    service.store.device(body.user_code, identity.id)
    selection = None
    if body.approve:
        chosen = service.default_route(identity)
        selection = {k: chosen[k] for k in ("mode", "gateway_id")}
        if body.application_id is not None or body.subscription_id is not None:
            raise HTTPException(409, "User authorization does not bind an application subscription")
        if body.gateway_id is not None and str(body.gateway_id) != selection["gateway_id"]:
            raise HTTPException(409, "The personal gateway changed; reload the verification page")
    owner = store.session_owner(
        hash_session_token(request.cookies.get(service.settings.session_cookie_name, ""))
    )
    if not owner or "created_at" not in owner:
        raise HTTPException(401, "A valid platform session is required")
    service.store.decide(
        body.user_code, identity.id, body.approve, selection, identity.method, owner["created_at"]
    )
    return JSONResponse({"status": "approved" if body.approve else "denied"}, headers=PRIVATE)


@router.post("/api/v1/client-auth/token")
def token(body: TokenRequest, service: Service) -> JSONResponse:
    service.require_enabled()
    try:
        result = (
            service.poll(body.device_code)
            if body.grant_type == "device_code"
            else (service.refresh(body.refresh_token, str(body.request_id or "")))
        )
    except HTTPException as error:
        return JSONResponse({"error": error.detail}, status_code=error.status_code, headers=PRIVATE)
    return JSONResponse(result, headers=PRIVATE)


@router.post("/api/v1/client-auth/revoke")
def revoke(request: Request, service: Service) -> JSONResponse:
    service.require_enabled()
    value = bearer(request)
    if value.startswith("tsci_"):
        grant = service.store.grant(value, image=True)
        service.store.remove_image_key(grant["id"])
        return JSONResponse({"revoked": True}, headers=PRIVATE)
    grant, _ = authorized(request, service)
    service.store.revoke(grant["id"])
    return JSONResponse({"revoked": True}, headers=PRIVATE)


@router.get("/api/v1/client-runtime/catalog")
def catalog(request: Request, service: Service) -> JSONResponse:
    grant, identity = authorized(request, service)
    return JSONResponse(service.catalog(grant, identity), headers=PRIVATE)


@router.post("/api/v1/client-runtime/refresh-models")
def refresh_models(request: Request, service: Service) -> JSONResponse:
    grant, identity = authorized(request, service)
    return JSONResponse(service.catalog(grant, identity, fresh=True), headers=PRIVATE)


@router.get("/api/v1/client-runtime/usage")
def usage(request: Request, service: Service) -> JSONResponse:
    grant, identity = authorized(request, service)
    return JSONResponse(service.usage(grant, identity), headers=PRIVATE)


@router.post("/api/v1/client-runtime/image-capabilities")
def image_capability(request: Request, service: Service) -> JSONResponse:
    import secrets

    grant, identity = authorized(request, service)
    service.catalog(grant, identity)
    key = "tsci_" + secrets.token_urlsafe(32)
    service.store.image_key(grant["id"], key)
    return JSONResponse({"key": key, "expires_in": 86400}, headers=PRIVATE)


@router.get("/api/v1/client-runtime/models")
@router.get("/api/v1/client-runtime/v1/models")
def models(request: Request, service: Service) -> JSONResponse:
    service.require_enabled()
    access = bearer(request)
    image = access.startswith("tsci_")
    grant = service.store.grant(access, image=image)
    identity = service.identity(grant)
    current = service.selected(identity, grant.get("selection") or {})
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": m["id"], "object": "model", "owned_by": "aigateway"}
                for m in current["models"]
                if not image or m["protocol"] == "openai_images"
            ],
        },
        headers=PRIVATE,
    )


async def limited_body(request: Request) -> bytes:
    parts = bytearray()
    async for part in request.stream():
        parts.extend(part)
        if len(parts) > 16 * 1024 * 1024:
            raise HTTPException(413, "Client request is too large")
    return bytes(parts)


def attributed_body(body: dict[str, Any], user_id: str, protocol: str) -> bytes:
    body = dict(body)
    if protocol == "anthropic_messages":
        metadata = body.get("metadata")
        body["metadata"] = {
            **(metadata if isinstance(metadata, dict) else {}),
            # Anthropic rejects email-shaped IDs; APIM attribution keeps the signed identity.
            "user_id": hashlib.sha256(user_id.encode()).hexdigest(),
        }
        body.pop("user", None)
    else:
        body["user"] = user_id
    return json.dumps(body, ensure_ascii=False).encode()


@router.post("/api/v1/client-runtime/{path:path}")
async def proxy(path: str, request: Request, service: Service) -> Response:
    endpoints = {
        "responses": "openai_responses",
        "chat/completions": "openai_chat",
        "v1/messages": "anthropic_messages",
        "messages": "anthropic_messages",
        "images/generations": "openai_images",
        "v1/images/generations": "openai_images",
    }
    if path not in endpoints:
        raise HTTPException(404, "Unsupported client endpoint")
    raw = await limited_body(request)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise HTTPException(400, "A JSON body with an authorized model is required") from error

    def prepare() -> tuple[str, dict[str, str], bytes]:
        service.require_enabled()
        credential = bearer(request)
        image_key = credential.startswith("tsci_")
        if image_key and endpoints[path] != "openai_images":
            raise HTTPException(403, "This capability permits images only")
        grant = service.store.grant(credential, image=image_key)
        identity = service.identity(grant)
        selected = service.selected(identity, grant.get("selection") or {})
        service.store.activate(grant["id"])
        model = next((m for m in selected["models"] if m["id"] == body["model"]), None)
        if not model or model["protocol"] != endpoints[path]:
            raise HTTPException(403, "Model access or protocol is not granted")
        service.validate_model(identity, selected, model)
        rid = str(uuid4())
        context = service.context(identity, selected, body["model"], rid)
        suffix = (
            "v1/messages" if endpoints[path] == "anthropic_messages" else path.removeprefix("v1/")
        )
        # Never forward an Agent-supplied user identity, including nested Anthropic metadata.
        return (
            selected["base_url"] + "/" + suffix,
            {
                "x-turnstile-client-context": context,
                "x-request-id": rid,
                "x-request-source": "aigateway-user",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
            attributed_body(body, identity.effective_user_id, endpoints[path]),
        )

    url, headers, raw = await run_in_threadpool(prepare)
    upstream = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15), follow_redirects=False)
    try:
        response = await upstream.send(
            upstream.build_request("POST", url, headers=headers, content=raw), stream=True
        )
    except httpx.HTTPError as error:
        await upstream.aclose()
        raise HTTPException(502, "The model gateway is unavailable") from error

    async def chunks() -> Any:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await upstream.aclose()

    response_headers = {
        **PRIVATE,
        **{
            k: v
            for k, v in response.headers.items()
            if k.lower()
            in {
                "content-type",
                "retry-after",
                "x-correlation-id",
                "apim-request-id",
                "request-id",
            }
        },
    }
    return StreamingResponse(chunks(), status_code=response.status_code, headers=response_headers)
