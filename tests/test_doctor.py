"""設定の健康診断。

このシステムの失敗はどれも**エラーにならない**。テーマ未登録なら警句しか
出ず、DB が揮発する場所にあれば毎回記憶を失い、材料に的外れな語が1つ
混ざればその語を含む案が出続ける。動いているように見えたまま役に立たなく
なるので、診断が要る。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ideaman import service
from ideaman.doctor import diagnose


def _titles(store):
    return [f.title for f in diagnose(store).findings]


def _levels(store):
    return {f.title: f.level for f in diagnose(store).findings}


def test_a_fresh_store_says_register_a_theme(store):
    result = diagnose(store)
    assert result.worst == "bad"
    theme_finding = next(f for f in result.findings if "テーマ" in f.title)
    assert theme_finding.level == "bad"
    assert theme_finding.fix and "--scan" in theme_finding.fix


def test_a_theme_without_materials_is_bad(store):
    store.add_theme("空のテーマ")
    levels = _levels(store)
    assert levels["「空のテーマ」に材料がありません"] == "bad"


def test_thin_materials_are_a_warning(store):
    theme = store.add_theme("痩せたテーマ")
    store.add_materials(theme.id, ["通知", "語彙"])
    warned = [f for f in diagnose(store).findings if "語しかありません" in f.title]
    assert warned and warned[0].level == "warn"


def test_junk_materials_are_named_with_a_drop_command(store):
    """止め語が材料に混ざっていたら、外すコマンドをそのまま出す。"""
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "場合", "データ", "連鎖"])
    junk = next(f for f in diagnose(store).findings if "的外れ" in f.title)
    assert junk.level == "warn"
    assert "場合" in junk.detail and "データ" in junk.detail
    assert junk.fix and "--drop" in junk.fix
    assert "通知" not in junk.fix


def test_a_paused_theme_set_is_a_warning(store):
    theme = store.add_theme("休むほう")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫"])
    store.set_theme_active(theme.id, False)
    levels = _levels(store)
    assert levels["有効なテーマがありません"] == "warn"


def test_an_idle_worker_is_reported(store):
    theme = store.add_theme("t")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫"])
    service.generate_idea(store, use_llm=False)

    old = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    store.conn.execute("UPDATE ideas SET created_at = ?", (old,))
    store.conn.commit()

    stale = next(f for f in diagnose(store).findings if "考えていません" in f.title)
    assert stale.level == "warn"
    assert "9 時間前" in stale.detail


def test_a_healthy_setup_reports_no_problem(store):
    # pytest の一時ディレクトリは /tmp 配下なので、揮発判定に引っかかる。
    # ここで見たいのはそこではないので、永続ディスク上ということにする。
    store.path = Path("/data/ideaman.db")
    theme = store.add_theme("健康なテーマ")
    store.add_materials(theme.id, ["通知", "語彙", "連鎖", "在庫", "評価"])
    for _ in range(60):
        service.generate_idea(store, use_llm=False)
    ideas = [i for i in store.search(None, limit=200)][:25]
    for idea in ideas:
        store.rate(idea.id, 1)

    result = diagnose(store)
    assert result.worst == "ok", result.report()
    assert "問題ありません" in result.report()


def test_an_ephemeral_database_is_flagged(tmp_path):
    """/tmp に置くと再起動で消える。溜まらないなら本体が成立しない。"""
    from ideaman.store import Store

    s = Store(tmp_path / "ideaman.db")
    s.path = Path("/tmp/ideaman.db")
    try:
        finding = next(f for f in diagnose(s).findings if "消える場所" in f.title)
        assert finding.level == "bad"
        assert "IDEAMAN_DB" in (finding.fix or "")
    finally:
        s.close()


@pytest.mark.parametrize("level", ["ok", "warn", "bad"])
def test_report_always_ends_with_guidance(store, level):
    text = diagnose(store).report()
    assert text.strip()
    assert text.rstrip().endswith("。")
