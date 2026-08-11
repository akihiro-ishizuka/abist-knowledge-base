"""アプリ画面キャプチャの仕様（CaptureProfile / CaptureRequest）。

**設計の中心は「起動コマンドを外から受け取らない」こと。**

MCP・API・CLI・LLM のいずれからも生の `launch.command` は受け付けない。
運用者が事前に `config/capture-profiles.json` へ登録した**プロファイル名**だけを
受け取り、実行するコマンドはそのファイルの中身に固定する。これがないと
「動画を作って」という依頼が任意コード実行になる。

さらにキャプチャ機能は**既定で無効**（`ABIST_KB_VIDEO_CAPTURE_ENABLED=1` で有効）。
無効のまま、あるいはプロファイル未登録のままでも**動画生成は完走する**
（該当シーンは図解／プレースホルダで代替する）。

純関数のみ。ファイルの読み込みは `application/video/capture_planner.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

#: 起動方式。`web` は Playwright、`desktop` はウィンドウ撮影、`cli` は出力の画像化。
LAUNCH_TYPES: tuple[str, ...] = ("web", "desktop", "cli")

#: プロファイル名（ファイル名にもなるので厳しめに縛る）。
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
#: shot id（`captures/<id>.png` になる）。
_SHOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
#: 取得元。`https://` / `ssh://` / `git@host:path` / ローカルの絶対パス を許す。
#: **`ext::` / `file://` / `http://` / `git://` は許さない** ——
#: `ext::` は git のコマンド実行トランスポートそのもの（登録ミスがそのまま
#: 任意コード実行になる）、残りは平文・意図の曖昧さを避けるため。
#: なお、この値は運用者が登録したレジストリからしか来ない（MCP/API 経由では
#: 渡せない）ので、ローカルパスを許しても外部入力の混入経路にはならない。
_REPO_URL_RE = re.compile(r"^(?:https://|ssh://|git@)[^\s]+$")
_LOCAL_REPO_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|[\\/])[^\s]*$")
_FORBIDDEN_REPO_PREFIXES = ("ext::", "file://", "http://", "git://", "-")
#: branch / tag / commit。git のリビジョン表記として安全な文字だけ。
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

#: 起動待ちの上限（秒）。長すぎるとジョブが張り付くので上限を設ける。
MAX_READY_TIMEOUT_SEC = 180.0
DEFAULT_READY_TIMEOUT_SEC = 60.0
#: 1プロファイルあたりの shot 上限。
MAX_SHOTS = 20


@dataclass(frozen=True, slots=True)
class CaptureError:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class Shot:
    """1枚の撮影指示。"""

    id: str
    scene_id: str | None = None
    selector: str | None = None
    wait_ms: int = 0
    mask: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptureProfile:
    """運用者が登録した撮影プロファイル（**唯一の起動コマンドの出どころ**）。"""

    name: str
    repo_url: str
    ref: str
    launch_type: str
    command: tuple[str, ...]
    url: str | None = None
    ready_timeout_sec: float = DEFAULT_READY_TIMEOUT_SEC
    shots: tuple[Shot, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repo": {"url": self.repo_url, "ref": self.ref},
            "launch": {
                "type": self.launch_type,
                "command": list(self.command),
                "url": self.url,
                "ready_timeout_sec": self.ready_timeout_sec,
            },
            "shots": [
                {
                    "id": s.id,
                    "scene_id": s.scene_id,
                    "selector": s.selector,
                    "wait_ms": s.wait_ms,
                    "mask": list(s.mask),
                }
                for s in self.shots
            ],
        }


@dataclass(frozen=True, slots=True)
class ProfileResult:
    ok: bool
    profile: CaptureProfile | None = None
    errors: list[CaptureError] = field(default_factory=list)


def _is_allowed_repo_url(value: Any) -> bool:
    """取得元として許す形かどうか。

    `ext::<command>` を弾くことがこの関数の一番の目的。git は `ext::` 経由で
    任意のコマンドをトランスポートとして起動できるので、登録ミス1つで
    「読み取り専用」の前提が崩れる。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    lowered = candidate.lower()
    if any(lowered.startswith(prefix) for prefix in _FORBIDDEN_REPO_PREFIXES):
        return False
    return bool(_REPO_URL_RE.match(candidate) or _LOCAL_REPO_RE.match(candidate))


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_shots(raw: Any, errors: list[CaptureError]) -> tuple[Shot, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(CaptureError("shots", "invalid", "shots は配列で指定してください"))
        return ()
    if len(raw) > MAX_SHOTS:
        errors.append(
            CaptureError("shots", "invalid", f"shots は {MAX_SHOTS} 件以内にしてください")
        )
        return ()

    shots: list[Shot] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        at = f"shots[{index}]"
        if not isinstance(entry, dict):
            errors.append(CaptureError(at, "invalid", "shot はオブジェクトで指定してください"))
            continue
        shot_id = entry.get("id")
        if not isinstance(shot_id, str) or not _SHOT_ID_RE.match(shot_id):
            errors.append(
                CaptureError(
                    f"{at}.id",
                    "invalid",
                    "id は英小文字・数字・ハイフン・アンダースコアで指定してください"
                    "（成果物のファイル名になります）",
                )
            )
            continue
        if shot_id in seen:
            errors.append(CaptureError(f"{at}.id", "duplicate_id", f'"{shot_id}" が重複しています'))
            continue
        seen.add(shot_id)

        wait_ms = entry.get("wait_ms", 0)
        if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) or not (0 <= wait_ms <= 30000):
            errors.append(
                CaptureError(f"{at}.wait_ms", "invalid", "wait_ms は 0〜30000 の整数です")
            )
            wait_ms = 0

        mask = entry.get("mask") or []
        if not isinstance(mask, list) or not all(_is_str(m) for m in mask):
            errors.append(CaptureError(f"{at}.mask", "invalid", "mask は文字列配列です"))
            mask = []

        shots.append(
            Shot(
                id=shot_id,
                scene_id=entry.get("scene_id") if _is_str(entry.get("scene_id")) else None,
                selector=entry.get("selector") if _is_str(entry.get("selector")) else None,
                wait_ms=wait_ms,
                mask=tuple(mask),
            )
        )
    return tuple(shots)


