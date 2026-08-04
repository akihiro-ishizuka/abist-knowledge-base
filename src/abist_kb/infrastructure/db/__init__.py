"""SQLite 接続基盤(設計書 §9.2, §10)。

接続ファクトリとマイグレーションランナーの実装は連携先モジュールを直接参照する。
"""

from __future__ import annotations
