"""Async HTTP client for the X1 API + subgraph.

Endpoint paths and payload shapes mirror the original X1-Ecochain-BOT.
If the upstream API changes, only this file needs updating — chain.py and
main.py talk to the bot through this client's high-level methods.

Proxy support:
- HTTP/HTTPS proxies use aiohttp's `proxy=` kwarg
- SOCKS proxies use ProxyConnector (aiohttp-socks)
"""
from __future__ import annotations

import json
from typing import Any, Optional

import aiohttp

from .printer import Printer
from .utils import retry


class APIError(Exception):
    """Raised on non-2xx responses or unexpected payload shape."""


def _is_socks(url: str) -> bool:
    return url.startswith(("socks5://", "socks4://"))


class X1Client:
    def __init__(
        self,
        api_base: str,
        subgraph_url: str,
        proxy_url: Optional[str] = None,
        printer: Optional[Printer] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.subgraph_url = subgraph_url
        self.proxy_url = proxy_url
        self.printer = printer or Printer()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: Optional[str] = None

    async def __aenter__(self) -> "X1Client":
        if self.proxy_url and _is_socks(self.proxy_url):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError as e:
                raise RuntimeError(
                    "PROXY_URL is SOCKS but aiohttp-socks is not installed. "
                    "Run: pip install aiohttp-socks"
                ) from e
            connector = ProxyConnector.from_url(self.proxy_url)
        else:
            connector = aiohttp.TCPConnector()
        self._session = aiohttp.ClientSession(connector=connector, timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def set_token(self, token: Optional[str]) -> None:
        self._token = token

    def _proxy_kwarg(self) -> dict[str, Any]:
        if self.proxy_url and not _is_socks(self.proxy_url):
            return {"proxy": self.proxy_url}
        return {}

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "safe-x1-eco-bot/0.1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("X1Client must be used as an async context manager")
        headers = self._headers({"Content-Type": "application/json"} if json_body else None)
        if extra_headers:
            headers.update(extra_headers)
        async with self._session.request(
            method,
            url,
            json=json_body,
            params=params,
            headers=headers,
            **self._proxy_kwarg(),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise APIError(f"{method} {url} -> {resp.status}: {text[:500]}")
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    # --- Auth ---

    @retry(attempts=3, base_delay=3.0)
    async def auth_message(self, address: str) -> str:
        """Get the message that needs to be signed for SIWE auth."""
        data = await self._request("GET", f"{self.api_base}/auth/message", params={"address": address})
        if isinstance(data, dict) and "message" in data:
            return data["message"]
        raise APIError(f"unexpected auth_message payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def auth_signin(self, address: str, signature: str) -> str:
        """Submit signature, receive bearer token."""
        data = await self._request(
            "POST",
            f"{self.api_base}/auth/signin",
            json_body={"address": address, "signature": signature},
        )
        if isinstance(data, dict) and "token" in data:
            return data["token"]
        raise APIError(f"unexpected auth_signin payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def auth_me(self) -> dict[str, Any]:
        return await self._request("GET", f"{self.api_base}/auth/me")

    @retry(attempts=3, base_delay=3.0)
    async def auth_nonce(self) -> str:
        data = await self._request("GET", f"{self.api_base}/auth/nonce")
        if isinstance(data, dict) and "nonce" in data:
            return data["nonce"]
        raise APIError(f"unexpected auth_nonce payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def auth_verify(self, address: str, signature: str, nonce: str) -> bool:
        data = await self._request(
            "POST",
            f"{self.api_base}/auth/verify",
            json_body={"address": address, "signature": signature, "nonce": nonce},
        )
        return bool(isinstance(data, dict) and data.get("ok"))

    # --- Quests + faucet ---

    @retry(attempts=3, base_delay=3.0)
    async def quests_list(self) -> list[dict[str, Any]]:
        data = await self._request("GET", f"{self.api_base}/quests")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("quests"), list):
            return data["quests"]
        raise APIError(f"unexpected quests_list payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def request_faucet(self, address: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"{self.api_base}/faucet", json_body={"address": address}
        )

    @retry(attempts=3, base_delay=3.0)
    async def complete_quest(self, quest_id: str, tx_hash: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"quest_id": quest_id}
        if tx_hash:
            body["tx_hash"] = tx_hash
        return await self._request("POST", f"{self.api_base}/quests/complete", json_body=body)

    # --- Deploy support ---

    @retry(attempts=3, base_delay=3.0)
    async def save_contracts(
        self,
        address: str,
        token_address: str,
        name: str,
        symbol: str,
        constructor_args: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{self.api_base}/contracts",
            json_body={
                "owner": address,
                "address": token_address,
                "name": name,
                "symbol": symbol,
                "constructor_args": constructor_args,
            },
        )

    # --- Subgraph ---

    @retry(attempts=3, base_delay=3.0)
    async def pool_by_tokens(self, token0: str, token1: str, fee: int) -> Optional[str]:
        """Look up Uniswap V3 pool address from the subgraph."""
        a, b = sorted([token0.lower(), token1.lower()])
        query = """
        query Pool($t0: String!, $t1: String!, $fee: Int!) {
          pools(where: {token0: $t0, token1: $t1, feeTier: $fee}, first: 1) {
            id
          }
        }
        """
        if self._session is None:
            raise RuntimeError("X1Client must be used as an async context manager")
        async with self._session.post(
            self.subgraph_url,
            json={"query": query, "variables": {"t0": a, "t1": b, "fee": fee}},
            headers={"Content-Type": "application/json"},
            **self._proxy_kwarg(),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise APIError(f"subgraph -> {resp.status}: {text[:500]}")
            data = json.loads(text)
        pools = data.get("data", {}).get("pools") or []
        if not pools:
            return None
        return pools[0]["id"]
