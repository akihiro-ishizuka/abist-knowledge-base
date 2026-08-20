"""送信者の分類と自己応答ループの防止(設計 §4)。

`context_only` は「除外」ではない。発言は文脈として読むが、応答トリガーにも
リマインド対象にもしない。

自己応答ループの防止は2つの独立したガードで行う。active 判定に含まれない送信者
は結果的に応答対象外になるが、それは副作用であって防御ではない。メンバー構成が
変われば崩れるため、以下2つを明示的に持つ。

1. 送信者が Workflows なら無条件除外
2. 本文先頭が `【AI設計エージェント】` なら除外
"""

from __future__ import annotations

from abist_kb.domain.chat_watch import InboundMessage, MemberRole

#: 本エージェントの投稿は Webhook 経由のため Teams 上ではこの送信者になる。
#: 人間の発言と区別できる唯一の手掛かり(設計 §4.3)。
WORKFLOWS_SENDER = "workflows@teams.microsoft.com"

#: Adaptive Card 本文の先頭に必ず置く。名義が Workflows のままで
#: 「石塚 昭宏 used a Workflow template」と表示されるため、名乗らないと
#: 石塚さんの発言と誤読される(設計 §8.2)。
AI_PREFIX = "【AI設計エージェント】"

ACTIVE_MEMBERS: dict[str, str] = {
    "t_isaka@abist.co.jp": "井坂 孝",
    "d_suzuki@abist.co.jp": "鈴木 大智",
    "a_ishizuka@abist.co.jp": "石塚 昭宏",
    "ma_ishii@abist.co.jp": "石井 優人",
    "y_osawa@abist.co.jp": "大澤 祐太",
    "t_niizeki@abist.co.jp": "新関 智也",
}

CONTEXT_ONLY_MEMBERS: dict[str, str] = {
    "yamaura@abist.co.jp": "山浦 雅生",
    "mi_hashikawa@abist.co.jp": "橋川 幹宏",
    "morita.abist@gmail.com": "森田 雅継",
    "j_saga@abist.co.jp": "佐賀 淳治",
}


def classify(message: InboundMessage) -> MemberRole:
    """送信者の役割を返す。

    `CONTEXT_ONLY_MEMBERS` に載っている送信者は明示的に `context_only`。
    未知の送信者も同じ `context_only` へ落ちるが、これは名簿を持たない送信者を
    誤って `active` にしないための安全側のフォールバックであり、
    `CONTEXT_ONLY_MEMBERS` による判定と意味は異なる(設計 §4)。
    """
    email = message.sender_email
    if email == WORKFLOWS_SENDER:
        return MemberRole.SYSTEM
    if email in ACTIVE_MEMBERS:
        return MemberRole.ACTIVE
    if email in CONTEXT_ONLY_MEMBERS:
        return MemberRole.CONTEXT_ONLY
    return MemberRole.CONTEXT_ONLY


def is_self_post(message: InboundMessage) -> bool:
    """本エージェント自身の投稿なら True。

    送信者判定と本文先頭判定の両方を見る。片方をすり抜けても止まるようにする。
    """
    if message.sender_email == WORKFLOWS_SENDER:
        return True
    return message.body.lstrip().startswith(AI_PREFIX)
