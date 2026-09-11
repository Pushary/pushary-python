"""Keys resource: mint, list, and revoke per-end-user (bound) API keys.

For multi-tenant Partners. Call ``issue`` from your backend with your own
(unbound, Partner-plan) key to mint a child key bound to a single end-user, hand
that key to the agent runtime acting for that user, then ``revoke`` it when the
session ends. A bound key can only create/resolve decisions and enroll that exact
end-user, so a prompt-injected agent can never reach another of your users.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

RequestFn = Callable[..., Dict[str, Any]]


class KeysResource:
    """Obtain an instance from ``PusharyServer(...).keys``. Requires the Partner plan."""

    def __init__(self, request: RequestFn) -> None:
        self._request = request

    def issue(
        self,
        external_id: str,
        *,
        name: Optional[str] = None,
        expires_in_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Mint a key bound to ``external_id``.

        The full key is returned once under ``apiKey`` and cannot be retrieved
        again -- store it as the session credential. ``expires_in_seconds`` is an
        optional auto-expiry (clamped to [60s, 365d]); omit for a key that lives
        until revoked. Returns a dict with ``apiKey``, ``keyPrefix``, ``scope``,
        ``boundExternalId``, and ``expiresAt``.
        """

        body: Dict[str, Any] = {"externalId": external_id}
        if name is not None:
            body["name"] = name
        if expires_in_seconds is not None:
            body["expiresInSeconds"] = expires_in_seconds
        return self._request("POST", "/keys", body=body)

    def list(self) -> List[Dict[str, Any]]:
        """List this site's bound keys (never the secret) for audit/rotation."""

        result = self._request("GET", "/keys")
        keys = result.get("keys")
        return keys if isinstance(keys, list) else []

    def revoke(self, key_prefix: str) -> Dict[str, Any]:
        """Revoke (deactivate) a bound key by its prefix.

        Returns a dict with ``keyPrefix`` and ``revoked`` (False if the prefix is
        unknown or already inactive).
        """

        return self._request("DELETE", f"/keys/{quote(str(key_prefix), safe='')}")
