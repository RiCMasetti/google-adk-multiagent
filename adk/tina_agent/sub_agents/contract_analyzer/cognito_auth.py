"""
Cognito authentication helpers for the Tina MCP server.

The Tina MCP toolset uses ADK's dynamic MCP header provider. Each MCP session
request asks this module for authorization headers, so short-lived Cognito
tokens can be refreshed before expiry without restarting the ADK container.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import boto3
import httpx


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CognitoConfig:
    auth_flow: str
    client_id: str
    client_secret: str | None
    region: str | None
    user_pool_id: str | None
    username: str | None
    password: str | None
    auth_endpoint: str | None
    token_endpoint: str | None
    token_type: str
    scopes: str | None


_TOKEN_CACHE: dict[str, Any] = {}
_TOKEN_LOCK = threading.Lock()
_DEFAULT_REFRESH_MARGIN_SECONDS = 300


def tina_authorization_headers() -> dict[str, str]:
    token = os.environ.get("TINA_MCP_BEARER_TOKEN")
    if not token:
        if not os.environ.get("TINA_COGNITO_CLIENT_ID"):
            return {}
        token = _get_cognito_token()
    return {"Authorization": f"Bearer {token}"}


def tina_authorization_header_provider(_context: Any = None) -> dict[str, str]:
    return tina_authorization_headers()


def _config() -> CognitoConfig:
    return CognitoConfig(
        auth_flow=os.environ.get("TINA_COGNITO_AUTH_FLOW", "USER_PASSWORD_AUTH").strip(),
        client_id=_required("TINA_COGNITO_CLIENT_ID"),
        client_secret=os.environ.get("TINA_COGNITO_CLIENT_SECRET"),
        region=os.environ.get("TINA_COGNITO_REGION") or os.environ.get("AWS_REGION_NAME"),
        user_pool_id=os.environ.get("TINA_COGNITO_USER_POOL_ID"),
        username=os.environ.get("TINA_COGNITO_USERNAME"),
        password=os.environ.get("TINA_COGNITO_PASSWORD"),
        auth_endpoint=os.environ.get("TINA_COGNITO_AUTH_ENDPOINT"),
        token_endpoint=os.environ.get("TINA_COGNITO_TOKEN_ENDPOINT"),
        token_type=os.environ.get("TINA_COGNITO_TOKEN_TYPE", "access_token").strip().lower(),
        scopes=os.environ.get("TINA_COGNITO_SCOPES"),
    )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set for Tina MCP Cognito authentication.")
    return value


def _get_cognito_token() -> str:
    now = time.time()
    cached = _TOKEN_CACHE.get("token")
    refresh_after = float(_TOKEN_CACHE.get("refresh_after") or 0)
    if cached and now < refresh_after:
        return cached

    with _TOKEN_LOCK:
        now = time.time()
        cached = _TOKEN_CACHE.get("token")
        refresh_after = float(_TOKEN_CACHE.get("refresh_after") or 0)
        if cached and now < refresh_after:
            return cached

        cfg = _config()
        if cfg.auth_flow.upper() == "CLIENT_CREDENTIALS":
            token, expires_in = _client_credentials_token(cfg)
        else:
            token, expires_in = _boto3_user_token(cfg)

        lifetime = max(int(expires_in or 3600), 60)
        refresh_margin = _refresh_margin_seconds(lifetime)
        refreshed_at = time.time()
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = refreshed_at + lifetime
        _TOKEN_CACHE["refresh_after"] = refreshed_at + max(lifetime - refresh_margin, 0)
        logger.info(
            "Refreshed Tina MCP Cognito token; expires in %ss, next refresh in %ss.",
            lifetime,
            max(lifetime - refresh_margin, 0),
        )
        return token


def _refresh_margin_seconds(token_lifetime_seconds: int) -> int:
    raw = os.environ.get("TINA_COGNITO_TOKEN_REFRESH_MARGIN_SECONDS")
    if raw is None:
        requested = _DEFAULT_REFRESH_MARGIN_SECONDS
    else:
        try:
            requested = int(raw)
        except ValueError:
            logger.warning(
                "Invalid TINA_COGNITO_TOKEN_REFRESH_MARGIN_SECONDS=%r; using %s.",
                raw,
                _DEFAULT_REFRESH_MARGIN_SECONDS,
            )
            requested = _DEFAULT_REFRESH_MARGIN_SECONDS

    requested = max(requested, 0)
    max_safe_margin = max(token_lifetime_seconds - 30, 0)
    return min(requested, max_safe_margin)


def _boto3_user_token(cfg: CognitoConfig) -> tuple[str, int]:
    if not cfg.region:
        raise RuntimeError("TINA_COGNITO_REGION or AWS_REGION_NAME must be set.")
    if not cfg.username or not cfg.password:
        raise RuntimeError(
            "TINA_COGNITO_USERNAME and TINA_COGNITO_PASSWORD must be set when "
            "using a Cognito user-password auth flow."
        )

    client_kwargs: dict[str, str] = {"region_name": cfg.region}
    if cfg.auth_endpoint:
        client_kwargs["endpoint_url"] = cfg.auth_endpoint
    client = boto3.client("cognito-idp", **client_kwargs)

    auth_parameters = {
        "USERNAME": cfg.username,
        "PASSWORD": cfg.password,
    }
    if cfg.client_secret:
        auth_parameters["SECRET_HASH"] = _secret_hash(
            cfg.username,
            cfg.client_id,
            cfg.client_secret,
        )

    flow = cfg.auth_flow.upper()
    if flow.startswith("ADMIN_"):
        if not cfg.user_pool_id:
            raise RuntimeError(
                "TINA_COGNITO_USER_POOL_ID must be set for admin Cognito auth flows."
            )
        response = client.admin_initiate_auth(
            UserPoolId=cfg.user_pool_id,
            ClientId=cfg.client_id,
            AuthFlow=flow,
            AuthParameters=auth_parameters,
        )
    else:
        response = client.initiate_auth(
            ClientId=cfg.client_id,
            AuthFlow=flow,
            AuthParameters=auth_parameters,
        )

    result = response.get("AuthenticationResult") or {}
    token = _select_token(result, cfg.token_type)
    expires_in = int(result.get("ExpiresIn") or 3600)
    return token, expires_in


def _client_credentials_token(cfg: CognitoConfig) -> tuple[str, int]:
    if not cfg.token_endpoint:
        raise RuntimeError(
            "TINA_COGNITO_TOKEN_ENDPOINT must be set for CLIENT_CREDENTIALS auth."
        )
    if not cfg.client_secret:
        raise RuntimeError(
            "TINA_COGNITO_CLIENT_SECRET must be set for CLIENT_CREDENTIALS auth."
        )

    data = {"grant_type": "client_credentials"}
    if cfg.scopes:
        data["scope"] = _normalize_scopes(cfg.scopes)

    with httpx.Client(timeout=20.0, trust_env=False) as client:
        response = client.post(
            cfg.token_endpoint,
            data=data,
            auth=(cfg.client_id, cfg.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()

    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Cognito token endpoint did not return access_token.")
    return token, int(payload.get("expires_in") or 3600)


def _normalize_scopes(scopes: str) -> str:
    """
    Cognito expects OAuth scopes as a space-delimited string.

    Operators often write env vars as comma-separated lists. Normalize both
    forms so `scope_a, scope_b` is sent as `scope_a scope_b`.
    """
    return " ".join(scope.strip() for scope in scopes.replace(",", " ").split())


def _select_token(result: dict[str, Any], token_type: str) -> str:
    key = "IdToken" if token_type in ("id", "id_token") else "AccessToken"
    token = result.get(key)
    if not token:
        raise RuntimeError(f"Cognito response did not include {key}.")
    return token


def _secret_hash(username: str, client_id: str, client_secret: str) -> str:
    digest = hmac.new(
        client_secret.encode("utf-8"),
        msg=(username + client_id).encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")
