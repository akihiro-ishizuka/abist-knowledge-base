"""QA（`qa-report.json`）—— 機械検査と、人間が判断すべき項目の切り分け。

原則:

- **QA FAIL でも `output.mp4` は残す。** 落とすのは承認と公開審査提出であって、
  成果物ではない。何が悪いのかを見るために現物が要る
- **レポートにファイル全文を載せない。** 秘密スキャンのヒットは位置と種別だけを
  書き、値そのものは書かない（レポートが漏洩経路になる）
- 人間必須の項目は `human_required` に分けて、機械が勝手に pass にしない
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.video.project_store import check_input_usage
from abist_kb.application.video.video_metadata import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    MIN_CHAPTERS_FOR_YOUTUBE,
)
from abist_kb.infrastructure.video.ffmpeg_runner import (
    FfmpegUnavailableError,
    probe,
)

QA_REPORT_FILE = "qa-report.json"

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

#: 秘密情報の検出パターン。**値は記録しない**（種別と位置だけ）。
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "google_oauth_token": re.compile(r"\bya29\.[A-Za-z0-9_-]{10,}"),
    "google_api_key": re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
    "esa_token": re.compile(r"\bESA_[A-Z_]*TOKEN\b"),
    "generic_bearer": re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "windows_path": re.compile(r"\b[A-Za-z]:\\\\?(?:Users|Temp)\\\\?"),
}

#: 人間の判断が要る項目（機械では pass にしない）。
HUMAN_REQUIRED = ("preview_approval", "public_candidate_review")

#: 音声が無くても warn どまりにする（無音経路は正当な MVP の完成形）。
_AUDIO_OPTIONAL = True


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    status: str
    detail: str
    severity: str = "auto"

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "severity": self.severity,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class QaReport:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    human_required: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "human_required": list(self.human_required),
        }

    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == STATUS_FAIL]


def scan_secrets(text: str) -> list[dict[str, Any]]:
    """秘密情報らしき並びを探す。**ヒットした値は返さない。**"""
    findings: list[dict[str, Any]] = []
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append({"kind": name, "offset": match.start(), "length": len(match.group(0))})
    return findings


def _media_checks(output: Path | None, spec: dict[str, Any]) -> list[Check]:
    fmt = spec.get("format") or {}
    want_w = int(fmt.get("width") or 1920)
    want_h = int(fmt.get("height") or 1080)
    want_fps = int(fmt.get("fps") or 30)
    target = fmt.get("target_duration_sec") or {}

    if output is None or not output.is_file():
        return [Check("playable", STATUS_FAIL, "output.mp4 がありません")]
    try:
        info = probe(output)
    except FfmpegUnavailableError:
        return [Check("playable", STATUS_WARN, "ffprobe が無いため再生確認をスキップしました")]

    checks: list[Check] = []
    if info.duration_sec is None or info.duration_sec <= 0:
        checks.append(Check("playable", STATUS_FAIL, "尺を読み取れません（壊れた MP4）"))
        return checks
    checks.append(Check("playable", STATUS_PASS, f"再生可能（{info.duration_sec:.1f} 秒）"))

    if info.width == want_w and info.height == want_h:
        checks.append(Check("resolution_fps", STATUS_PASS, f"{info.width}x{info.height}"))
    else:
        checks.append(
            Check(
                "resolution_fps",
                STATUS_FAIL,
                f"解像度が契約と違います（実測 {info.width}x{info.height} / "
                f"契約 {want_w}x{want_h}）",
            )
        )
    if info.fps is not None and abs(info.fps - want_fps) < 0.5:
        checks.append(Check("fps", STATUS_PASS, f"{info.fps:.0f}fps"))
    else:
        checks.append(Check("fps", STATUS_FAIL, f"fps が契約と違います（実測 {info.fps}）"))

    want_aspect = fmt.get("aspect_ratio") or "16:9"
    actual_ratio = (info.width or 1) / max(1, info.height or 1)
    expected_ratio = 9 / 16 if want_aspect == "9:16" else 16 / 9
    checks.append(
        Check("aspect", STATUS_PASS, want_aspect)
        if abs(actual_ratio - expected_ratio) < 0.01
        else Check("aspect", STATUS_FAIL, f"アスペクト比が {want_aspect} と一致しません")
    )

    lo, hi = target.get("min"), target.get("max")
    if isinstance(lo, int | float) and isinstance(hi, int | float):
        if lo <= info.duration_sec <= hi:
            checks.append(
                Check("duration_range", STATUS_PASS, f"{info.duration_sec:.1f} 秒（{lo}〜{hi}）")
            )
        else:
            checks.append(
                Check(
                    "duration_range",
                    STATUS_FAIL,
                    f"尺が目標範囲外です（実測 {info.duration_sec:.1f} 秒 / 目標 {lo}〜{hi} 秒）",
                )
            )

    if info.has_audio:
        checks.append(Check("has_audio", STATUS_PASS, "音声ストリームあり"))
    else:
        checks.append(
            Check(
                "has_audio",
                STATUS_WARN if _AUDIO_OPTIONAL else STATUS_FAIL,
                "音声がありません（無音モードなら想定どおり）",
            )
        )
    return checks


def _sound_checks(project_dir: Path) -> list[Check]:
    """効果音の密度・重なり・出所（`sound-cues.json` を読む）。"""
    path = project_dir / "audio" / "sound-cues.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [Check("sfx_density", STATUS_WARN, "効果音の記録がありません")]

    cues = payload.get("cues") or []
    checks: list[Check] = []

    from abist_kb.application.video.sound_events import MAX_PER_SCENE, MIN_INTERVAL_SEC

    per_scene: dict[str, int] = {}
    for cue in cues:
        per_scene[str(cue.get("scene_id"))] = per_scene.get(str(cue.get("scene_id")), 0) + 1
    over = [s for s, n in per_scene.items() if n > max(MAX_PER_SCENE.values())]
    checks.append(
        Check("sfx_density", STATUS_FAIL, f"1シーン上限を超えています: {sorted(over)}")
        if over
        else Check("sfx_density", STATUS_PASS, f"{len(cues)} 箇所（上限内）")
    )

    times = sorted(float(c.get("t_sec", 0.0)) for c in cues)
    too_close = [(a, b) for a, b in zip(times, times[1:], strict=False) if b - a < MIN_INTERVAL_SEC]
    checks.append(
        Check("sfx_overlap", STATUS_FAIL, f"最短間隔を割る組が {len(too_close)} 件あります")
        if too_close
        else Check("sfx_overlap", STATUS_PASS, "最短間隔を満たしています")
    )

    missing = [c for c in cues if not c.get("sha256")]
    checks.append(
        Check("sfx_source_integrity", STATUS_FAIL, f"SHA-256 の無い音源が {len(missing)} 件")
        if missing
        else Check("sfx_source_integrity", STATUS_PASS, "全ての音源に SHA-256 の記録あり")
    )
    return checks


def _citation_checks(project_dir: Path, spec: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    try:
        citations = json.loads((project_dir / "citations.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [Check("citations_complete", STATUS_FAIL, "citations.json がありません")]

    if citations.get("sources"):
        checks.append(
            Check("citations_complete", STATUS_PASS, f"{len(citations['sources'])} 件の出典")
        )
    else:
        checks.append(Check("citations_complete", STATUS_FAIL, "出典が1件もありません"))

    # 事実を述べるシーンに出典があるか（装飾のみのカードは対象外）
    factual_without_source = []
    for scene in spec.get("scenes") or []:
        scene_spec = scene.get("scene_spec") or {}
        beats = scene_spec.get("beats") or []
        has_source = any(b.get("source_refs") for b in beats if isinstance(b, dict))
        has_factual = any(
            isinstance(b, dict) and b.get("decorative") is not True and b.get("type") != "image"
            for b in beats
        )
        if has_factual and not has_source:
            factual_without_source.append(scene.get("id"))
    checks.append(
        Check(
            "scene_citations",
            STATUS_FAIL,
            f"出典の無い事実シーンがあります: {factual_without_source}",
        )
        if factual_without_source
        else Check("scene_citations", STATUS_PASS, "事実シーンはすべて出典つき")
    )

    attributions = citations.get("sound_attributions") or []
    if attributions and any(not a.get("license") for a in attributions):
        checks.append(Check("sfx_attribution", STATUS_FAIL, "ライセンス記載の無い効果音があります"))
    else:
        checks.append(Check("sfx_attribution", STATUS_PASS, f"効果音の帰属 {len(attributions)} 件"))
    return checks


def _subtitle_checks(project_dir: Path, duration_sec: float | None) -> list[Check]:
    srt = project_dir / "subtitles" / "narration.srt"
    if not srt.is_file():
        return [Check("subtitle_bounds", STATUS_WARN, "字幕がありません")]
    text = srt.read_text(encoding="utf-8")
    stamp_re = r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})"
    stamps = re.findall(stamp_re, text)
    if not stamps:
        return [Check("subtitle_bounds", STATUS_WARN, "字幕キューがありません")]
    ends = [
        int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for _, _, _, _, h, m, s, ms in stamps
    ]
    if duration_sec and max(ends) > duration_sec + 1.0:
        return [
            Check(
                "subtitle_bounds",
                STATUS_FAIL,
                f"字幕が動画の尺を超えています（{max(ends):.1f} 秒 > {duration_sec:.1f} 秒）",
            )
        ]
    return [Check("subtitle_bounds", STATUS_PASS, f"{len(stamps)} 枚が尺内に収まっています")]


def _metadata_checks(project_dir: Path) -> list[Check]:
    from abist_kb.application.video.video_metadata import load_metadata

    metadata = load_metadata(project_dir)
    if metadata is None:
        return [Check("metadata_limits", STATUS_WARN, "video-metadata.json がありません")]
    problems: list[str] = []
    if len(metadata.get("title") or "") > MAX_TITLE_CHARS:
        problems.append("タイトルが上限超過")
    if len(metadata.get("description") or "") > MAX_DESCRIPTION_CHARS:
        problems.append("説明文が上限超過")
    chapters = metadata.get("chapters") or []
    starts = [c.get("start_sec", 0) for c in chapters]
    if starts != sorted(starts):
        problems.append("チャプターが単調増加でない")
    checks = [
        Check("metadata_limits", STATUS_FAIL, " / ".join(problems))
        if problems
        else Check("metadata_limits", STATUS_PASS, "手動登録用の文字数制限を満たしています")
    ]
    checks.append(
        Check("chapters", STATUS_PASS, f"{len(chapters)} 章")
        if len(chapters) >= MIN_CHAPTERS_FOR_YOUTUBE
        else Check("chapters", STATUS_WARN, f"チャプターが {len(chapters)} 件（3 件以上を推奨）")
    )
    return checks


def _secret_checks(project_dir: Path) -> list[Check]:
    """成果物のテキストに秘密情報が混ざっていないか。

    走査対象は**人の目に触れる成果物**（メタデータ・字幕・出典）。
    project-spec.json のような内部ファイルは対象にしない（ローカルパスを
    正当に含むため、ここで見ると常に FAIL になる）。
    """
    targets = [
        project_dir / "video-metadata.json",
        project_dir / "citations.json",
        project_dir / "subtitles" / "narration.srt",
        project_dir / "subtitles" / "narration.vtt",
    ]
    findings: list[dict[str, Any]] = []
    for path in targets:
        if not path.is_file():
            continue
        for hit in scan_secrets(path.read_text(encoding="utf-8", errors="replace")):
            findings.append({**hit, "file": path.name})
    if findings:
        kinds = sorted({f["kind"] for f in findings})
        return [
            Check(
                "secret_scan",
                STATUS_FAIL,
                f"秘密情報らしき記載が {len(findings)} 件あります（種別: {', '.join(kinds)}）"
                "。値はレポートに記録していません",
            )
        ]
    return [Check("secret_scan", STATUS_PASS, "秘密情報らしき記載はありません")]


def _distribution_checks(spec: dict[str, Any]) -> list[Check]:
    dist = spec.get("distribution") or {}
    sensitive = [
        s
        for s in (spec.get("sources") or [])
        if isinstance(s, dict)
        and s.get("sensitivity") in ("confidential", "pii", "internal_restricted")
    ]
    if dist.get("public_candidate") and (sensitive or dist.get("classification") == "confidential"):
        return [
            Check(
                "distribution_consistency",
                STATUS_FAIL,
                "機密ソースを含むのに public_candidate=true のままです",
            )
        ]
    return [
        Check(
            "distribution_consistency",
            STATUS_PASS,
            f"classification={dist.get('classification')} / "
            f"public_candidate={bool(dist.get('public_candidate'))}",
        )
    ]


def _input_usage_checks(spec: dict[str, Any], used_paths: set[str] | None) -> list[Check]:
    if used_paths is None:
        return []
    errors = check_input_usage(spec, used_paths)
    if errors:
        return [Check("input_usage", STATUS_FAIL, " / ".join(e.message for e in errors))]
    return [Check("input_usage", STATUS_PASS, "明示した主入力はすべて本編で使われています")]


def run_qa(
    project_dir: Path,
    *,
    spec: dict[str, Any],
    output: Path | None = None,
    used_paths: set[str] | None = None,
) -> QaReport:
    """自動検査を全部走らせて `QaReport` を返す（書き出しは `write_report`）。"""
    resolved_output = output or (project_dir / "output-with-audio.mp4")
    if not resolved_output.is_file():
        resolved_output = project_dir / "output.mp4"

    checks: list[Check] = []
    checks += _media_checks(resolved_output if resolved_output.is_file() else None, spec)
    duration = next(
        (
            float(c.detail.split("（")[1].split(" ")[0])
            for c in checks
            if c.id == "playable" and c.status == STATUS_PASS and "（" in c.detail
        ),
        None,
    )
    checks += _subtitle_checks(project_dir, duration)
    checks += _sound_checks(project_dir)
    checks += _citation_checks(project_dir, spec)
    checks += _metadata_checks(project_dir)
    checks += _secret_checks(project_dir)
    checks += _distribution_checks(spec)
    checks += _input_usage_checks(spec, used_paths)

    ok = not any(c.status == STATUS_FAIL for c in checks)
    return QaReport(ok=ok, checks=checks, human_required=list(HUMAN_REQUIRED))


def write_report(report: QaReport, project_dir: Path) -> Path:
    """`qa-report.json` を書く。**FAIL でも成果物は消さない。**"""
    path = project_dir / QA_REPORT_FILE
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(project_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((project_dir / QA_REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "HUMAN_REQUIRED",
    "QA_REPORT_FILE",
    "SECRET_PATTERNS",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_WARN",
    "Check",
    "QaReport",
    "load_report",
    "run_qa",
    "scan_secrets",
    "write_report",
]
