"""互換 re-export: 本体は `presentation/common/serialize.py`。"""

from __future__ import annotations

from abist_kb.presentation.common.serialize import error_to_dict, event_to_dict, job_to_dict

__all__ = ["error_to_dict", "event_to_dict", "job_to_dict"]
