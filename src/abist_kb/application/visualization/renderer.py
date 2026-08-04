"""可視化レンダリングのオーケストレーション(MCP 非依存)。

旧実装 `tools/lib/visualize-runner.js` の移植。流れ: SceneSpec 検証 →
出典検証(source_verifier)→ Python 解決 → 出力ディレクトリ作成 →
scene-spec.json / scene.py 書き出し → render_scene.py 実行(タイムアウト)→
出力検査 → manifest 書き出し。

同時実行の直列化(旧 `CONCURRENT_RENDER`)は呼び出し側(MCP ツール層)が
`render` リソースリースで行う(設計書 §10.2: 全プロセス横断で1件、
このモジュール自体は DB に触れないプロセス内オーケストレーションに留める)。

manifest.json は出力ディレクトリを作成した後なら失敗時も必ず書く(デバッグ可能性)。
失敗は `{"ok": False, "code": ..., ...}`。code は kb-visualize MCP がそのまま
エージェントに返す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.visualization.source_verifier import verify_sources
from abist_kb.domain.scene_spec import validate_scene_spec
from abist_kb.infrastructure.visualization.artifact_store import (
    OutputPathViolation,
    assert_inside_path,
    build_manifest,
    create_visualization_dir,
    normalize_slug,
    sha256_file,
    sha256_text,
)
from abist_kb.infrastructure.visualization.manim_runner import (
    default_timeout_seconds,
    parse_last_json_line,
    resolve_python,
    run_python_process,
    tail,
)
from abist_kb.infrastructure.visualization.manim_runner import (
    ffmpeg_version as default_ffmpeg_version,
)

#: 出力ディレクトリに保存する再現用の薄いファイル。spec の値は一切埋め込まない
#: (インジェクション不可能)。実体は scene-spec.json。
_SCENE_PY = """# 自動生成ファイル。編集しないこと。
# 再現描画: <python> tools/visualize/render_scene.py --spec scene-spec.json --outdir .
# (このファイルはリポジトリ標準配置 reports/visualizations/<id>/ を前提とする)
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "tools" / "visualize"))

from render_scene import build_scene_class

