"""破壊的操作の確認モーダルとヘルプモーダル(§7.2 TUI要件)。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

HELP_TEXT = """\
キー操作
  Ctrl+K   コマンドパレット
  /        検索へ移動
  r        現在の画面を再読込
  x        選択したソース/バッチを削除
  t        選択したソースの接続テスト
  e        選択したバッチを実行
  Esc      前の画面へ戻る
  ?        このヘルプ
  q        終了
  1-9      左ナビの各領域へ直接移動
"""


class ConfirmModal(ModalScreen[bool]):
    """破壊的操作(ジョブのキャンセル・再試行等)の確認モーダル。

    `dismiss(True)`/`dismiss(False)` で呼び出し元へ結果を返す。
    """

    BINDINGS = [
        Binding("escape", "cancel", "キャンセル"),
        Binding("y", "confirm", "はい"),
        Binding("n", "cancel", "いいえ"),
    ]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 60;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, message: str, *, danger: bool = True) -> None:
        super().__init__()
        self._message = message
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message, id="confirm-message")
            yield Static("")
            with Vertical():
                yield Button(
                    "はい (y)",
                    id="confirm-yes",
                    variant="error" if self._danger else "primary",
                )
                yield Button("いいえ (n)", id="confirm-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HelpModal(ModalScreen[None]):
    """`?` で開くキー割当一覧。"""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "閉じる"),
        Binding("q", "dismiss_modal", "閉じる"),
    ]

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal > Vertical {
        width: 60;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("ヘルプ", id="help-title")
            yield Static(HELP_TEXT)
            yield Button("閉じる (Esc)", id="help-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


__all__ = ["ConfirmModal", "HelpModal"]
