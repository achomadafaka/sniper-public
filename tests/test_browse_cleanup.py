import pytest

import scripts.browse as browse


def test_screenshot_closes_context_and_browser_when_navigation_fails(monkeypatch, tmp_path):
    closed = {"context": False, "browser": False}

    class Page:
        def set_default_timeout(self, _timeout):
            pass

        def goto(self, *_args, **_kwargs):
            raise RuntimeError("navigation failed")

    class Context:
        def new_page(self):
            return Page()

        def close(self):
            closed["context"] = True

    class Browser:
        def new_context(self, **_kwargs):
            return Context()

        def close(self):
            closed["browser"] = True

    class Chromium:
        def launch(self, **_kwargs):
            return Browser()

    class Playwright:
        chromium = Chromium()

    class Manager:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(browse, "sync_playwright", Manager)

    with pytest.raises(RuntimeError, match="navigation failed"):
        browse.screenshot("https://example.test", str(tmp_path / "shot.png"), wait=0)

    assert closed == {"context": True, "browser": True}
