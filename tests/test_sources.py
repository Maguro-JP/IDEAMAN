"""外の文章を取ってくる部分。**ネットワークには出ない。**

取得の関数を偽の応答に差し替えて動かす。見るのは採取の約束ごと:

- 間隔を 1 秒より詰めない
- 見た題を二度読まない
- すでに保存した一文を候補に出さない
- 候補は `mechanisms.yaml` と同じ形で、`aspect` と `domains` は空
- 保存先には書かない（候補は生成に使われない）
"""

import email.message
import json
import random
import urllib.error
import urllib.parse

import pytest
import yaml

from ideaman import sources
from ideaman.cli import main
from ideaman.mechanism import looks_like_mechanism

#: 抽出器を通る一文と、通らない文。
PASSES = "改札は通れなかった人を後ろに溜めるのではなく、脇へ出して個別に捌く。"
PASSES_TOO = "開口部を広げることでピッチを上げ、狭めることでピッチを下げる。"
FAILS = "改札は駅の出入口に置かれている。"

SAVED = [
    {
        "subject": "パイプオルガン",
        "how": PASSES_TOO.rstrip("。"),
        "source": "https://ja.wikipedia.org/wiki/パイプオルガン",
        "domains": ["structure", "creation"],
    }
]


class FakeWiki:
    """偽のウィキペディア。題ごとのリンクと本文を持ち、叩かれた URL を残す。"""

    def __init__(self, links, texts, redirects=None):
        self.links = links
        self.texts = texts
        self.redirects = redirects or {}
        self.calls: list[dict] = []

    def __call__(self, url: str) -> bytes:
        assert url.startswith(sources.API + "?")
        query = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
        self.calls.append(query)
        title = query["titles"]
        if query["prop"] == "links":
            page = {"title": title, "links": [{"ns": 0, "title": t} for t in self.links[title]]}
        else:
            target = self.redirects.get(title, title)
            page = {"title": target}
            if target in self.texts:
                page["extract"] = self.texts[target]
            else:
                page["missing"] = ""
        return json.dumps({"query": {"pages": {"1": page}}}).encode()

    def read_titles(self) -> list[str]:
        return [c["titles"] for c in self.calls if c["prop"] == "extracts"]


def _wiki(fake) -> sources.Wikipedia:
    return sources.Wikipedia(fake, sleep=lambda _: None, clock=lambda: 0.0)


def _collect(fake, tmp_path, count=10, saved=SAVED, **kwargs):
    return sources.collect(
        count,
        wiki=_wiki(fake),
        saved=saved,
        visited_path=tmp_path / "visited.txt",
        rng=random.Random(0),
        **kwargs,
    )


def test_the_sample_sentences_are_what_they_claim():
    """下のテストは、この 3 文が通る・通らないことに乗っている。"""
    assert looks_like_mechanism(PASSES)
    assert looks_like_mechanism(PASSES_TOO)
    assert not looks_like_mechanism(FAILS)


def test_candidates_come_from_the_links_of_saved_articles(tmp_path):
    fake = FakeWiki(
        links={"パイプオルガン": ["改札", "駅"]},
        texts={"改札": FAILS + PASSES, "駅": FAILS},
    )
    result = _collect(fake, tmp_path)

    assert result.articles == 2
    assert result.sentences == 3
    assert [(c.subject, c.how, c.source) for c in result.candidates] == [
        ("改札", PASSES.rstrip("。"), "https://ja.wikipedia.org/wiki/改札")
    ]
    assert result.reasons.most_common(1)[0][1] == 2
    # 起点の記事そのものは読み直さない。読むのはリンク先だけ。
    assert sorted(fake.read_titles()) == ["改札", "駅"]
    assert result.exhausted  # 10 記事を頼んだが、辿れるのは 2 記事だった


def test_a_title_is_never_read_twice(tmp_path):
    """見た題は記録に残り、次の回には読まれない。"""
    fake = FakeWiki(links={"パイプオルガン": ["改札", "駅"]}, texts={"改札": PASSES, "駅": FAILS})
    _collect(fake, tmp_path)
    visited = (tmp_path / "visited.txt").read_text(encoding="utf-8").splitlines()
    assert sorted(visited) == ["改札", "駅"]

    again = FakeWiki(links={"パイプオルガン": ["改札", "駅"]}, texts={"改札": PASSES, "駅": FAILS})
    result = _collect(again, tmp_path)
    assert again.read_titles() == []
    assert result.articles == 0
    assert result.candidates == []


