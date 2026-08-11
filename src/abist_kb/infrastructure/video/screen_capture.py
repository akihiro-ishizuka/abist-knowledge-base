"""アプリを起動して画面を撮影し、指定領域をマスクする。

**任意機能。** 失敗しても例外を投げず、`ShotResult(ok=False, code=...)` を返す。
動画生成は図解／プレースホルダで続行する（Phase 7 の方針）。

守っている性質:

- 起動コマンドは `CaptureProfile.command`（運用者が登録した配列）のみ。
  `shell=False` で配列のまま渡すので、文字列連結による注入が原理的に起きない
- 撮影プロセスは `manim_runner.kill_process_tree` で**木ごと**終了させる。
  子孫が残ると次回のポート衝突・ゾンビ化の原因になる
- マスク対象が1つでも見つからなければ**その shot を落とす**（未マスクのまま
  出すより落とす）。`MASK_TARGET_NOT_FOUND`
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from abist_kb.domain.capture_spec import CaptureProfile, Shot
from abist_kb.infrastructure.visualization.manim_runner import kill_process_tree, spawn_kwargs

#: ready 待ちのポーリング間隔（秒）。
READY_POLL_INTERVAL = 0.5
#: 1 shot あたりの撮影タイムアウト（秒）。
SHOT_TIMEOUT_SECONDS = 60.0
#: プロセス終了を待つ猶予（秒）。超えたら木ごと kill。
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ShotResult:
    id: str
    ok: bool
    path: Path | None = None
    sha256: str | None = None
    masked_regions: int = 0
    scene_id: str | None = None
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureRunResult:
    ok: bool
    shots: list[ShotResult] = field(default_factory=list)
    code: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: 起動した子プロセスの PID（後始末の検証に使う）。
    launched_pid: int | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_until_ready(
    url: str, timeout_sec: float, *, now: Callable[[], float] = time.monotonic
) -> bool:
    """HTTP が応答するまで待つ（起動確認）。"""
    deadline = now() + timeout_sec
    while now() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3.0):  # noqa: S310 - 登録済み URL のみ
                return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(READY_POLL_INTERVAL)
    return False


@contextlib.contextmanager
def launched(profile: CaptureProfile, cwd: Path):
    """プロファイルのコマンドでアプリを起動し、**必ず木ごと終了させる**。

    `shell=False` かつ配列渡し。`profile.command` は運用者が登録した配列なので、
    ここに外部入力が混じる経路は存在しない。
    """
    process = subprocess.Popen(  # noqa: S603 - 登録済みプロファイルの配列のみ
        list(profile.command),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        **spawn_kwargs(),
    )
    try:
        yield process
    finally:
        # **木 kill を先に撃つ。** Windows の `taskkill /F /T` は親が生きている
        # 間しか子孫を辿れないため、`terminate()` で親を先に落とすと孫が孤児として
        # 残る（実測で確認済み）。行儀のよい停止より「子孫を残さない」を優先する。
        if process.poll() is None:
            kill_process_tree(process)
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=TERMINATE_GRACE_SECONDS)


def _capture_web(profile: CaptureProfile, shot: Shot, target: Path) -> ShotResult:
    """Playwright で撮影し、`mask` のセレクタを黒塗りしてから保存する。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ShotResult(
            id=shot.id,
            ok=False,
            scene_id=shot.scene_id,
            code="CAPTURE_FAILED",
            message="playwright が入っていません（uv sync --group dev で導入します）",
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 900})
                page.goto(profile.url or "", timeout=SHOT_TIMEOUT_SECONDS * 1000)
                if shot.wait_ms:
                    page.wait_for_timeout(shot.wait_ms)

                # マスクは「撮る前に DOM を潰す」。撮ってから画像処理で消すより
                # 確実で、element_handle が無い＝マスクできない、が即座に分かる。
                masked = 0
                for selector in shot.mask:
                    elements = page.query_selector_all(selector)
                    if not elements:
                        browser.close()
                        return ShotResult(
                            id=shot.id,
                            ok=False,
                            scene_id=shot.scene_id,
                            code="MASK_TARGET_NOT_FOUND",
                            message=f'マスク対象 "{selector}" が見つからないため撮影を破棄しました',
                        )
                    for element in elements:
                        element.evaluate(
                            "el => { el.style.background = '#000'; el.style.color = '#000'; "
                            "el.textContent = '****'; }"
                        )
                        masked += 1

                target.parent.mkdir(parents=True, exist_ok=True)
                if shot.selector:
                    element = page.query_selector(shot.selector)
                    if element is None:
                        browser.close()
                        return ShotResult(
                            id=shot.id,
                            ok=False,
                            scene_id=shot.scene_id,
                            code="CAPTURE_FAILED",
                            message=f'セレクタ "{shot.selector}" が見つかりません',
                        )
                    element.screenshot(path=str(target))
                else:
                    page.screenshot(path=str(target), full_page=False)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - 撮影失敗で動画生成を止めない
        return ShotResult(
            id=shot.id,
            ok=False,
            scene_id=shot.scene_id,
            code="CAPTURE_FAILED",
            message=str(exc)[:300],
        )

    if not target.is_file():
        return ShotResult(
            id=shot.id,
            ok=False,
            scene_id=shot.scene_id,
            code="CAPTURE_FAILED",
            message="画像が保存されませんでした",
        )
    return ShotResult(
        id=shot.id,
        ok=True,
        path=target,
        sha256=_sha256_file(target),
        masked_regions=masked,
        scene_id=shot.scene_id,
    )


def run(profile: CaptureProfile, *, workspace: Path, out_dir: Path) -> CaptureRunResult:
    """アプリを起動して全 shot を撮影する。

    1 shot の失敗は他の shot を巻き込まない（`CAPTURE_FAILED` はその shot のみ）。
    起動そのものに失敗した場合は `APP_LAUNCH_FAILED` / `APP_NOT_READY` を返し、
    呼び出し側はキャプチャ全体を skip して動画生成を続ける。
    """
    if profile.launch_type != "web":
        return CaptureRunResult(
            ok=False,
            code="CAPTURE_FAILED",
            message=f"launch.type={profile.launch_type} の撮影は未対応です（web のみ）",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    try:
        with launched(profile, workspace) as process:
            if process.poll() is not None:
                return CaptureRunResult(
                    ok=False,
                    code="APP_LAUNCH_FAILED",
                    message=f"アプリが即座に終了しました (exit={process.returncode})",
                )
            if not wait_until_ready(profile.url or "", profile.ready_timeout_sec):
                return CaptureRunResult(
                    ok=False,
                    code="APP_NOT_READY",
                    message=f"{profile.ready_timeout_sec} 秒以内に応答しませんでした",
                    launched_pid=process.pid,
                )

            results: list[ShotResult] = []
            for shot in profile.shots:
                outcome = _capture_web(profile, shot, out_dir / f"{shot.id}.png")
                results.append(outcome)
                if not outcome.ok:
                    warnings.append(f"{shot.id}: {outcome.message}")
            return CaptureRunResult(
                ok=any(r.ok for r in results),
                shots=results,
                warnings=warnings,
                launched_pid=process.pid,
            )
    except OSError as exc:
        return CaptureRunResult(ok=False, code="APP_LAUNCH_FAILED", message=str(exc)[:300])


__all__ = [
    "READY_POLL_INTERVAL",
    "SHOT_TIMEOUT_SECONDS",
    "CaptureRunResult",
    "ShotResult",
    "launched",
    "run",
    "wait_until_ready",
]
