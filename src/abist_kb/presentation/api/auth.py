"""非ローカル公開時のアクセストークン認証(設計書 §7.1, §12)。

「既定は`127.0.0.1`へバインドする。`0.0.0.0`へ公開する場合はアクセストークン
または リバースプロキシ認証を必須とする」。このモジュールはアクセストークン側
を実装する(リバースプロキシ認証は既に上流で認証済みという前提のため、この
アプリケーションからは関与しない — 運用者がリバースプロキシ経路を選ぶ場合は
`access_token=None` のまま bind_host を非ループバックにできるよう、
強制はミドルウェア登録時の呼び出し側の判断に委ねる)。
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

TOKEN_HEADER = "X-API-Token"


def is_loopback_host(host: str) -> bool:
    """バインドホストがループバックか判定する(§7.1: 既定 `127.0.0.1`)。"""
    return host in _LOOPBACK_HOSTS


class AccessTokenMiddleware:
    """`bind_host` が非ループバックなら `TOKEN_HEADER` の一致を必須にする ASGI ミドルウェア。

    ループバードバインド時はトークン未設定でも通す(ローカル利用者しか
    到達できない前提)。非ループバック公開時に `access_token` が未設定のまま
    起動しようとした場合は `require_token_when_exposed()` で起動時に弾く
    (ここでは「誰も入れない」フェイルクローズではなく、起動側の責務とする)。
    """

    def __init__(self, app: ASGIApp, *, bind_host: str, access_token: str | None) -> None:
        self._app = app
        self._enforce = not is_loopback_host(bind_host) and access_token is not None
        self._access_token = access_token

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not self._enforce:
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if request.url.path == "/api/v1/health":
            await self._app(scope, receive, send)
            return

        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, self._access_token or ""):
            response: Response = JSONResponse(
                {
                    "code": "UNAUTHORIZED",
                    "message": "アクセストークンが不正または未指定です。",
                    "hint": f"{TOKEN_HEADER} ヘッダーへ有効なトークンを指定してください。",
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def require_token_when_exposed(*, bind_host: str, access_token: str | None) -> None:
    """非ループバック bind でトークン未設定なら起動時に例外にする(§12 のフェイルセーフ)。

    リバースプロキシ認証を使う運用は `ABIST_KB_TRUST_REVERSE_PROXY=1` 相当の
    明示フラグ経由でこのチェックをスキップする設計にできるが、本タスクの
    スコープでは既定を安全側(トークン必須)に倒す。
    """
    if not is_loopback_host(bind_host) and not access_token:
        raise ValueError(
            f"bind_host={bind_host!r} は非ループバックです。"
            "access_token を指定するか、リバースプロキシ側で認証してください"
            "(design/system-design.md §7.1)。"
        )


__all__ = [
    "TOKEN_HEADER",
    "AccessTokenMiddleware",
    "is_loopback_host",
    "require_token_when_exposed",
]
