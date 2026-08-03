"""ジョブ状態・エラー等をセマンティックトークンへ写像する(§6.1)。

**色だけで状態を伝えない**という設計原則を、Web/TUI 両方の view-model が
同じ判定ロジックで守れるようにする。ここではトークン名(`SemanticToken`)を
返すだけで、Rich style や web hex は選ばない — それは
`presentation/console/theme.py` の `TOKEN_STYLES` を参照する側(Web の
`theme.py`、将来の TUI テーマ)の責務。
"""

from __future__ import annotations

from abist_kb.domain.job import JobState
from abist_kb.presentation.console.theme import SemanticToken

JOB_STATE_TOKENS: dict[JobState, SemanticToken] = {
    JobState.QUEUED: SemanticToken.MUTED,
    JobState.RUNNING: SemanticToken.INFO,
    JobState.SUCCEEDED: SemanticToken.SUCCESS,
    JobState.PARTIAL: SemanticToken.WARNING,
    JobState.FAILED: SemanticToken.DANGER,
    JobState.CANCELLED: SemanticToken.MUTED,
    JobState.INTERRUPTED: SemanticToken.WARNING,
}


def job_state_token(state: JobState) -> SemanticToken:
    """ジョブ状態に対応するセマンティックトークン。

    `PARTIAL` は `WARNING`(黄)であって `SUCCESS`(緑)ではない —
    一部失敗したジョブは成功として塗ってはならない(M3 の申し送り、
    design/plans/M6-M10-remaining.md の共通申し送り表を参照)。
    """
    return JOB_STATE_TOKENS.get(state, SemanticToken.MUTED)


__all__ = ["JOB_STATE_TOKENS", "job_state_token"]
