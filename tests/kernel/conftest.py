"""kernel fixture ローダ(`tests/fixtures/kernel/*.json`)。

M2 の全カーネルモジュール(frontmatter/chunker/line-range/...)のテストが共有する。
fixture は旧 Node 実装を実行して得たゴールデン値なので、ここでの読み込みは
一切の正規化・変換を行わない(base64 デコードのみ)。特に BOM を保持したまま
デコードすることが重要で、`utf-8-sig` を使うと fixture が検証したいBOMそのものが
デコード時に消えてしまう。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "kernel"


def load_kernel_fixture(name: str) -> dict:
    """`tests/fixtures/kernel/<name>.json` を読んで dict を返す。

    fixture が見つからない場合は `FileNotFoundError` で明示的に落とす。
    存在しないケースを黙ってスキップすると、カーネルテストが1件も実行されずに
    "green" に見えてしまう(合格したテストと見分けがつかない)ため、これは事故を
    防ぐための意図的な仕様である。
    """
    path = FIXTURES_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"kernel fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def b64d_bytes(s: str) -> bytes:
    """base64 文字列を生バイト列にデコードする。"""
    return base64.b64decode(s)


def b64d(s: str) -> str:
    """base64 文字列を UTF-8 文字列にデコードする。

    `bytes.decode("utf-8")` を使う(`"utf-8-sig"` は使わない)。fixture の
    `bom_b64` / `raw_b64` は BOM(`\\ufeff`)をバイト列として保持したまま
    格納されており、`utf-8-sig` はデコード時にそのBOMを黙って剥がしてしまう
    ため、BOM保持の検証そのものが壊れてしまう。
    """
    return b64d_bytes(s).decode("utf-8")
