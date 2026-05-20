"""Async HTTP client for the X1 testnet API + DEX subgraph.

Endpoint paths and payload shapes are matched to the actual X1 testnet
backend as observed in vonssy/X1-Ecochain-BOT bot.py. The X1 API uses a
non-standard scheme on two points:

  1. Authorization header is the raw token, not "Bearer <token>"
  2. complete_quest is POST /quests with quest_id as a *query string*
     parameter, not a JSON body

If the upstream API changes, only this file needs updating.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import aiohttp

from . import constants as C
from .printer import Printer
from .utils import retry


class APIError(Exception):
    """Raised on non-2xx responses or unexpected payload shape."""


def _is_socks(url: str) -> bool:
    return url.startswith(("socks5://", "socks4://"))


# Headers the X1 web app sends. We mirror them so the API doesn't reject us
# for looking like a non-browser client.
_BASE_HEADERS = {
    "Accept": "*/*",
    "Origin": "https://testnet.x1ecochain.com",
    "Referer": "https://testnet.x1ecochain.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}

_DEX_HEADERS_OVERRIDE = {
    "Origin": "https://ecodex.one",
    "Referer": "https://ecodex.one/",
}


class X1Client:
    def __init__(
        self,
        api_base: str,
        subgraph_url: str,
        nft_api_base: Optional[str] = None,
        proxy_url: Optional[str] = None,
        printer: Optional[Printer] = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.nft_api_base = (nft_api_base or C.API_NFT).rstrip("/")
        self.subgraph_base = subgraph_url.rstrip("/")
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
        """X1 sends the token raw in the Authorization header (no Bearer prefix)."""
        self._token = token

    def _proxy_kwarg(self) -> dict[str, Any]:
        if self.proxy_url and not _is_socks(self.proxy_url):
            return {"proxy": self.proxy_url}
        return {}

    def _headers(self, *, with_auth: bool = False, json_body: bool = False, dex: bool = False) -> dict[str, str]:
        headers = dict(_BASE_HEADERS)
        if dex:
            headers.update(_DEX_HEADERS_OVERRIDE)
        if json_body:
            headers["Content-Type"] = "application/json"
        if with_auth:
            if not self._token:
                raise APIError("auth required but no token set; call auth_signin first")
            headers["Authorization"] = self._token
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("X1Client must be used as an async context manager")
        async with self._session.request(
            method, url, json=json_body, params=params, headers=headers, **self._proxy_kwarg()
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
        """GET /signin?address=X -> { message: ... }"""
        data = await self._request(
            "GET",
            f"{self.api_base}/signin",
            headers=self._headers(json_body=True),
            params={"address": address},
        )
        if isinstance(data, dict) and "message" in data:
            return data["message"]
        raise APIError(f"unexpected /signin GET payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def auth_signin(self, address: str, signature: str, ref_code: str = "") -> str:
        """POST /signin {signature, address, ref_code} -> { token: ... }"""
        body: dict[str, Any] = {"signature": signature, "address": address}
        if ref_code:
            body["ref_code"] = ref_code
        data = await self._request(
            "POST",
            f"{self.api_base}/signin",
            headers=self._headers(json_body=True),
            json_body=body,
        )
        if isinstance(data, dict) and "token" in data:
            return data["token"]
        raise APIError(f"unexpected /signin POST payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def auth_me(self) -> dict[str, Any]:
        """GET /me -> { points, ref_points, rank, referral_rank, ... }"""
        return await self._request(
            "GET", f"{self.api_base}/me", headers=self._headers(with_auth=True, json_body=True)
        )

    # --- Quests + faucet ---

    @retry(attempts=3, base_delay=3.0)
    async def quests_list(self) -> list[dict[str, Any]]:
        """GET /quests -> [ {id, title, type, reward, periodicity, ...}, ... ]"""
        data = await self._request(
            "GET", f"{self.api_base}/quests", headers=self._headers(with_auth=True, json_body=True)
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("quests"), list):
            return data["quests"]
        raise APIError(f"unexpected /quests payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def request_faucet(self, address: str) -> bool:
        """GET <nft_api>/testnet/faucet?address=X — succeeds with empty body or 'try again later'."""
        if self._session is None:
            raise RuntimeError("X1Client must be used as an async context manager")
        url = f"{self.nft_api_base}/testnet/faucet"
        async with self._session.get(
            url,
            headers=self._headers(with_auth=True, json_body=True),
            params={"address": address},
            **self._proxy_kwarg(),
        ) as resp:
            text = await resp.text()
            if resp.status == 500 and "try again later" in text.lower():
                self.printer.warn("faucet on cooldown — counted as success")
                return True
            if resp.status >= 400:
                raise APIError(f"faucet -> {resp.status}: {text[:500]}")
            return True

    @retry(attempts=3, base_delay=3.0)
    async def complete_quest(self, quest_id: str) -> dict[str, Any]:
        """POST /quests?quest_id=X (quest_id is a *query string* param)."""
        return await self._request(
            "POST",
            f"{self.api_base}/quests",
            headers=self._headers(with_auth=True, json_body=True),
            params={"quest_id": quest_id},
        )

    # --- Token Constructor (separate auth) ---

    @retry(attempts=3, base_delay=3.0)
    async def auth_nonce(self, address: str) -> str:
        """GET <constructor>/api/v1/auth/nonce?address=X -> { nonce: ... }"""
        data = await self._request(
            "GET",
            f"{C.API_CONSTRUCTOR}/api/v1/auth/nonce",
            headers=self._constructor_headers(),
            params={"address": address},
        )
        if isinstance(data, dict) and "nonce" in data:
            return data["nonce"]
        raise APIError(f"unexpected nonce payload: {data!r}")

    @retry(attempts=3, base_delay=3.0)
    async def auth_verify(self, address: str, signature: str, message: str) -> dict[str, Any]:
        """POST <constructor>/api/v1/auth/verify."""
        return await self._request(
            "POST",
            f"{C.API_CONSTRUCTOR}/api/v1/auth/verify",
            headers=self._constructor_headers(json_body=True),
            json_body={"address": address, "signature": signature, "message": message},
        )

    @retry(attempts=3, base_delay=3.0)
    async def save_contracts(
        self, address: str, token_address: str, name: str, symbol: str
    ) -> dict[str, Any]:
        """Notify constructor backend of a freshly deployed token (so it shows up in the UI)."""
        return await self._request(
            "POST",
            f"{C.API_CONSTRUCTOR}/api/v1/contracts",
            headers=self._constructor_headers(json_body=True),
            json_body={"address": address, "token_address": token_address, "name": name, "symbol": symbol},
        )

    # --- DEX subgraph ---

    @retry(attempts=3, base_delay=3.0)
    async def pool_by_tokens(
        self, token_a: str, token_b: str
    ) -> Optional[list[dict[str, Any]]]:
        """Query `ms.kod.af/subgraphs/name/uniswap-v3` for pools containing tokens a and b."""
        if self._session is None:
            raise RuntimeError("X1Client must be used as an async context manager")
        query = """
        query PoolByTokens($a: String!, $b: String!) {
          pools(
            where: { token0_in: [$a, $b], token1_in: [$a, $b] }
            first: 5
          ) {
            id
            feeTier
            sqrtPrice
            liquidity
            tick
            token0 { id symbol name decimals }
            token1 { id symbol name decimals }
          }
        }
        """
        url = f"{self.subgraph_base}/subgraphs/name/uniswap-v3"
        async with self._session.post(
            url,
            headers=self._headers(json_body=True, dex=True),
            json={"query": query, "variables": {"a": token_a.lower(), "b": token_b.lower()}, "operationName": "PoolByTokens"},
            **self._proxy_kwarg(),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise APIError(f"subgraph -> {resp.status}: {text[:500]}")
            data = json.loads(text)
        return (data.get("data") or {}).get("pools")

    # --- Internals ---

    def _constructor_headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Origin": "https://constructor.x1ecochain.com",
            "Referer": "https://constructor.x1ecochain.com/",
            "User-Agent": _BASE_HEADERS["User-Agent"],
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers
