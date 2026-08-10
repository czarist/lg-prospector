"""Cliente HTTP para API REST do EspoCRM — nunca acessa o banco diretamente.

Auth (conforme API.md):
1. ESPO_CRM_TOKEN como API key → header ``X-Api-Key``
2. ESPO_CRM_TOKEN como ``user:password`` → Basic + Espo-Authorization
3. Fallback CRM_USER + CRM_PASSWORD
4. Token de sessão via GET /App/user (opcional)
"""

from __future__ import annotations

import base64
from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.resilience.circuit_breaker import CircuitBreaker
from app.infrastructure.resilience.retry import async_retry

logger = get_logger(__name__)


class CRMClient:
    def __init__(
        self,
        base_url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        api_token: str | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.effective_crm_url).rstrip("/")
        self.user = user if user is not None else settings.crm_user
        self.password = password if password is not None else settings.crm_password
        # ESPO_CRM_TOKEN tem prioridade (API key ou user:password)
        self.api_token = (api_token if api_token is not None else settings.espo_crm_token).strip()
        self._session_token: Optional[str] = None
        self._auth_mode = self._resolve_auth_mode()
        self._enabled = bool(self.base_url) and (
            bool(self.api_token) or bool(self.password)
        )
        self._dry_run = settings.app_env.lower() in {"test", "testing"}
        self._circuit = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0, name="espocrm")

    def _resolve_auth_mode(self) -> str:
        """
        Returns:
            api_key | basic_token | basic_password
        """
        token = self.api_token
        if token:
            # "user:password" (ex.: admin:Senha) → Basic Auth
            if ":" in token and not token.startswith("eyJ"):
                user, _, password = token.partition(":")
                if user and password:
                    self.user = user
                    self.password = password
                    return "basic_token"
            # Caso contrário trata como API Key (X-Api-Key)
            return "api_key"
        return "basic_password"

    def _auth_header(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if self._session_token:
            raw = f"{self.user}:{self._session_token}"
            encoded = base64.b64encode(raw.encode()).decode()
            headers["Espo-Authorization"] = encoded
            headers["Espo-Authorization-By-Token"] = "true"
            return headers

        if self._auth_mode == "api_key":
            headers["X-Api-Key"] = self.api_token
            return headers

        # basic_token ou basic_password
        raw = f"{self.user}:{self.password}"
        encoded = base64.b64encode(raw.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
        headers["Espo-Authorization"] = encoded
        return headers

    async def authenticate(self) -> dict[str, Any]:
        """Valida credenciais via GET /App/user e, se possível, guarda token de sessão."""
        data = await self.request("GET", "/App/user")
        token = data.get("token")
        if token:
            self._session_token = token
        user_name = (data.get("user") or {}).get("userName") or self.user
        logger.info(
            "crm_authenticated",
            user=user_name,
            auth_mode=self._auth_mode,
            has_session_token=bool(token),
        )
        return data

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | list[tuple[str, Any]] | None = None,
        json: dict | list | None = None,
    ) -> dict[str, Any]:
        if self._dry_run or not self._enabled:
            from uuid import uuid4

            logger.debug("crm_dry_run", method=method, path=path, auth_mode=self._auth_mode)
            if method.upper() == "GET" and path.rstrip("/").endswith("user"):
                return {"token": "dry-run-token", "user": {"userName": self.user}}
            if method.upper() == "GET":
                return {"total": 0, "list": []}
            payload = json if isinstance(json, dict) else {}
            return {"id": uuid4().hex[:16], "name": payload.get("name", "dry-run")}

        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"

        async def _do() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=self._auth_header(),
                    params=params,
                    json=json,
                )
                # 409 Conflict = duplicado; body traz o registro existente (API Espo)
                if resp.status_code == 409:
                    try:
                        body = resp.json()
                    except Exception:
                        body = None
                    existing = self._extract_conflict_record(body)
                    if existing and existing.get("id"):
                        logger.info(
                            "crm_duplicate_reused",
                            path=path,
                            id=existing.get("id"),
                        )
                        return existing
                    logger.warning(
                        "crm_conflict_no_body",
                        path=path,
                        body=(resp.text or "")[:300],
                    )
                    resp.raise_for_status()

                if resp.status_code >= 400:
                    logger.error(
                        "crm_http_error",
                        status=resp.status_code,
                        path=path,
                        body=resp.text[:500],
                        auth_mode=self._auth_mode,
                    )
                    resp.raise_for_status()
                if resp.status_code == 204 or not resp.content:
                    return {}
                return resp.json()

        return await self._circuit.call(
            lambda: async_retry(
                _do,
                attempts=3,
                exceptions=(httpx.HTTPError, httpx.TransportError),
            )
        )

    @staticmethod
    def _extract_conflict_record(body: Any) -> dict[str, Any] | None:
        """Espo devolve 409 com lista ou objeto do registro duplicado."""
        if isinstance(body, list) and body:
            first = body[0]
            return first if isinstance(first, dict) else None
        if isinstance(body, dict):
            if body.get("id"):
                return body
            # às vezes { "list": [ {...} ] }
            lst = body.get("list")
            if isinstance(lst, list) and lst and isinstance(lst[0], dict):
                return lst[0]
        return None

    async def list(
        self,
        entity: str,
        *,
        max_size: int = 50,
        offset: int = 0,
        where: list | None = None,
        select: str | None = None,
        order_by: str | None = None,
        order: str = "asc",
    ) -> dict[str, Any]:
        """
        Lista com filtros Espo.

        O ``where`` deve ir no formato de query array do Espo
        (where[0][type]=…), não só JSON compactado — o compactado
        era ignorado e a API devolvia o primeiro registro sempre.
        """
        params: list[tuple[str, Any]] = [
            ("maxSize", max_size),
            ("offset", offset),
            ("order", order),
        ]
        if select:
            params.append(("select", select))
        if order_by:
            params.append(("orderBy", order_by))
        if where:
            for i, cond in enumerate(where):
                if not isinstance(cond, dict):
                    continue
                for key, val in cond.items():
                    # where[0][type]=equals&where[0][attribute]=name&where[0][value]=X
                    if isinstance(val, (list, dict)):
                        import json as _json

                        params.append((f"where[{i}][{key}]", _json.dumps(val)))
                    else:
                        params.append((f"where[{i}][{key}]", val))
        return await self.request("GET", f"/{entity}", params=params)

    async def get(self, entity: str, record_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/{entity}/{record_id}")

    async def exists(self, entity: str, record_id: str) -> bool:
        """GET /{Entity}/{id} sem retry/circuit-breaker: 404 aqui é um resultado de
        negócio válido (registro apagado/nunca existiu no CRM), não falha de infra —
        varrer muitos IDs ausentes via ``request()`` abriria o circuit breaker
        (``failure_threshold=5``) e bloquearia o resto da checagem."""
        if self._dry_run or not self._enabled:
            return True
        url = f"{self.base_url}/{entity}/{record_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._auth_header())
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def create(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/{entity}", json=data)

    async def update(self, entity: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", f"/{entity}/{record_id}", json=data)

    async def delete(self, entity: str, record_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/{entity}/{record_id}")

    async def link(
        self,
        entity: str,
        record_id: str,
        link: str,
        related_id: str | None = None,
        related_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /{Entity}/{id}/{link} — vincula registros (API.md §5)."""
        body: dict[str, Any] = {}
        if related_ids is not None:
            body["ids"] = related_ids
        elif related_id:
            body["id"] = related_id
        return await self.request("POST", f"/{entity}/{record_id}/{link}", json=body)

    async def action(
        self,
        entity: str,
        action_name: str,
        *,
        record_id: str | None = None,
        data: dict | None = None,
    ) -> dict[str, Any]:
        """POST /{Entity}/{id}/action/{action} ou /{Entity}/action/{action}."""
        if record_id:
            path = f"/{entity}/{record_id}/action/{action_name}"
        else:
            path = f"/{entity}/action/{action_name}"
        return await self.request("POST", path, json=data or {})

    async def find_one(
        self,
        entity: str,
        field: str,
        value: str,
    ) -> Optional[dict[str, Any]]:
        where = [{"type": "equals", "attribute": field, "value": value}]
        data = await self.list(entity, max_size=1, where=where)
        items = data.get("list") or []
        return items[0] if items else None