def validate_capture_profile(name: Any, raw: Any) -> ProfileResult:
    """登録済みプロファイル1件を検証する。

    **`command` は配列で受け取る。** 文字列を受け付けると `shell=True` 相当の
    解釈が必要になり、登録内容そのものがインジェクション面になる。
    """
    errors: list[CaptureError] = []
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        errors.append(
            CaptureError(
                "name",
                "invalid",
                "プロファイル名は英小文字・数字・ハイフン・アンダースコア 1〜63 文字です",
            )
        )
    if not isinstance(raw, dict):
        errors.append(CaptureError("", "invalid", "プロファイルはオブジェクトで指定してください"))
        return ProfileResult(ok=False, errors=errors)

    repo = raw.get("repo")
    repo_url, ref = "", "main"
    if not isinstance(repo, dict):
        errors.append(CaptureError("repo", "invalid", "repo はオブジェクトで指定してください"))
    else:
        repo_url = repo.get("url") if isinstance(repo.get("url"), str) else ""
        if not _is_allowed_repo_url(repo_url):
            errors.append(
                CaptureError(
                    "repo.url",
                    "invalid",
                    "repo.url は https:// / ssh:// / git@host:path、"
                    "またはローカルの絶対パスで指定してください"
                    "（ext:: / file:// / http:// / git:// は使えません）",
                )
            )
        ref = repo.get("ref") if isinstance(repo.get("ref"), str) else "main"
        if not _REF_RE.match(ref or ""):
            errors.append(
                CaptureError("repo.ref", "invalid", "repo.ref に使えない文字が含まれています")
            )

    launch = raw.get("launch")
    launch_type, command, url = "", (), None
    ready_timeout = DEFAULT_READY_TIMEOUT_SEC
    if not isinstance(launch, dict):
        errors.append(CaptureError("launch", "invalid", "launch はオブジェクトで指定してください"))
    else:
        launch_type = launch.get("type")
        if launch_type not in LAUNCH_TYPES:
            errors.append(
                CaptureError(
                    "launch.type",
                    "invalid",
                    f"launch.type は {' / '.join(LAUNCH_TYPES)} のいずれかです",
                )
            )
        raw_command = launch.get("command")
        if isinstance(raw_command, str):
            errors.append(
                CaptureError(
                    "launch.command",
                    "invalid",
                    "launch.command は配列で指定してください"
                    "（文字列を受け付けるとシェル解釈が必要になり、"
                    "登録内容そのものが注入面になります）",
                )
            )
        elif (
            not isinstance(raw_command, list)
            or not raw_command
            or not all(_is_str(part) for part in raw_command)
        ):
            errors.append(
                CaptureError("launch.command", "invalid", "launch.command は非空の文字列配列です")
            )
        else:
            command = tuple(raw_command)

        url = launch.get("url") if _is_str(launch.get("url")) else None
        if launch_type == "web" and not url:
            errors.append(CaptureError("launch.url", "invalid", "web では launch.url が必要です"))

        raw_timeout = launch.get("ready_timeout_sec", DEFAULT_READY_TIMEOUT_SEC)
        if not isinstance(raw_timeout, int | float) or isinstance(raw_timeout, bool):
            errors.append(CaptureError("launch.ready_timeout_sec", "invalid", "数値で指定します"))
        elif not (0 < raw_timeout <= MAX_READY_TIMEOUT_SEC):
            errors.append(
                CaptureError(
                    "launch.ready_timeout_sec",
                    "invalid",
                    f"ready_timeout_sec は 0 より大きく {MAX_READY_TIMEOUT_SEC} 秒以下です",
                )
            )
        else:
            ready_timeout = float(raw_timeout)

    shots = _validate_shots(raw.get("shots"), errors)
    if not shots:
        errors.append(CaptureError("shots", "invalid", "shots を1件以上登録してください"))

    if errors:
        return ProfileResult(ok=False, errors=errors)
    return ProfileResult(
        ok=True,
        profile=CaptureProfile(
            name=str(name),
            repo_url=repo_url,
            ref=ref,
            launch_type=str(launch_type),
            command=command,
            url=url,
            ready_timeout_sec=ready_timeout,
            shots=shots,
        ),
    )


def is_capture_enabled(env: dict[str, str]) -> bool:
    """キャプチャ機能が有効か（**既定は無効**）。

    `"1" / "true" / "yes"` のときだけ有効。誤って有効になることを避けるため、
    未設定・空文字・その他の値はすべて無効に倒す。
    """
    raw = (env.get("ABIST_KB_VIDEO_CAPTURE_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = [
    "DEFAULT_READY_TIMEOUT_SEC",
    "LAUNCH_TYPES",
    "MAX_READY_TIMEOUT_SEC",
    "MAX_SHOTS",
    "SCHEMA_VERSION",
    "CaptureError",
    "CaptureProfile",
    "ProfileResult",
    "Shot",
    "is_capture_enabled",
    "validate_capture_profile",
]
