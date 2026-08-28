"""シーン種別の見本（ギャラリー）を決定的に生成する。

`list_scene_kinds` が返すのは文字の説明だけで、書き手は17種の見た目を**想像して**
選んでいた。その結果、手書きの高品質台本でさえ 14シーン中 10 が key_points に偏った。
**見えないものは選ばれない。**

音源生成器（`generate-video-sfx.py` / `generate-video-bgm.py`）と同じ流儀:

- 純粋に決定論的（乱数なし）。同じスクリプトから常に同じ PNG が出る
- 生成物と一緒に `manifest.json`（sha256）を書き、再現できることを検査可能にする
- **生成器をコミットする**。PNG をどう作ったのか復元できない状態を作らない

例文は**架空のダミーではなく**、その種別がどんな内容に向くかが伝わるものにする。
「Lorem ipsum」を並べた見本を見ても、どの種別を選ぶべきかは分からない。

動画（mp4）ではなく PNG にするのは、17本の動画を毎回描くとコミット容量と生成時間が
見合わないため。動きの差は文章で説明できる（timeline は上から順に現れる、flow は
エッジに光が流れる）が、**レイアウトの差は見ないと分からない**。

実行:
    <python> scripts/generate-scene-gallery.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from abist_kb.domain.scene_spec import SCENE_KINDS  # noqa: E402

OUTPUT_DIR = ROOT / "assets" / "scene-gallery"
#: 見本の中で参照する架空の出典（実在の文書に依存させない）。
SOURCE_PATH = "例/シーン種別の見本.md"
SOURCE_LINES = (1, 20)


def _src(*ids: str) -> list[str]:
    return list(ids) or ["s1"]


#: 種別ごとの beats。**その種別が向く内容**を選ぶ（種別選びの手がかりになるように）。
EXAMPLES: dict[str, list[dict]] = {
    # --- 図解 ---
    "explain": [
        {"type": "statement", "text": "抽出から出力までを1つのアプリで完結させる", "source_refs": _src()},
        {"type": "metric", "label": "対象パターン", "value": "50", "unit": "件", "source_refs": _src()},
        {"type": "transition", "from": "手作業", "to": "自動化"},
    ],
    "flow": [
        {"type": "flow_step", "label": "図面を取り込む", "source_refs": _src()},
        {"type": "flow_step", "label": "寸法を判定する", "source_refs": _src()},
        {"type": "decision", "label": "基準内か", "source_refs": _src()},
        {"type": "flow_step", "label": "帳票を出力する", "source_refs": _src()},
        {"type": "transition", "from": "図面を取り込む", "to": "寸法を判定する"},
        {"type": "transition", "from": "寸法を判定する", "to": "基準内か"},
        # ラベル付きの矢印は「その条件でそう進む」という事実の主張なので出典が要る。
        {
            "type": "transition",
            "from": "基準内か",
            "to": "帳票を出力する",
            "label": "はい",
            "source_refs": _src(),
        },
    ],
    "timeline": [
        {"type": "timeline_point", "at": "5/25", "label": "現場課題を確認", "source_refs": _src()},
        {"type": "timeline_point", "at": "6/9", "label": "新技術を調査", "source_refs": _src()},
        {"type": "timeline_point", "at": "7/22", "label": "顧客現場で前進", "source_refs": _src()},
        {"type": "timeline_point", "at": "7/28", "label": "展開と判断", "source_refs": _src()},
    ],
    "comparison": [
        {"type": "comparison_item", "side": "現行", "aspect": "作業時間", "text": "1件あたり40分", "source_refs": _src()},
        {"type": "comparison_item", "side": "現行", "aspect": "判定", "text": "人が目視で確認", "source_refs": _src()},
        {"type": "comparison_item", "side": "次期", "aspect": "作業時間", "text": "1件あたり8分", "source_refs": _src()},
        {"type": "comparison_item", "side": "次期", "aspect": "判定", "text": "AIが候補を提示", "source_refs": _src()},
    ],
    "domain": [
        {"type": "domain_entity", "name": "設計者", "group": "利用者", "source_refs": _src()},
        {"type": "domain_entity", "name": "検図支援", "group": "社内基盤", "source_refs": _src()},
        {"type": "domain_entity", "name": "図面DB", "group": "社内基盤", "source_refs": _src()},
        # 要素間の関係も事実の主張なので出典が要る。
        {"type": "domain_relation", "from": "設計者", "to": "検図支援", "label": "依頼", "source_refs": _src()},
        {"type": "domain_relation", "from": "検図支援", "to": "図面DB", "label": "参照", "source_refs": _src()},
    ],
    "chart": [
        {
            "type": "chart_series",
            "label": "案件A",
            "values": [{"name": "計画", "value": 40}, {"name": "実績", "value": 36}],
            "unit": "h",
            "source_refs": _src(),
        },
        {
            "type": "chart_series",
            "label": "案件B",
            "values": [{"name": "計画", "value": 32}, {"name": "実績", "value": 35}],
            "unit": "h",
            "source_refs": _src(),
        },
    ],
    # --- カード ---
    "title": [
        {"type": "statement", "text": "2026年5〜7月の活動", "decorative": True},
        {"type": "statement", "text": "12の主要活動を5つの領域で整理", "decorative": True},
    ],
    "chapter": [{"type": "statement", "text": "顧客業務の自動化を進めた3案件", "decorative": True}],
    "key_points": [
        {"type": "statement", "text": "蛇腹設計：形状候補の提案と図面出力を支援", "source_refs": _src()},
        {"type": "statement", "text": "評価業務：報告・権限・組織管理を仕組み化", "source_refs": _src()},
        {"type": "statement", "text": "曲げ治具：定型作業を自動化", "source_refs": _src()},
    ],
    "quote": [
        {
            "type": "quote",
            "text": "編集画面はビューオンリーとし、履歴は50件まで保持する",
            "attribution": "チーム内定例",
            "source_refs": _src(),
        }
    ],
    "summary": [
        {"type": "statement", "text": "個別の自動化から、改善を回す仕組みへ", "source_refs": _src()},
        {"type": "statement", "text": "知識・ツール・監査を組織で共有", "source_refs": _src()},
    ],
    "cta": [{"type": "statement", "text": "詳細は社内ポータルの手順書を参照", "decorative": True}],
    "ending": [{"type": "statement", "text": "社内限定。取り扱いに注意してください", "decorative": True}],
    "code": [
        {
            "type": "code",
            "language": "python",
            "text": "def check(drawing):\n    if drawing.tolerance > LIMIT:\n        return 'review'\n    return 'ok'",
            "source_refs": _src(),
        }
    ],
    "formula": [
        {
            "type": "formula",
            "text": "許容差 = 基準寸法 x 係数 + 補正",
            "caption": "寸法判定に使う式",
            "source_refs": _src(),
        }
    ],
    "image": [
        {"type": "image", "path": "missing.png", "caption": "アプリ画面（未取得時の見え方）", "decorative": True}
    ],
    "thumbnail": [{"type": "statement", "text": "一覧で内容が分かる看板", "decorative": True}],
}


def build_spec(kind: str) -> dict:
    """1種別ぶんの見本 SceneSpec（PNG 出力）。"""
    info = next(entry for entry in SCENE_KINDS if entry["kind"] == kind)
    spec: dict = {
        "schema_version": "1.0",
        "scene_kind": kind,
        "output_format": "png",
        "template": info["template"],
        "title": f"{kind}: {info['description'].split('。')[0]}"[:80],
        "quality": "standard",
        "frame": {"aspect_ratio": "16:9"},
        "sources": [
            {
                "id": "s1",
                "path": SOURCE_PATH,
                "start_line": SOURCE_LINES[0],
                "end_line": SOURCE_LINES[1],
                "content_hash": "0" * 64,
            }
        ],
        "beats": EXAMPLES[kind],
    }
    if kind == "chapter":
        spec["chapter_index"] = 1
        spec["chapter_total"] = 3
    if kind == "chart":
        spec["chart"] = {"variant": "grouped_bar", "value_label": "単位: 時間"}
    return spec


def render(kind: str, target: Path) -> None:
    """1枚描く。出典検証を通さずテンプレートを直接呼ぶ。

    見本の出典は実在しない架空のパスなので `render_scene`（出典を実ファイルと
    照合する）は使えない。ここが検証したいのは**レイアウトの見た目**だけ。
    """
    from manim import tempconfig

    sys.path.insert(0, str(ROOT / "tools" / "visualize"))
    from importlib import import_module

    from templates import layout, theme

    spec = build_spec(kind)
    module = import_module(f"templates.{spec['template']}")
    _anim, static_cls = module.make_scene_classes(spec)

    pixel_width, pixel_height = layout.pixel_size("16:9", "standard")
    frame_width, frame_height = layout.frame_size("16:9")
    with tempfile.TemporaryDirectory() as work:
        media = Path(work) / "media"
        with tempconfig(
            {
                "media_dir": str(media),
                "output_file": "output",
                "disable_caching": True,
                "progress_bar": "none",
                "verbosity": "ERROR",
                "save_last_frame": True,
                "format": "png",
                "pixel_width": pixel_width,
                "pixel_height": pixel_height,
                "frame_width": frame_width,
                "frame_height": frame_height,
                "background_color": theme.get_theme(None).background,
            }
        ):
            static_cls().render()
        produced = next(media.rglob("output*.png"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(target))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    previews = []
    for info in SCENE_KINDS:
        kind = info["kind"]
        target = OUTPUT_DIR / f"{kind}.png"
        render(kind, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        previews.append({"kind": kind, "path": f"{kind}.png", "sha256": digest})
        print(f"{kind}: {target.stat().st_size:>8} bytes  {digest[:12]}")
    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps(
            {
                "note": "scripts/generate-scene-gallery.py が生成する。手で編集しない。",
                "previews": previews,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
