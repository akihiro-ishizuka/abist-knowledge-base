"""`artifact_store` と `manim_runner` の純関数（従来テストが無かった箇所）。

`normalize_slug` の Windows 予約名対応や `resolve_python` の解決順は、これまで
renderer 経由でしか触られず単体で固定されていなかった。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.infrastructure.visualization.artifact_store import (
    assert_inside_path,
    create_visualization_dir,
    make_visualization_id,
    normalize_slug,
    visualizations_dir,
)
from abist_kb.infrastructure.visualization.manim_runner import (
    default_lease_ttl_seconds,
    default_timeout_seconds,
    resolve_python,
    visualize_python_path,
)


class TestNormalizeSlug:
    def test_ascii_is_lowercased_and_hyphenated(self) -> None:
        assert normalize_slug("Hello World") == "hello-world"

    def test_japanese_only_falls_back(self) -> None:
        """日本語だけのタイトルは ascii 化で消えるので fallback になる。"""
        assert normalize_slug("蛇腹パターン") == "scene"

    @pytest.mark.parametrize("reserved", ["con", "CON", "prn", "aux", "nul", "com1", "lpt9"])
    def test_windows_reserved_names_are_suffixed(self, reserved: str) -> None:
        """Windows の予約デバイス名はそのままだとディレクトリを作れない。"""
        assert normalize_slug(reserved) != reserved.lower()
        assert normalize_slug(reserved).startswith(reserved.lower())

    def test_long_slug_is_truncated(self) -> None:
        assert len(normalize_slug("a" * 200)) <= 60

    def test_empty_and_none_fall_back(self) -> None:
        assert normalize_slug("") == "scene"
        assert normalize_slug(None) == "scene"

    def test_no_trailing_hyphen(self) -> None:
        assert not normalize_slug("abc---").endswith("-")


class TestVisualizationId:
    def test_has_no_colon(self) -> None:
        """`:` は Windows のパスに使えないので ID に含めない。"""
        assert ":" not in make_visualization_id("title")

    def test_shape(self) -> None:
        parts = make_visualization_id("my title").split("-")
        assert parts[0].endswith("Z")
        assert len(parts[-1]) == 4


class TestCreateVisualizationDir:
    def test_creates_unique_directories(self, tmp_path: Path) -> None:
        first = create_visualization_dir(tmp_path, "同じ題")
        second = create_visualization_dir(tmp_path, "同じ題")
        assert first.dir != second.dir, "同じ題名でも別ディレクトリになること"
        assert first.dir.is_dir() and second.dir.is_dir()
        assert ":" not in first.visualization_id


class TestAssertInsidePath:
    def test_allows_paths_inside(self, tmp_path: Path) -> None:
        assert_inside_path(tmp_path, tmp_path / "a" / "b.png")

    def test_rejects_escape(self, tmp_path: Path) -> None:
        with pytest.raises(Exception):  # noqa: B017 - 例外型は実装詳細
            assert_inside_path(tmp_path / "out", tmp_path / "out" / ".." / ".." / "evil.png")


class TestVisualizationsDir:
    def test_derives_subdirectory(self, tmp_path: Path) -> None:
        assert visualizations_dir(tmp_path) == tmp_path / "visualizations"

    def test_is_not_applied_twice(self, tmp_path: Path) -> None:
        """語義は「常にベースの reports/」。二重適用しないことを固定する。"""
        once = visualizations_dir(tmp_path)
        assert once.name == "visualizations"
        assert once.parent == tmp_path


class TestResolvePython:
    def test_env_override_wins(self, tmp_path: Path) -> None:
        assert (
            resolve_python(root=tmp_path, env={"KB_VISUALIZE_PYTHON": "C:/custom/python.exe"})
            == "C:/custom/python.exe"
        )

    def test_repo_venv_is_used_when_present(self, tmp_path: Path) -> None:
        exe = tmp_path / ".venv-visualize" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        bin_exe = tmp_path / ".venv-visualize" / "bin" / "python"
        bin_exe.parent.mkdir(parents=True, exist_ok=True)
        bin_exe.write_text("", encoding="utf-8")
        assert ".venv-visualize" in resolve_python(root=tmp_path, env={})

    def test_falls_back_to_launcher(self, tmp_path: Path) -> None:
        assert resolve_python(root=tmp_path, env={}) in ("py", "python3")

    def test_visualize_python_path_returns_none_without_venv(self, tmp_path: Path) -> None:
        """`resolve_python` と違い、フォールバックは返さない。"""
        assert visualize_python_path(root=tmp_path) is None


class TestTimeoutParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 600.0), ("", 600.0), ("abc", 600.0), ("0", 600.0), ("-1", 600.0), ("5000", 5.0)],
    )
    def test_timeout(self, monkeypatch, raw: str | None, expected: float) -> None:
        if raw is None:
            monkeypatch.delenv("KB_VISUALIZE_TIMEOUT_MS", raising=False)
        else:
            monkeypatch.setenv("KB_VISUALIZE_TIMEOUT_MS", raw)
        assert default_timeout_seconds() == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 30.0), ("abc", 30.0), ("0", 30.0), ("-5", 30.0), ("45", 45.0)],
    )
    def test_lease_ttl(self, monkeypatch, raw: str | None, expected: float) -> None:
        if raw is None:
            monkeypatch.delenv("KB_VISUALIZE_LEASE_TTL_SECONDS", raising=False)
        else:
            monkeypatch.setenv("KB_VISUALIZE_LEASE_TTL_SECONDS", raw)
        assert default_lease_ttl_seconds() == expected

    def test_lease_ttl_is_far_below_the_render_timeout(self) -> None:
        """TTL は「処理の最大時間」に縛られない（自動更新するため）。

        従来は timeout+60 秒 = 11 分で、実測 4.6 秒に対して2桁過剰だった。
        """
        assert default_lease_ttl_seconds() < default_timeout_seconds() / 10