def test_a_sentence_already_saved_is_not_offered_again(tmp_path):
    fake = FakeWiki(links={"パイプオルガン": ["オルガン"]}, texts={"オルガン": PASSES_TOO + PASSES})
    result = _collect(fake, tmp_path)
    assert [c.how for c in result.candidates] == [PASSES.rstrip("。")]
    assert result.already_saved == 1


def test_the_same_sentence_in_two_articles_is_offered_once(tmp_path):
    fake = FakeWiki(
        links={"パイプオルガン": ["改札", "自動改札機"]},
        texts={"改札": PASSES, "自動改札機": PASSES},
    )
    result = _collect(fake, tmp_path)
    assert len(result.candidates) == 1
    assert result.already_saved == 1


def test_it_stops_at_the_count(tmp_path):
    titles = [f"記事{chr(0x3042 + i)}" for i in range(4)]
    fake = FakeWiki(links={"パイプオルガン": titles}, texts=dict.fromkeys(titles, PASSES))
    result = _collect(fake, tmp_path, count=2)
    assert result.articles == 2
    assert len(fake.read_titles()) == 2
    assert not result.exhausted


def test_one_article_does_not_supply_all_the_links(tmp_path):
    """1 つの記事から辿る数に上限がある。その記事の分野に偏らないように。"""
    titles = [f"記事{chr(0x3042 + i)}" for i in range(9)]
    fake = FakeWiki(links={"パイプオルガン": titles}, texts=dict.fromkeys(titles, FAILS))
    result = _collect(fake, tmp_path, count=9, per_seed=3)
    assert result.articles == 3


def test_years_dates_and_lists_are_not_read(tmp_path):
    skipped = ["1964年", "3月11日", "日本の鉄道駅一覧", "20世紀", "改札 (曖昧さ回避)"]
    fake = FakeWiki(links={"パイプオルガン": [*skipped, "改札"]}, texts={"改札": PASSES})
    _collect(fake, tmp_path)
    assert fake.read_titles() == ["改札"]
    assert all(not sources.worth_reading(t) for t in skipped)
    assert sources.worth_reading("改札")


def test_a_redirect_is_read_once_under_its_real_title(tmp_path):
    """別の題から同じ記事に着いたら、2 回目は数えない。題は転送の先を使う。"""
    fake = FakeWiki(
        links={"パイプオルガン": ["自動改札", "改札機"]},
        texts={"自動改札機": PASSES},
        redirects={"自動改札": "自動改札機", "改札機": "自動改札機"},
    )
    result = _collect(fake, tmp_path)
    assert result.articles == 1
    assert [c.subject for c in result.candidates] == ["自動改札機"]
    visited = set((tmp_path / "visited.txt").read_text(encoding="utf-8").splitlines())
    assert visited == {"自動改札", "改札機", "自動改札機"}


def test_a_missing_article_is_remembered_but_not_counted(tmp_path):
    fake = FakeWiki(links={"パイプオルガン": ["無い記事"]}, texts={})
    result = _collect(fake, tmp_path)
    assert result.articles == 0
    assert (tmp_path / "visited.txt").read_text(encoding="utf-8").splitlines() == ["無い記事"]


def test_a_failed_fetch_is_reported_and_can_be_retried(tmp_path):
    """取れなかった題は「見た」に入れない。次の回にもう一度試せる。"""

    class Flaky(FakeWiki):
        def __call__(self, url):
            if "extracts" in url and urllib.parse.quote("駅") in url:
                raise OSError("接続できない")
            return super().__call__(url)

    fake = Flaky(links={"パイプオルガン": ["改札", "駅"]}, texts={"改札": PASSES, "駅": PASSES})
    result = _collect(fake, tmp_path)
    assert result.articles == 1
    assert [title for title, _ in result.failed] == ["駅"]
    assert (tmp_path / "visited.txt").read_text(encoding="utf-8").splitlines() == ["改札"]


