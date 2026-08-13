"""持ち込み画像の取り込み（作者が用意した図・スクリーンショット）。

台本を書くエージェントは、組み込みテンプレートで描けない図を外部で作って持ち込む。
その画像は**プロジェクトの中へ複製し、sha256 とライセンスを記録する**。原本は
リポジトリ外にあってよいが、成果物側だけを見て「この画は何で、誰のものか」を
辿れる状態にしておく（社内配布物の来歴を後から復元できないと困る）。

キャプチャ経路（`capture_planner`）が付ける image beat とは別レーン。あちらは
`path` を直接埋めるので、ここは `asset_id` 参照だけを見る。

画像の中身に機密が写っていないかは機械では判定できない。QA の人間ゲート
（`preview_approval`）の確認項目として残す。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: プロジェクト内での置き場（`project-spec.json` からの相対）。
IMAGE_ASSETS_DIR = "assets/images"
#: 受け付ける画像形式。
ALLOWED_IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
#: 1枚あたりの上限。動画1シーンに載る図としては十分で、成果物ツリーが太らない値。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: アセット id に使える文字（そのままファイル名になるので厳しく縛る）。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ImageIngestResult:
    ok: bool
    #: `{id, path, source_path, sha256, bytes, license, attribution, caption}`。
    assets: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _error(index: int, code: str, message: str, asset_id: str | None = None) -> dict[str, str]:
    error = {"path": f"image_assets[{index}]", "code": code, "message": message}
    if asset_id:
        error["assetId"] = asset_id
    return error


def ingest_image_assets(raw_assets: list[dict[str, Any]], project_dir: Path) -> ImageIngestResult:
    """持ち込み画像を検証し、プロジェクト内へ複製して来歴を返す。

    1件でも不正なら**何も複製せずに**失敗を返す（半分だけ取り込まれた状態を作らない）。
    """
    if not raw_assets:
        return ImageIngestResult(ok=True)

    errors: list[dict[str, str]] = []
    planned: list[tuple[dict[str, Any], Path, bytes]] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_assets):
        if not isinstance(raw, dict):
            errors.append(_error(index, "INVALID_IMAGE_ASSET", "オブジェクトで指定します"))
            continue
        asset_id = str(raw.get("id") or "")
        if not _ID_RE.match(asset_id):
            errors.append(
                _error(
                    index,
                    "INVALID_IMAGE_ASSET_ID",
                    "id は英数字・ハイフン・アンダースコアの 1〜64 文字です",
                    asset_id or None,
                )
            )
            continue
        if asset_id in seen_ids:
            errors.append(
                _error(
                    index, "DUPLICATE_IMAGE_ASSET_ID", f"id が重複しています: {asset_id}", asset_id
                )
            )
            continue
        seen_ids.add(asset_id)

        license_text = str(raw.get("license") or "").strip()
        if not license_text:
            errors.append(
                _error(
                    index,
                    "IMAGE_ASSET_LICENSE_REQUIRED",
                    "license は必須です（出所の分からない画像は載せられません）",
                    asset_id,
                )
            )
            continue

        source = Path(str(raw.get("path") or ""))
        suffix = source.suffix.lower()
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            errors.append(
                _error(
                    index,
                    "IMAGE_ASSET_UNSUPPORTED_FORMAT",
                    f"対応形式は {' / '.join(ALLOWED_IMAGE_SUFFIXES)} です"
                    f"（指定: {suffix or '不明'}）",
                    asset_id,
                )
            )
            continue
        if not source.is_file():
            errors.append(
                _error(index, "IMAGE_ASSET_NOT_FOUND", f"画像がありません: {source}", asset_id)
            )
            continue
        if source.stat().st_size > MAX_IMAGE_BYTES:
            errors.append(
                _error(
                    index,
                    "IMAGE_ASSET_TOO_LARGE",
                    f"1枚あたり {MAX_IMAGE_BYTES // (1024 * 1024)}MB までです",
                    asset_id,
                )
            )
            continue

        payload = source.read_bytes()
        entry: dict[str, Any] = {
            "id": asset_id,
            "path": f"{IMAGE_ASSETS_DIR}/{asset_id}{suffix}",
            "source_path": str(source),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "license": license_text,
            "origin": "author_supplied",
        }
        for optional in ("caption", "attribution"):
            value = raw.get(optional)
            if isinstance(value, str) and value.strip():
                entry[optional] = value.strip()
        planned.append((entry, source, payload))

    if errors:
        return ImageIngestResult(ok=False, errors=errors)

    target_dir = project_dir / IMAGE_ASSETS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for entry, source, _payload in planned:
        shutil.copyfile(source, project_dir / entry["path"])
    return ImageIngestResult(ok=True, assets=[entry for entry, _s, _p in planned])


def validate_image_asset_refs(
    scenes: list[dict[str, Any]], *, registered_ids: set[str]
) -> list[dict[str, str]]:
    """台本の image beat が、登録済みのアセットだけを指しているか調べる。"""
    errors: list[dict[str, str]] = []
    for scene in scenes:
        scene_id = str(scene.get("id") or "?")
        for index, beat in enumerate((scene.get("scene_spec") or {}).get("beats") or []):
            if not isinstance(beat, dict) or beat.get("type") != "image":
                continue
            asset_id = beat.get("asset_id")
            if asset_id is None:
                continue  # キャプチャ経路が `path` を直接埋めた beat
            if str(asset_id) not in registered_ids:
                errors.append(
                    {
                        "sceneId": scene_id,
                        "path": f"scenes[{scene_id}].beats[{index}].asset_id",
                        "code": "UNKNOWN_IMAGE_ASSET",
                        "message": f"未登録の画像を参照しています: {asset_id}",
                    }
                )
    return errors


def attach_image_assets(scenes: list[dict[str, Any]], assets: list[dict[str, Any]]) -> None:
    """`asset_id` 参照を、SceneSpec が読める相対パスへ解決する（その場で書き換える）。

    `image.path` は **scene-spec.json からの相対パス**（`capture_planner._attach` と
    同じ約束）なので、プロジェクト直下からの相対を `../../` で登り直す。
    """
    by_id = {asset["id"]: asset for asset in assets}
    for scene in scenes:
        for beat in (scene.get("scene_spec") or {}).get("beats") or []:
            if not isinstance(beat, dict) or beat.get("type") != "image":
                continue
            asset = by_id.get(str(beat.get("asset_id") or ""))
            if asset is None:
                continue
            beat["path"] = "../../" + asset["path"]
            beat.setdefault("license", asset["license"])
            if asset.get("caption") and not beat.get("caption"):
                beat["caption"] = asset["caption"]


__all__ = [
    "ALLOWED_IMAGE_SUFFIXES",
    "IMAGE_ASSETS_DIR",
    "MAX_IMAGE_BYTES",
    "ImageIngestResult",
    "attach_image_assets",
    "ingest_image_assets",
    "validate_image_asset_refs",
]
