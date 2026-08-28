from rembggui.app import main


def test_main_reports_version_without_opening_qt(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("rembgGUI ")


def test_main_smoke_test_is_headless(capsys):
    assert main(["--smoke-test"]) == 0
    assert "smoke: ok" in capsys.readouterr().out
