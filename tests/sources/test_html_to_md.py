"""`infrastructure.sources.html_to_md`: `tests/fixtures/html/turndown-goldens.json` との突合せ。

`tests/fixtures/PROVENANCE.md` の通り、このフィクスチャは **advisory** 契約
(`comparison_policy: "advisory"`)である。旧実装は Node の `turndown`(GFM
プラグイン無し)、この実装は Python の `markdownify` であり、ライブラリが違う
以上ビット互換までは要求されない。

このテストは15ケース全件についてビット一致まで確認しているが、それは要求を
超える結果である(`.superpowers/sdd/M3-collection-jobs/task-4-5-report.md` に
1件ずつの判断根拠を記録した)。将来 `markdownify` を更新するなどして差分が
出た場合は、advisory契約に従い「許容できる逸脱かどうか」を見て判断すればよく、
このテストの失敗が即座にブロッカーになるわけではない。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from abist_kb.infrastructure.sources.html_to_md import html_to_markdown

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "html" / "turndown-goldens.json"


def _load_cases() -> list[dict]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["comparison_policy"].startswith("advisory")
    return payload["cases"]


_CASES = _load_cases()


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_html_to_markdown_matches_or_is_an_accepted_advisory_diff(case: dict) -> None:
    html = base64.b64decode(case["input_html_b64"]).decode("utf-8")
    expected = base64.b64decode(case["output_markdown_b64"]).decode("utf-8")

    actual = html_to_markdown(html, case["base_url"])

    # 15ケース全件がビット一致する(advisoryが要求する以上の結果)。将来ここが
    # 崩れたら、`task-4-5-report.md` の判断根拠に沿って許容可否を見直すこと。
    assert actual == expected, (
        f"golden diff for {case['id']!r} — see task-4-5-report.md for judgement policy"
    )
