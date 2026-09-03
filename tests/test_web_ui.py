"""ブラウザで実際に触る。

Web UI は主要な入口なのに、一度も動かしていなかった。JavaScript は
テストが無いと、壊れても誰も気づかない（サーバは 200 を返し続ける）。

`playwright` かブラウザが無い環境では丸ごと飛ばす。本体の方針
（追加依存が無くても動く）を崩さないため。
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

#: 環境に置かれている Chromium を探す。無ければテストごと飛ばす。
_CANDIDATES = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
]


def _browser_path() -> str | None:
    for path in _CANDIDATES:
        if Path(path).exists():
            return path
    for pattern in ("chromium-*/chrome-linux/chrome",):
        root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
        found = sorted(root.glob(pattern))
        if found:
            return str(found[-1])
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """本物の uvicorn を立てる。TestClient では JavaScript が動かない。"""
    work = tmp_path_factory.mktemp("ui")
    port = _free_port()
    env = {
        **os.environ,
        "IDEAMAN_DB": str(work / "ui.db"),
        "IDEAMAN_STOCK": "3",
    }
    env.pop("IDEAMAN_TOKEN", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ideaman.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            if process.poll() is not None:
                pytest.fail("サーバが起動しなかった")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("サーバが応答しなかった")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def page(server):
    path = _browser_path()
    if path is None:
        pytest.skip("Chromium が見つからない")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=path, args=["--no-sandbox"])
        page = browser.new_page()
        page.problems: list[str] = []
        page.on("pageerror", lambda e: page.problems.append(f"JS例外: {e}"))
        page.on(
            "console",
            lambda m: page.problems.append(f"console: {m.text}")
            if m.type == "error"
            else None,
        )
        page.goto(server, wait_until="networkidle")
        yield page
        browser.close()


def test_the_page_loads_without_errors(page):
    """404 も JS 例外も出さないこと。

    `/favicon.ico` を別ファイルにしていたとき、毎回 404 がコンソールと
    サーバのログに出ていた。
    """
    assert page.title() == "IDEAMAN"
    assert "溜まっている" in page.inner_text("#stock")
    assert page.problems == []


def test_registering_a_theme_changes_what_comes_out(page):
    page.click("#themes-toggle")
    page.fill("#theme-name", "受注フローを直す")
    page.fill("#theme-materials", "受注, 検品 ,請求")
    page.click("#theme-form button[type=submit]")
    page.wait_for_selector("#theme-list .mat")

    chips = page.eval_on_selector_all("#theme-list .mat", "e => e.map(x => x.textContent)")
    assert chips == ["受注", "検品", "請求"], "前後の空白が落ちていない"

    page.click("#summon")
    page.wait_for_function(
        "() => document.querySelector('#idea').textContent.trim() !== '・・・'",
        timeout=30000,
    )
    text = page.inner_text("#idea")
    assert any(m in text for m in ("受注", "検品", "請求")), text
    assert page.problems == []


def test_a_junk_material_is_marked_so_it_can_be_removed(page):
    """CLI と doctor は警告するのに、Web だけ黙っていた。"""
    page.click("#themes-toggle")
    page.fill("#theme-name", "t")
    page.fill("#theme-materials", "受注,場合,検品")
    page.click("#theme-form button[type=submit]")
    page.wait_for_selector("#theme-list .mat.weak")

    weak = page.eval_on_selector_all(".mat.weak", "e => e.map(x => x.textContent)")
    assert weak == ["場合"]

    # 押すと外れる
    page.click(".mat.weak")
    page.wait_for_function("() => !document.querySelector('.mat.weak')")
    chips = page.eval_on_selector_all("#theme-list .mat", "e => e.map(x => x.textContent)")
    assert "場合" not in chips
    assert page.problems == []


def test_rating_reaches_the_archive(page):
    page.click("#themes-toggle")
    page.fill("#theme-name", "t")
    page.fill("#theme-materials", "受注,検品,請求")
    page.click("#theme-form button[type=submit]")
    page.wait_for_selector("#theme-list .mat")

    page.click("#summon")
    page.wait_for_function(
        "() => document.querySelector('#idea').textContent.trim() !== '・・・'",
        timeout=30000,
    )
    idea = page.inner_text("#idea")

    page.click('.vote[data-score="1"]')
    page.wait_for_function("() => document.querySelector('.vote.chosen')")

    page.click("#archive-toggle")
    page.wait_for_selector("#archive-list li")
    archive = page.inner_text("#archive-list")
    assert idea in archive
    assert "+1" in archive
    assert page.problems == []
