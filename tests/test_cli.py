from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zakupki_monitor import config
from zakupki_monitor.cli import app
from zakupki_monitor.profiles import load_profile_file

from .test_search_url import TEST_URL

runner = CliRunner()
ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ZM_DB_PATH", str(tmp_path / "cli.db"))
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_example_profile_file_is_valid() -> None:
    spec = load_profile_file(ROOT / "profiles-example" / "gas-design.yaml")
    assert spec.search and spec.search.okpd2_codes == ["71.12.19"]
    assert len(spec.rules) == 6


def test_cli_flow(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init-db"]).exit_code == 0
    r = runner.invoke(
        app, ["profile", "add", "--file", str(ROOT / "profiles-example" / "gas-design.yaml")]
    )
    assert r.exit_code == 0, r.output
    assert (
        runner.invoke(
            app, ["profile", "add", "-f", str(ROOT / "profiles-example" / "gas-design.yaml")]
        ).exit_code
        == 1
    )
    r = runner.invoke(app, ["profile", "import-url", TEST_URL, "--name", "Тест"])
    assert r.exit_code == 0, r.output
    out = runner.invoke(app, ["profile", "list"]).output
    assert "Проектирование газопроводов" in out and "Тест" in out and "мособлгаз" in out
    assert runner.invoke(app, ["tenders", "list", "--top", "5"]).exit_code == 0