def test_requests_are_at_least_a_second_apart():
    """どの取得も、前の取得から 1 秒以上あける。偽の時計で確かめる。"""
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    def fetch(url: str) -> bytes:
        now[0] += 0.3  # 取得そのものに 0.3 秒かかる
        return b'{"query": {"pages": {}}}'

    wiki = sources.Wikipedia(fetch, sleep=sleep, clock=lambda: now[0])
    wiki.article("改札")
    assert slept == []  # 最初の 1 回は待たない
    wiki.article("駅")
    wiki.links("駅")
    assert slept == [pytest.approx(1.0), pytest.approx(1.0)]
    now[0] += 5.0  # 十分に間が空いていれば待たない
    wiki.article("切符")
    assert len(slept) == 2


def test_the_interval_cannot_be_set_below_a_second():
    with pytest.raises(ValueError):
        sources.Wikipedia(lambda url: b"{}", interval=0.2)


def _too_many(url: str, retry_after: str | None = "7") -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, 429, "Too Many Requests", headers, None)


def test_too_many_requests_waits_as_told_and_then_slows_down():
    """429 には言われた秒数だけ待ってやり直し、以後の間隔も広げる。

    1 秒あけていても、本物のウィキペディアは 3 回目の取得で 429 を返した。
    待ってから元の間隔に戻すと、すぐまた止められる。
    """
    now = [0.0]
    slept: list[float] = []
    answers = ["429", "ok", "ok"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    def fetch(url: str) -> bytes:
        if answers.pop(0) == "429":
            raise _too_many(url)
        return b'{"query": {"pages": {}}}'

    wiki = sources.Wikipedia(fetch, sleep=sleep, clock=lambda: now[0])
    wiki.article("改札")
    assert slept == [7.0]  # 言われた 7 秒。そのあいだに広げた間隔（2 秒）も過ぎている
    assert wiki.interval == 2.0
    wiki.article("駅")
    assert slept == [7.0, 2.0]


def test_too_many_requests_gives_up_after_a_few_tries():
    calls: list[str] = []
    slept: list[float] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        raise _too_many(url, retry_after=None)

    wiki = sources.Wikipedia(fetch, sleep=slept.append, clock=lambda: 0.0)
    with pytest.raises(urllib.error.HTTPError):
        wiki.article("改札")
    assert len(calls) == sources.MAX_RETRIES + 1
    # 秒数を言われなかったら既定の秒数。間隔は上限で止まる。
    assert sources.DEFAULT_BACKOFF in slept
    assert wiki.interval <= sources.MAX_INTERVAL


def test_other_http_errors_are_not_retried():
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        raise urllib.error.HTTPError(url, 500, "Server Error", email.message.Message(), None)

    wiki = sources.Wikipedia(fetch, sleep=lambda _: None, clock=lambda: 0.0)
    with pytest.raises(urllib.error.HTTPError):
        wiki.article("改札")
    assert len(calls) == 1


def test_links_follow_the_continuation():
    pages = [
        {"continue": {"plcontinue": "1|0|次", "continue": "||"},
         "query": {"pages": {"1": {"links": [{"title": "改札"}]}}}},
        {"query": {"pages": {"1": {"links": [{"title": "駅"}]}}}},
    ]
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return json.dumps(pages[len(seen) - 1]).encode()

    wiki = sources.Wikipedia(fetch, sleep=lambda _: None, clock=lambda: 0.0)
    assert wiki.links("パイプオルガン") == ["改札", "駅"]
    assert "plcontinue=" in seen[1]


def test_the_default_fetch_names_a_generic_client(monkeypatch):
    """名乗るのは道具の名前だけ。利用者の連絡先を載せない。"""
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def urlopen(request, timeout):
        captured["agent"] = request.get_header("User-agent")
        return Response()

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    assert sources.fetch_url("https://ja.wikipedia.org/w/api.php") == b"ok"
    assert captured["agent"] == sources.USER_AGENT
    assert "@" not in sources.USER_AGENT and "http" not in sources.USER_AGENT


def test_urls_and_titles_round_trip():
    assert sources.article_url("ごみ収集車") == "https://ja.wikipedia.org/wiki/ごみ収集車"
    assert sources.article_url("A B") == "https://ja.wikipedia.org/wiki/A_B"
    assert sources.title_from_url("https://ja.wikipedia.org/wiki/A_B") == "A B"
    assert sources.title_from_url("https://ja.wikipedia.org/wiki/%E6%94%B9%E6%9C%AD") == "改札"
    assert sources.title_from_url("https://example.com/wiki/改札") is None


def test_candidates_are_written_in_the_shape_of_the_saved_file(tmp_path):
    """候補の YAML は保存先と同じ形で、読んで付ける欄は空。"""
    fake = FakeWiki(links={"パイプオルガン": ["改札"]}, texts={"改札": PASSES})
    result = _collect(fake, tmp_path)
    loaded = yaml.safe_load(sources.to_yaml(result.candidates))
    assert loaded == {
        "mechanisms": [
            {
                "subject": "改札",
                "how": PASSES.rstrip("。"),
                "source": "https://ja.wikipedia.org/wiki/改札",
                "aspect": None,
                "domains": [],
            }
        ]
    }
    assert yaml.safe_load(sources.to_yaml([])) == {"mechanisms": []}


def test_quotes_in_a_sentence_survive_the_yaml():
    from ideaman.mechanism import Mechanism

    item = Mechanism(subject='"A": B', how='「止まれ」と"表示"することで\\車を止める', source="s")
    loaded = yaml.safe_load(sources.to_yaml([item]))["mechanisms"][0]
    assert (loaded["subject"], loaded["how"]) == (item.subject, item.how)


# --- コマンド ---------------------------------------------------------


@pytest.fixture
def harvest_cli(tmp_path, monkeypatch):
    """`ideaman harvest` を、偽の応答と一時の保存先で動かす。"""
    monkeypatch.setenv("IDEAMAN_DB", str(tmp_path / "cli.db"))
    saved = tmp_path / "mechanisms.yaml"
    saved.write_text(yaml.safe_dump({"mechanisms": SAVED}, allow_unicode=True), encoding="utf-8")
    fake = FakeWiki(links={"パイプオルガン": ["改札", "駅"]}, texts={"改札": PASSES, "駅": FAILS})
    monkeypatch.setattr(sources, "fetch_url", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda _: None)
    base = ["harvest", "--mechanisms", str(saved), "--visited", str(tmp_path / "visited.txt")]
    return base, saved, fake


def test_harvest_prints_yaml_with_the_numbers_as_comments(harvest_cli, capsys):
    base, saved, _ = harvest_cli
    before = saved.read_text(encoding="utf-8")
    assert main([*base, "--count", "5"]) == 0
    out = capsys.readouterr().out
    # 標準出力は、そのまま YAML として読める。
    assert [m["subject"] for m in yaml.safe_load(out)["mechanisms"]] == ["改札"]
    assert "# 記事 2 / 文 2 / 候補 1" in out
    assert "落ちた理由" in out
    # **保存先には書かない。** 候補は読んで選ぶまで生成に使われない。
    assert saved.read_text(encoding="utf-8") == before


def test_harvest_writes_the_candidates_to_a_file(harvest_cli, tmp_path, capsys):
    base, _, _ = harvest_cli
    out_file = tmp_path / "candidates.yaml"
    assert main([*base, "--count", "5", "--out", str(out_file)]) == 0
    assert len(yaml.safe_load(out_file.read_text(encoding="utf-8"))["mechanisms"]) == 1
    assert "記事 2 / 文 2 / 候補 1" in capsys.readouterr().out


def test_harvest_refuses_a_count_below_one(harvest_cli, capsys):
    base, _, fake = harvest_cli
    assert main([*base, "--count", "0"]) == 1
    assert "1 以上" in capsys.readouterr().err
    assert fake.calls == []


def test_harvest_says_so_when_nothing_could_be_read(harvest_cli, capsys):
    """読める記事が残っていないのに 0 を返すと、候補が無かったように見える。"""
    base, _, _ = harvest_cli
    assert main([*base, "--count", "5"]) == 0
    capsys.readouterr()
    assert main([*base, "--count", "5"]) == 1
    assert "1 つも読めませんでした" in capsys.readouterr().err


def test_harvest_refuses_a_saved_file_without_articles(harvest_cli, tmp_path, capsys):
    base, saved, _ = harvest_cli
    saved.write_text("mechanisms: []\n", encoding="utf-8")
    assert main([*base]) == 1
    assert "起点" in capsys.readouterr().err


def test_the_default_run_does_not_touch_the_generation_pool(harvest_cli):
    """候補を採ったあとも、生成が引く一文は保存先のものだけ。"""
    from ideaman.engine.lexicon import load_mechanisms

    base, _, _ = harvest_cli
    before = load_mechanisms()
    assert main([*base, "--count", "5"]) == 0
    assert load_mechanisms() == before