KbScene = build_scene_class(_HERE / "scene-spec.json")
"""


@dataclass(frozen=True, slots=True)
class RenderOutcome:
    ok: bool
    code: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    visualization_id: str | None = None
    output_dir: Path | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path | None = None
    duration_ms: float | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None


_UNSET = object()


def render_scene(
    spec: dict[str, Any],
    *,
    docs_dir: Path,
    reports_dir: Path,
    repo_root: Path,
    timeout_seconds: float | None = None,
    slug: str | None = None,
    now: datetime | None = None,
    random: Any = None,
    python_path: Any = _UNSET,
    run_process: Any = run_python_process,
    resolve_python_fn: Any = resolve_python,
    ffmpeg_version_fn: Any = default_ffmpeg_version,
) -> RenderOutcome:
    """SceneSpec を検証・出典検証し、Manim レンダリングを実行する。

    呼び出し側(`presentation.mcp.kb_visualize`)が `render` リソースリースの
    直列化(旧 CONCURRENT_RENDER 意味論)を担う。この関数自体は単発実行のみを
    扱う。
    """
    # 1. SceneSpec 検証(Python 起動前に fail fast)
    validated = validate_scene_spec(spec)
    if not validated.ok:
        return RenderOutcome(
            ok=False, code="INVALID_SCENE_SPEC", errors=[e.to_dict() for e in validated.errors]
        )
    assert validated.spec is not None

    # 2. 出典検証とビート剪定
    verified = verify_sources(validated.spec, docs_dir)
    if not verified.ok:
        return RenderOutcome(
            ok=False,
            code=verified.code,
            errors=[e.to_dict() for e in verified.errors],
            warnings=verified.warnings,
        )
    assert verified.spec is not None
    final_spec = verified.spec
    warnings = list(verified.warnings)

    # 3. Python 解決
    resolved_python = (
        python_path if python_path is not _UNSET else resolve_python_fn(root=repo_root)
    )
    if not resolved_python:
        return RenderOutcome(
            ok=False,
            code="PYTHON_NOT_FOUND",
            errors=[
                {
                    "path": "",
                    "code": "missing",
                    "message": (
                        "Python が見つかりません。"
                        "py ランチャーか .venv-visualize を用意してください"
                    ),
                }
            ],
            warnings=warnings,
        )

    # 4. 出力ディレクトリと入力ファイル
    created = create_visualization_dir(
        reports_dir, slug or normalize_slug(final_spec["title"]), now=now, random=random
    )
    visualization_id, out_dir = created.visualization_id, created.dir
    spec_json = json.dumps(final_spec, ensure_ascii=False, indent=2)
    spec_file = out_dir / "scene-spec.json"
    spec_file.write_text(spec_json, encoding="utf-8")
    (out_dir / "scene.py").write_text(_SCENE_PY, encoding="utf-8")

    # 5. レンダリング実行
    effective_timeout = (
        timeout_seconds if timeout_seconds is not None else default_timeout_seconds()
    )
    started_at = datetime.now(UTC)
    result = run_process(
        python_path=resolved_python,
        args=[
            str(repo_root / "tools" / "visualize" / "render_scene.py"),
            "--spec",
            str(spec_file),
            "--outdir",
            str(out_dir),
        ],
        timeout_seconds=effective_timeout,
        cwd=repo_root,
    )
    duration_ms = (datetime.now(UTC) - started_at).total_seconds() * 1000
    payload = parse_last_json_line(result.stdout)

    manifest_versions = {
        "python": (payload or {}).get("python"),
        "manim": (payload or {}).get("manim"),
        "ffmpeg": ffmpeg_version_fn(),
    }
    manifest_base: dict[str, Any] = {
        "spec": final_spec,
        "visualization_id": visualization_id,
        "created_at": datetime.now(UTC),
        "versions": manifest_versions,
        "spec_sha256": sha256_text(spec_json),
        "warnings": warnings,
        "render": {
            "duration_ms": duration_ms,
            "exit_code": result.exit_code,
            "stdout_tail": tail(result.stdout),
            "stderr_tail": tail(result.stderr),
        },
    }
    manifest_path = out_dir / "manifest.json"

    def fail_with(code: str, message: str) -> RenderOutcome:
        manifest_path.write_text(
            json.dumps(build_manifest(outputs=[], **manifest_base), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return RenderOutcome(
            ok=False,
            code=code,
            errors=[{"path": "", "code": "render", "message": message}],
            warnings=warnings,
            visualization_id=visualization_id,
            output_dir=out_dir,
            manifest_path=manifest_path,
            stdout_tail=tail(result.stdout),
            stderr_tail=tail(result.stderr),
        )

    if result.timed_out:
        return fail_with(
            "RENDER_TIMEOUT",
            f"レンダリングが {round(effective_timeout / 60)} 分を超えたため処理を終了しました",
        )
    if payload and payload.get("ok") is False and payload.get("code"):
        return fail_with(
            payload["code"],
            payload.get("error") or f"レンダリングに失敗しました（{payload['code']}）",
        )
    if result.exit_code != 0:
        return fail_with(
            "RENDER_FAILED", f"レンダリングに失敗しました（exit code {result.exit_code}）"
        )

    # 6. 出力検査(封じ込め + 存在確認)
    ext = "png" if final_spec["output_format"] == "png" else "mp4"
    output_file = out_dir / f"output.{ext}"
    try:
        assert_inside_path(out_dir, output_file)
    except OutputPathViolation as exc:
        manifest_path.write_text(
            json.dumps(build_manifest(outputs=[], **manifest_base), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return RenderOutcome(
            ok=False,
            code="OUTPUT_PATH_VIOLATION",
            errors=[{"path": "", "code": "invalid", "message": str(exc)}],
            warnings=warnings,
            visualization_id=visualization_id,
            output_dir=out_dir,
            manifest_path=manifest_path,
        )
    if not output_file.exists():
        return fail_with("OUTPUT_NOT_FOUND", f"出力ファイルが見つかりません: output.{ext}")

    # 7. manifest 書き出し
    outputs = [
        {
            "path": f"output.{ext}",
            "sha256": sha256_file(output_file),
            "size_bytes": output_file.stat().st_size,
        }
    ]
    manifest_path.write_text(
        json.dumps(build_manifest(outputs=outputs, **manifest_base), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return RenderOutcome(
        ok=True,
        visualization_id=visualization_id,
        output_dir=out_dir,
        outputs=[{"type": "image" if ext == "png" else "video", "path": str(output_file)}],
        manifest_path=manifest_path,
        warnings=warnings,
        duration_ms=duration_ms,
    )


__all__ = ["RenderOutcome", "render_scene"]
