"""HTTP client for the Master Room → coder-bot contract.

The coder-bot is a thin wrapper over the Anthropic Claude API that
lives in a separate service (see ``TZ_MASTER_ROOM_v0.3.md`` §4.5).
Lilush only knows it through this httpx-based async client.

Contract:

* ``POST   /coder/task``         — create a new task. Body:
  ``{prompt, stack, target_id, budget, callback_url, github}``.
  Returns ``{task_id, status, accepted_at}``.

* ``GET    /coder/status``       — query status by ``?task_id=...``.
  Returns ``{task_id, status, progress, deploy_url, preview_url,
  github_url, error}``.

* ``GET    /coder/task/<id>/logs?lines=N`` — fetch tail of live logs.
  Returns ``{task_id, lines: [str], truncated: bool}``.

* ``POST   /coder/task/<id>/cancel`` — cancel a queued/running task.
  Returns ``{task_id, status}``.

* ``POST   /coder/task/<id>/redeploy?target_id=...`` — redeploy an
  existing project to a different target. Returns ``{task_id,
  status}``.

* ``GET    /coder/health``       — server liveness; no auth required.
  Returns ``{ok, version, claude_model, queue_depth}``.

All authenticated calls send ``Authorization: Bearer <server.token>``.

Until the real coder-bot exists, ``MasterRoomCoderClient`` is also the
seam tests monkey-patch (``client.post / client.get``) to inject mock
responses for the FSM and HTTP-tests.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class CoderClientError(RuntimeError):
    """Raised when the coder-bot returns a non-2xx, times out, or
    drops the connection. Carries ``status`` (int or None) and
    ``payload`` (parsed JSON or None) for the UI layer to render.
    """

    def __init__(self, message: str, status: int | None = None,
                 payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class MasterRoomCoderClient:
    """Async client to a single coder-bot server.

    Always created with the *currently active* server dict pulled
    from :mod:`bot.addons.masterroom.state` — never pickles the
    server, so token rotation between calls is picked up immediately.
    """

    def __init__(self, server: dict[str, Any],
                 timeout: float = DEFAULT_TIMEOUT,
                 client: Optional[httpx.AsyncClient] = None):
        self._url = str(server.get("url", "")).rstrip("/")
        self._token = str(server.get("token", ""))
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._url

    async def __aenter__(self) -> "MasterRoomCoderClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- low-level ----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self._url}{path}"
        try:
            r = await self._client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            raise CoderClientError(f"GET {path} failed: {e}") from e
        return _parse_json(r, f"GET {path}")

    async def _post(self, path: str, json: dict | None = None,
                    params: dict | None = None) -> Any:
        url = f"{self._url}{path}"
        try:
            r = await self._client.post(url, json=json, params=params,
                                        headers=self._headers())
        except httpx.HTTPError as e:
            raise CoderClientError(f"POST {path} failed: {e}") from e
        return _parse_json(r, f"POST {path}")

    # --- public API ---------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return await self._get("/coder/health")

    async def create_task(
        self,
        prompt: str,
        stack: str,
        target_id: str,
        budget: int,
        callback_url: str,
        github: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "prompt": prompt,
            "stack": stack,
            "target_id": target_id,
            "budget": budget,
            "callback_url": callback_url,
        }
        if github is not None:
            body["github"] = github
        if title:
            body["title"] = title
        return await self._post("/coder/task", json=body)

    async def get_status(self, task_id: str) -> dict[str, Any]:
        return await self._get("/coder/status", params={"task_id": task_id})

    async def get_logs(self, task_id: str, lines: int = 100) -> dict[str, Any]:
        return await self._get(f"/coder/task/{task_id}/logs", params={"lines": int(lines)})

    async def cancel(self, task_id: str) -> dict[str, Any]:
        return await self._post(f"/coder/task/{task_id}/cancel")

    async def redeploy(self, task_id: str, target_id: str) -> dict[str, Any]:
        return await self._post(f"/coder/task/{task_id}/redeploy",
                                params={"target_id": target_id})


def _parse_json(response: httpx.Response, ctx: str) -> Any:
    """Normalise httpx Response → dict, raise CoderClientError on 4xx/5xx."""
    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {"text": response.text[:200]}
        raise CoderClientError(
            f"{ctx} returned {response.status_code}",
            status=response.status_code,
            payload=payload,
        )
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        # Non-JSON 2xx — coder shouldn't do this but don't blow up.
        return {"raw": response.text[:200]}
