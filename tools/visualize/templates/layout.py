"""テンプレート共通のレイアウト計算(manim 非依存の純関数のみ)。

**このモジュールは manim を import してはならない。**

`tools/visualize/templates/*.py` は manim を import するため、主 venv(Python 3.12)
で走る pytest からは import できず、レイアウトのロジックが構造的にテスト不能だった。
グラフ計算・文字列処理をここへ集約することで、`tests/visualize/test_template_layout.py`
が主 venv から直接検証できる(conftest が `tools/visualize` を sys.path へ入れる)。

manim を使う描画そのものは呼び出し側(base.py / data_flow_v1.py など)の責務。
"""

from __future__ import annotations

import unicodedata

#: 行頭に置きたくない文字(禁則)。ぶら下げて前行の末尾に送る。
_NO_LINE_START = "。、，．・）」』】〉》!?！？：；ー々ぁぃぅぇぉっゃゅょゎ…"
#: 行末に置きたくない文字(禁則)。次行の先頭へ送る。
_NO_LINE_END = "（「『【〈《"

#: `display_width` が全角(2桁)として数える east_asian_width の区分。
_WIDE = frozenset({"W", "F", "A"})


def display_width(text: str) -> int:
    """半角を1・全角を2として数えた表示幅を返す。

    Manim の `Text` は等幅ではないので厳密な値にはならないが、折り返し桁数の
    見積もりには十分。`east_asian_width` の Ambiguous(A) は日本語環境で全角に
    倒れるため2桁として扱う。
    """
    return sum(2 if unicodedata.east_asian_width(c) in _WIDE else 1 for c in text)


def wrap_cjk(text: str, max_cols: int) -> list[str]:
    """半角換算 `max_cols` 桁で折り返した行のリストを返す。

    `textwrap` は空白でしか折らないため日本語では機能せず、Manim の
    `Text(width=...)` は折り返しではなく `scale_to_fit_width`(縮小)なので、
    CJK 対応の折り返しは自前で持つしかない。

    - 既存の改行は段落境界として保持する
    - ASCII の連続語は空白位置を優先して切る(英単語を途中で割らない)
    - 行頭・行末の禁則を1文字だけ調整する(追い出し。ぶら下げないので
      戻り値の各行は必ず `display_width(line) <= max_cols` を満たす)
    """
    if max_cols < 2:
        max_cols = 2
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        lines.extend(_wrap_paragraph(paragraph, max_cols))
    return lines


def _wrap_paragraph(paragraph: str, max_cols: int) -> list[str]:
    lines: list[str] = []
    current = ""
    width = 0
    for char in paragraph:
        char_width = display_width(char)
        if width + char_width <= max_cols:
            current += char
            width += char_width
            continue
        # 行が溢れた。まず ASCII 語の途中で切らないよう後退を試みる。
        head, tail = _break_ascii_word(current, char)
        if tail:
            lines.append(head)
            current = tail + char
            width = display_width(current)
            continue
        # 禁則(追い出し): 次行の先頭に来てほしくない文字は、直前の1文字ごと次行へ送る。
        # ぶら下げ(前行に足す)ではなく追い出しを選ぶのは、`display_width(line) <= max_cols`
        # の不変条件を保つため。この幅は箱幅の逆算にも使うので、超過を許すと壊れる。
        if char in _NO_LINE_START and len(current) > 1:
            lines.append(current[:-1])
            current = current[-1] + char
            width = display_width(current)
            continue
        # 禁則: 前行の末尾が来てほしくない文字なら、その1文字を次行へ送る。
        if current and current[-1] in _NO_LINE_END:
            lines.append(current[:-1])
            current = current[-1] + char
            width = display_width(current)
            continue
        lines.append(current)
        current = char
        width = char_width
    if current:
        lines.append(current)
    return lines or [""]


def _break_ascii_word(current: str, next_char: str) -> tuple[str, str]:
    """ASCII 語の途中で折り返そうとしている場合に、語の直前で切り直す。

    戻り値 `(前行, 次行へ送る断片)`。切り直す必要がなければ `("", "")`。
    """
    if not current or not _is_ascii_word_char(next_char):
        return "", ""
    if not _is_ascii_word_char(current[-1]):
        return "", ""
    index = len(current)
    while index > 0 and _is_ascii_word_char(current[index - 1]):
        index -= 1
    if index == 0:
        # 行全体が1つの長い ASCII 語。切り直すと無限後退になるのでそのまま折る。
        return "", ""
    return current[:index].rstrip(), current[index:]


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "_-.")


def find_duplicate_labels(beats: list[dict]) -> list[tuple[int, str]]:
    """ノードを名乗る beat のラベル重複を `(index, label)` で返す(2件目以降)。

    `data_flow_v1` は `boxes[label] = box` でノードを引くため、ラベルが重複すると
    後勝ちで上書きされ、前のノードには矢印が繋がらない。検証層とテンプレート層の
    双方から使う。
    """
    seen: dict[str, int] = {}
    duplicates: list[tuple[int, str]] = []
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        if beat.get("type") not in _NODE_BEAT_TYPES:
            continue
        label = beat.get("label")
        if not isinstance(label, str):
            continue
        if label in seen:
            duplicates.append((index, label))
        else:
            seen[label] = index
    return duplicates


#: ラベルでノードとして参照される beat type(transition / domain_relation の接続先)。
_NODE_BEAT_TYPES = frozenset({"flow_step", "decision"})

#: `classify_edge` が返す分類。
EDGE_ADJACENT = "adjacent"
EDGE_SKIP_FORWARD = "skip_forward"
EDGE_BACK = "back"
EDGE_WRAP_DOWN = "wrap_down"
EDGE_CROSS_BAND_BACK = "cross_band_back"


def classify_edge(src: tuple[int, int], dst: tuple[int, int]) -> str:
    """ノード位置 `(band, col)` の関係から矢印の描き方を決める。

    従来は「中心 y が一致すれば常に 右辺 -> 左辺 の直線」だったため、同じ行の
    後戻り(4番目 -> 2番目)が間のノードを貫通し、行をまたぐ矢印は右端から左端へ
    斜めに全幅を横切っていた。位置関係で描き分けることでこれを解消する。
    """
    src_band, src_col = src
    dst_band, dst_col = dst
    if src_band == dst_band:
        if dst_col == src_col + 1:
            return EDGE_ADJACENT
        if dst_col > src_col:
            return EDGE_SKIP_FORWARD
        return EDGE_BACK
    if dst_band == src_band + 1:
        return EDGE_WRAP_DOWN
    return EDGE_CROSS_BAND_BACK


#: 1つの帯(band)に並べる rank の数。これを超えたら次の帯へ折り返す。
MAX_RANKS_PER_BAND = 4


def find_back_edges(labels: list[str], edges: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """DFS で後退辺(サイクルを閉じる辺)を検出して返す。

    後退辺を除かないと最長路レイヤリングが停止しない。再帰ではなく明示スタックで
    走査する(ノード数は MAX_BEATS 以下だが、再帰は深さ制限に依存するため)。
    """
    adjacency: dict[str, list[str]] = {label: [] for label in labels}
    for src, dst in edges:
        if src in adjacency and dst in adjacency:
            adjacency[src].append(dst)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(labels, WHITE)
    back: set[tuple[str, str]] = set()

    for root in labels:
        if color[root] != WHITE:
            continue
        # (node, 未処理の隣接ノードのイテレータ) を積む
        color[root] = GRAY
        stack: list[tuple[str, list[str], int]] = [(root, adjacency[root], 0)]
        while stack:
            node, neighbours, index = stack[-1]
            if index >= len(neighbours):
                color[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, neighbours, index + 1)
            nxt = neighbours[index]
            if color[nxt] == GRAY:
                # 探索中のノードへ戻る辺 = 後退辺(自己ループも含む)
                back.add((node, nxt))
            elif color[nxt] == WHITE:
                color[nxt] = GRAY
                stack.append((nxt, adjacency[nxt], 0))
    return back


def assign_ranks(labels: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """最長路レイヤリング。ノードごとの rank(=列番号)を返す。

    - 後退辺は rank 計算から除外する(除かないと停止しない)
    - rank(v) = 入次数0なら0、そうでなければ max(rank(u) for u->v) + 1
    - 孤立ノード(どの辺にも現れない)は末尾 rank へ置く

    **直線フロー A->B->C->D->E では rank が 0,1,2,3,4 と1個ずつ増え、各 rank に
    ノード1個になる。** これが「直線フローの配置は変わらない」という後方互換の根拠。
    """
    if not labels:
        return {}
    back = find_back_edges(labels, edges)
    forward = [(s, d) for s, d in edges if (s, d) not in back and s in labels and d in labels]

    incoming: dict[str, list[str]] = {label: [] for label in labels}
    outgoing: dict[str, list[str]] = {label: [] for label in labels}
    for src, dst in forward:
        incoming[dst].append(src)
        outgoing[src].append(dst)

    connected = {label for label in labels if incoming[label] or outgoing[label]}
    isolated = [label for label in labels if label not in connected]

    ranks: dict[str, int] = {}
    # Kahn 法で入次数0から順に確定させる(forward は非巡回なので必ず全て埋まる)
    remaining = {label: len(incoming[label]) for label in connected}
    queue = [label for label in labels if label in connected and remaining[label] == 0]
    while queue:
        node = queue.pop(0)
        ranks[node] = max((ranks[u] + 1 for u in incoming[node] if u in ranks), default=0)
        for nxt in outgoing[node]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                queue.append(nxt)

    # 念のため: 何らかの理由で確定しなかったノードは出現順で末尾に寄せる
    unresolved = [label for label in connected if label not in ranks]
    tail = max(ranks.values(), default=-1)
    for offset, label in enumerate(unresolved, start=1):
        ranks[label] = tail + offset

    tail = max(ranks.values(), default=-1)
    for offset, label in enumerate(isolated, start=1):
        ranks[label] = tail + offset
    return ranks


def layout_grid(
    labels: list[str], ranks: dict[str, int], *, max_ranks_per_band: int = MAX_RANKS_PER_BAND
) -> dict[str, tuple[int, int, int]]:
    """rank から `(band, col, slot)` の配置を決める。

    - band = rank // max_ranks_per_band(帯の折り返し)
    - col  = rank % max_ranks_per_band(帯の中での列位置)
    - slot = 同じ rank に複数ノードがあるときの縦位置(labels の出現順)

    直線フロー(1 rank に1ノード)では slot が常に0になり、col が0,1,2,3 と進んで
    4個目で band が繰り上がる = 従来の `MAX_STEPS_PER_ROW = 4` と同じ配置になる。
    """
    by_rank: dict[int, list[str]] = {}
    for label in labels:
        by_rank.setdefault(ranks.get(label, 0), []).append(label)
    placement: dict[str, tuple[int, int, int]] = {}
    for rank, members in by_rank.items():
        band, col = divmod(rank, max_ranks_per_band)
        for slot, label in enumerate(members):
            placement[label] = (band, col, slot)
    return placement


#: フレームプリセット。動画は 16:9(通常)と 9:16(Shorts)の2種類だけを扱う。
#:
#: `frame_height` は Manim の既定(8.0)に固定し、`frame_width` をアスペクト比から
#: 導く。こうすると **同じ SceneSpec が縦型でも壊れない** —— 折り返し桁数は
#: `base.max_cols_for` が `config.frame_width` から実測で決めるので、
#: フレームが細くなれば自動的に行が短くなる。
FRAME_HEIGHT = 8.0
FRAME_ASPECTS: dict[str, tuple[int, int]] = {"16:9": (16, 9), "9:16": (9, 16)}

#: アスペクト比 x 品質 の画素寸法。`quality` は寸法の段だけを決め、
#: アスペクト比は `frame` が決める(2つの軸を混ぜない)。
FRAME_PIXELS: dict[str, dict[str, tuple[int, int]]] = {
    "16:9": {"draft": (1280, 720), "standard": (1920, 1080), "high": (2560, 1440)},
    "9:16": {"draft": (720, 1280), "standard": (1080, 1920), "high": (1440, 2560)},
}

#: セーフエリアの内側マージン(フレーム単位)。字幕・出典はここより内に収める。
SAFE_MARGIN_X = 0.6
SAFE_MARGIN_Y = 0.5

#: 縦型で1画面に載せる要素数の上限(「1画面1メッセージ」を機械的に守る)。
MAX_ELEMENTS_PORTRAIT = 4


def frame_size(aspect_ratio: str) -> tuple[float, float]:
    """アスペクト比から Manim のフレーム寸法(幅, 高さ)を返す。"""
    width_ratio, height_ratio = FRAME_ASPECTS.get(aspect_ratio, FRAME_ASPECTS["16:9"])
    return FRAME_HEIGHT * width_ratio / height_ratio, FRAME_HEIGHT


def pixel_size(aspect_ratio: str, quality: str) -> tuple[int, int]:
    """アスペクト比と品質から画素寸法を返す(未知の値は既定へ落とす)。"""
    table = FRAME_PIXELS.get(aspect_ratio, FRAME_PIXELS["16:9"])
    return table.get(quality, table["standard"])


def is_portrait(aspect_ratio: str) -> bool:
    return aspect_ratio == "9:16"


def safe_area(aspect_ratio: str) -> tuple[float, float]:
    """セーフエリアの幅・高さ(フレーム単位)。"""
    width, height = frame_size(aspect_ratio)
    return width - SAFE_MARGIN_X * 2, height - SAFE_MARGIN_Y * 2


#: 焼き込みテロップが画面下端から占める割合。`abist_kb.application.video.subtitles
#: .CAPTION_BAND_RATIO` の写し(別 venv のため import できない)。**実測値**であって
#: 理論値ではない —— 黒一色の動画へ2行のテロップを焼き、文字の上端を測った
#: (16:9 は 1080px 中 258px、9:16 は 1920px 中 411px)。少しだけ広く取って余裕を持つ。
CAPTION_BAND_RATIO: dict[str, float] = {"16:9": 0.25, "9:16": 0.23}


def caption_band_height(aspect_ratio: str) -> float:
    """テロップが占める帯の高さ(フレーム単位)。**ここに図を置かない。**

    出典フッターがこの帯に入ると、画面上は出典が消える。事実を出典に紐づける
    のがこのシステムの根幹なので、隠れているのは無いのと同じ。
    """
    _, height = frame_size(aspect_ratio)
    return height * CAPTION_BAND_RATIO.get(aspect_ratio, CAPTION_BAND_RATIO["16:9"])


def content_bounds(aspect_ratio: str) -> tuple[float, float]:
    """図を置いてよい範囲の (下端, 上端)。下はテロップ帯、上はセーフマージン。"""
    _, height = frame_size(aspect_ratio)
    return -height / 2 + caption_band_height(aspect_ratio), height / 2 - SAFE_MARGIN_Y


#: 折れ線の上下に取る余白(値域に対する割合)。
LINE_CHART_PADDING = 0.12
#: 値域が 0 からこの割合の内側に収まっていれば、0 起点のまま描く。
LINE_CHART_ZERO_ANCHOR = 0.35


def line_chart_range(values: list[float]) -> tuple[float, float]:
    """折れ線の目盛り範囲 (下端, 上端) を返す。

    棒グラフは長さが量そのものなので必ず 0 起点。折れ線は**推移**を見せる図
    なので、値が 0 から遠いところで動いているなら 0 起点にすると変化が上端に
    潰れる(315〜408 秒の差 30% が、見た目には平らな線になる)。

    ただし 0 起点をやめると差が誇張されるため、**目盛りの値を画面に出すこと**が
    条件(`chart_v1` が下端・上端のラベルを描く)。値が 0 に近いところまで
    下がっているなら、素直に 0 起点のままにする。
    """
    numbers = [float(v) for v in values]
    if not numbers:
        return 0.0, 1.0
    low, high = min(numbers), max(numbers)
    if high <= 0:
        return low - 1.0, 0.0
    if low <= high * LINE_CHART_ZERO_ANCHOR:
        return 0.0, _round_up(high * (1.0 + LINE_CHART_PADDING), high)
    span = high - low
    if span <= 0:
        # 全部同じ値。線を真ん中へ置く(高さ 0 の枠にすると描けない)。
        return low - abs(low) * LINE_CHART_PADDING - 1.0, high + abs(high) * LINE_CHART_PADDING + 1.0
    pad = span * LINE_CHART_PADDING
    return _round_down(low - pad, span), _round_up(high + pad, span)


def _nice_step(span: float) -> float:
    """値域に見合う「きりのいい」刻み(1/2/5 x 10^n)。"""
    import math

    if span <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(span))
    for factor in (0.1, 0.2, 0.5, 1.0):
        if span <= magnitude * factor * 10:
            return magnitude * factor
    return magnitude


def _round_down(value: float, span: float) -> float:
    import math

    step = _nice_step(span)
    return float(f"{math.floor(value / step) * step:.10g}")


def _round_up(value: float, span: float) -> float:
    import math

    step = _nice_step(span)
    return float(f"{math.ceil(value / step) * step:.10g}")


def fit_box(
    size: tuple[float, float], aspect_ratio: str, *, margin_x: float = SAFE_MARGIN_X
) -> tuple[float, float]:
    """図を安全域へ収めるための `(縮小率, 中心の y)` を返す。

    縮小はするが**拡大はしない**(小さい図を引き伸ばすと粗が目立つ)。縦位置は
    フレーム中央ではなく**テロップ帯を除いた領域の中央**。ここを間違えると、
    図の下端(たいてい出典フッター)がテロップに潜る。
    """
    width, height = size
    _, frame_h = frame_size(aspect_ratio)
    max_width = frame_size(aspect_ratio)[0] - margin_x * 2
    bottom, top = content_bounds(aspect_ratio)
    max_height = top - bottom

    scale = 1.0
    if width > 0 and width > max_width:
        scale = max_width / width
    if height > 0 and height * scale > max_height:
        scale = max_height / height
    return scale, (bottom + top) / 2


def subtitle_max_chars(aspect_ratio: str) -> int:
    """字幕1行の最大文字数(全角換算)。

    縦型は 1 行を短くし、文字そのものを大きく出す。横型と同じ 20 文字にすると
    画面幅に対して文字が小さくなりすぎ、Shorts で読めない。
    """
    return 12 if is_portrait(aspect_ratio) else 20


# -- モーション演出の予算 ---------------------------------------------------------
#
# 図に動きを付けると描画時間が伸びる。描画は RENDER リースで直列化されるため、
# 1シーンの増分がそのまま動画全体の待ち時間になる。**要素が増えたら演出を自動的に
# 落とす**ことで最悪ケースを抑える。ここは判断だけ(manim 非依存)で、実際の
# アニメーションは `templates/choreography.py` が持つ。

class OpacityMemo:
    """減光の前後で不透明度を覚えて戻す。

    Manim の `set_opacity(1.0)` は**塗りと線の両方**を 1.0 にする。塗らない
    前提の図形(折れ線、`progress` variant の目標枠)にこれを掛けると、シーンの
    最後で塗り潰される。減光は「戻す」ものであって「1.0 にする」ものではない。

    manim を import しない(判断だけを持つ)ので主 venv からテストできる。
    実際のアニメーションは `templates/choreography.py` が使う。
    """

    def __init__(self) -> None:
        self._saved: dict[int, tuple[float, float]] = {}

    @staticmethod
    def _leaves(mobject) -> list:
        """実際に色を持つ要素まで降りる。

        **入れ物(`VGroup` / `Text`)自身は塗りを持たない**(`fill_opacity` は 0.0)。
        入れ物を基準に減光すると、戻すときに `set_fill(0.0)` が中身へ伝播して
        **文字がまとめて消える**(比較表の中身が消えた)。葉だけを覚えて葉だけを戻す。
        """
        children = getattr(mobject, "submobjects", None)
        if not children:
            return [mobject]
        return [leaf for child in children for leaf in OpacityMemo._leaves(child)]

    @staticmethod
    def _current(mobject) -> tuple[float, float]:
        return (
            float(getattr(mobject, "fill_opacity", 0.0) or 0.0),
            float(getattr(mobject, "stroke_opacity", 1.0) or 0.0),
        )

    def remember(self, mobject) -> None:
        """元の不透明度を控える(値は動かさない)。

        アニメーションで沈める経路は、控えてから `animate` に渡す。
        """
        for leaf in self._leaves(mobject):
            self._saved.setdefault(id(leaf), self._current(leaf))

    def dim_targets(self, mobject, level: float) -> tuple[float, float]:
        """減光後の (塗り, 線) を返す。**元の値に対する比**で沈める。

        一律 `level` にすると、薄い塗りが**濃くなる**(0.0 の折れ線が塗られる)。
        基準は必ず**控えた元の値**。現在値を基準にすると、beat ごとの `focus` で
        0.25 → 0.0625 → … と掛け算が重なり、要素が画面から消える。

        入れ物を渡された場合は、葉のうち最も濃いものを代表値にする(`animate` は
        入れ物単位でしか掛けられないため)。
        """
        leaves = self._leaves(mobject)
        originals = [self._saved.get(id(leaf)) or self._current(leaf) for leaf in leaves]
        fill = max((o[0] for o in originals), default=0.0)
        stroke = max((o[1] for o in originals), default=0.0)
        return fill * level, stroke * level

    def dim(self, mobject, level: float) -> tuple[float, float]:
        self.remember(mobject)
        fill, stroke = self.dim_targets(mobject, level)
        for leaf in self._leaves(mobject):
            saved_fill, saved_stroke = self._saved[id(leaf)]
            leaf.set_fill(opacity=saved_fill * level)
            leaf.set_stroke(opacity=saved_stroke * level)
        return fill, stroke

    def reset(self) -> None:
        """控えを捨てる。シーンの頭で呼ぶ。

        キーは `id()` なので、控えたまま解放された Mobject の id が再利用されると
        別物を誤って「戻す」ことになる。シーン境界で空にしておけば起きない。
        """
        self._saved.clear()

    def restore(self, mobject) -> None:
        """減光したものだけを元へ戻す(触っていないものには手を出さない)。"""
        for leaf in self._leaves(mobject):
            saved = self._saved.pop(id(leaf), None)
            if saved is None:
                continue
            leaf.set_fill(opacity=saved[0])
            leaf.set_stroke(opacity=saved[1])


#: モーション段。閉じた列挙(エージェントが勝手な値を入れられない)。
MOTION_LEVELS: tuple[str, ...] = ("minimal", "standard", "rich")
DEFAULT_MOTION = "standard"

#: これを超える beat 数では演出を切る(1画面に載る量として既に限界)。
MOTION_BEAT_LIMIT = 20
#: カメラを動かす rich を許す beat 数の上限。寄る先が多すぎると目が回る。
RICH_BEAT_LIMIT = 12
#: 焦点化をアニメーションで見せる beat 数の上限。超えたら瞬時に切り替える。
ANIMATED_FOCUS_BEAT_LIMIT = 12
#: エッジに光を流す演出を許すエッジ数の上限。
FLOW_PULSE_EDGE_LIMIT = 16
#: 演出のために足してよい秒数の上限(1シーンあたり)。
MAX_CHOREO_EXTRA_SEC = 8.0

_FOCUS_RUN_TIME = 0.35
_PER_BEAT_EXTRA_SEC = 0.35


def resolve_motion(requested: str | None, *, quality: str, beat_count: int) -> str:
    """spec の要求と実際の条件から、実行するモーション段を決める。

    下書き(draft)は速さが要るので演出を切る。beat が多い図は、動かすより
    静かに見せたほうが読める。
    """
    level = requested if requested in MOTION_LEVELS else DEFAULT_MOTION
    if quality == "draft" or beat_count > MOTION_BEAT_LIMIT:
        return "minimal"
    if level == "rich" and beat_count > RICH_BEAT_LIMIT:
        return "standard"
    return level


def choreo_budget(beat_count: int, motion: str, *, edge_count: int = 0) -> dict:
    """そのシーンで実行してよい演出と、見込みの追加秒数。

    `focus_run_time == 0.0` は「減光はするがアニメーションはしない」の意味。
    要素が多い図で毎回 0.35 秒かけると、それだけで尺が倍になる。
    """
    if motion == "minimal":
        return {
            "focus": False,
            "focus_run_time": 0.0,
            "flow_pulse": False,
            "camera": False,
            "extra_sec": 0.0,
        }
    animated_focus = beat_count <= ANIMATED_FOCUS_BEAT_LIMIT
    flow_pulse = edge_count <= FLOW_PULSE_EDGE_LIMIT
    extra = min(MAX_CHOREO_EXTRA_SEC, beat_count * _PER_BEAT_EXTRA_SEC)
    return {
        "focus": True,
        "focus_run_time": _FOCUS_RUN_TIME if animated_focus else 0.0,
        "flow_pulse": flow_pulse,
        "camera": motion == "rich",
        "extra_sec": round(extra, 2),
    }
