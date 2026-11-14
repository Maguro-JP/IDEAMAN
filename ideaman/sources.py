"""外の世界の文章を取ってくる。いまは日本語版ウィキペディアだけ。

`mechanism.py` は渡された文章から一文を抜くだけで、文章をどこから持って
くるかを知らない。前回の採取は使い捨てのスクリプトで 1 回やっただけで、
**繰り返す入口が無かった**。ここがその入口。

題の集め方は「すでに採れた記事のリンク先を辿る」。`mechanisms.yaml` の
`source` にある記事は、仕組みを説明する文を実際に持っていた記事なので、
そこから張られているリンクの先も、無作為に選ぶより同じ種類の記事に
当たりやすい——**これは見込みで、測っていない**。採れた数は
`docs/mechanism-wikipedia.md` に残す。

守ること:

- **依存を足さない。** 取得は標準ライブラリの `urllib` だけ
- **間隔を 1 秒より詰めない。** 相手は共有の資源で、急ぐ理由が無い
- **見た題は二度読まない。** `data/mechanism-visited.txt` に 1 行 1 題で残す。
  同じ記事を読み直しても、同じ候補をもう一度読まされるだけになる
- **候補は候補のまま出す。** 抽出器を通っても 2 割は仕組みではないので、
  ここからは `mechanisms.yaml` に書かない。人が読んで選んでから足す

取得の関数（URL を受け取って本文のバイト列を返す）は差し替えられる。
テストは偽の応答で動かし、ネットワークに出ない。
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import mechanism

API = "https://ja.wikipedia.org/w/api.php"
ARTICLE_PREFIX = "https://ja.wikipedia.org/wiki/"

#: 名乗る名前。連絡先や利用者の情報は入れない。
USER_AGENT = "ideaman-harvest/1.0"

#: 取得の間隔の下限（秒）。これより短くは設定できない。
MIN_INTERVAL = 1.0

#: 「多すぎる」（429）と言われたとき、同じ取得をやり直す回数。
MAX_RETRIES = 3

#: 429 のあとに待つ秒数の上限と、相手が秒数を言わなかったときの値。
MAX_BACKOFF = 300.0
DEFAULT_BACKOFF = 60.0

#: 429 を受けるたびに間隔を何倍にするかと、その上限（秒）。
#: **1 秒あけていても止められる**（実際に 3 回目の取得で 429 が返った）。
#: 言われた秒数だけ待って元の間隔に戻すと、すぐまた止められるので、
#: その回の採取が終わるまで間隔を広げたままにする。
SLOWDOWN = 2.0
MAX_INTERVAL = 30.0

#: 1 つの記事から辿るリンクの上限。1 記事のリンク先ばかり読むと、
#: 候補がその記事の分野に偏る。
PER_SEED = 5

#: リンクの一覧を何ページまで続けて取るか。1 ページ 500 件。
MAX_LINK_PAGES = 3

#: URL を受け取り、応答の本文を返す関数。
Fetch = Callable[[str], bytes]

#: 読んでも仕組みの文が出ない題。年・日付・一覧・曖昧さ回避は、沿革か
#: 項目の羅列で、本文に「何をどうしているか」が無い。
_NOT_WORTH_READING = re.compile(
    r"^[0-9０-９]+(?:年|月|世紀|年代)|[0-9０-９]+月[0-9０-９]+日|一覧|の年表$|\(曖昧さ回避\)$"
)


def fetch_url(url: str, timeout: float = 30.0) -> bytes:
    """URL の中身を取る。`Wikipedia` の既定の取得関数。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def article_url(title: str) -> str:
    """記事の題から URL を作る。`mechanisms.yaml` の `source` と同じ形。"""
    return ARTICLE_PREFIX + title.replace(" ", "_")


def title_from_url(url: str) -> str | None:
    """`source` の URL から記事の題を取り出す。ウィキペディアでなければ None。"""
    if not url.startswith(ARTICLE_PREFIX):
        return None
    title = urllib.parse.unquote(url[len(ARTICLE_PREFIX) :]).replace("_", " ").strip()
    return title or None


def worth_reading(title: str) -> bool:
    """題を見て、読む価値があるか。年・日付・一覧は読まない。"""
    return not _NOT_WORTH_READING.search(title)


@dataclass(frozen=True)
class Article:
    """取ってきた記事。`title` は転送を辿ったあとの題。"""

    title: str
    text: str


class Wikipedia:
    """日本語版ウィキペディアの API。**どの取得も、前の取得から間隔を空ける。**

    `fetch` `sleep` `clock` を差し替えると、ネットワークにも時計にも
    触らずに動く。
    """

    def __init__(
        self,
        fetch: Fetch | None = None,
        *,
        interval: float = MIN_INTERVAL,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if interval < MIN_INTERVAL:
            raise ValueError(f"間隔は {MIN_INTERVAL} 秒以上にします（{interval} は短すぎる）")
        self._fetch = fetch or fetch_url
        self._interval = interval
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._last: float | None = None

    @property
    def interval(self) -> float:
        """いまの間隔（秒）。429 を受けると広がる。"""
        return self._interval

    def _get(self, **params: str) -> dict:
        query = urllib.parse.urlencode({"action": "query", "format": "json", **params})
        for attempt in range(MAX_RETRIES + 1):
            if self._last is not None:
                wait = self._interval - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            try:
                body = self._fetch(f"{API}?{query}")
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == MAX_RETRIES:
                    raise
                # 相手が言った秒数だけ待ち、以後の間隔も広げる。
                self._interval = min(self._interval * SLOWDOWN, MAX_INTERVAL)
                backoff = _retry_after(exc)
            else:
                return json.loads(body)
            finally:
                # 失敗した取得も 1 回と数える。失敗のあとに間を空けずに
                # 叩き直すのが、一番相手に迷惑な形。
                self._last = self._clock()
            self._sleep(backoff)
        raise AssertionError("ここには来ない")  # pragma: no cover

    def links(self, title: str) -> list[str]:
        """その記事から張られている、記事（名前空間 0）への リンクの題。"""
        found: list[str] = []
        params = {"prop": "links", "plnamespace": "0", "pllimit": "max", "titles": title}
        for _ in range(MAX_LINK_PAGES):
            data = self._get(**params)
            for page in (data.get("query", {}).get("pages") or {}).values():
                found.extend(link["title"] for link in page.get("links") or [])
            more = data.get("continue")
            if not more:
                break
            params = {**params, **more}
        return found

    def article(self, title: str) -> Article | None:
        """記事の本文（プレーンテキスト）。無い記事なら None。"""
        data = self._get(prop="extracts", explaintext="1", redirects="1", titles=title)
        for page in (data.get("query", {}).get("pages") or {}).values():
            text = page.get("extract")
            if text:
                return Article(title=page.get("title") or title, text=text)
        return None


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """429 の応答が言う「何秒待て」。読めなければ既定の秒数。"""
    try:
        seconds = float(exc.headers.get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_BACKOFF
    return min(max(seconds, 0.0), MAX_BACKOFF)


def read_visited(path: Path) -> set[str]:
    """これまでに読んだ題。ファイルが無ければ空。"""
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def remember(path: Path, title: str) -> None:
    """読んだ題を 1 行足す。**1 件ごとに書く**ので、途中で止めても残る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(title + "\n")


@dataclass
class Harvest:
    """1 回の採取の結果。"""

    #: 本文を読めた記事の数。
    articles: int = 0
    #: 割った文の数。
    sentences: int = 0
    #: 抽出器を通り、まだ保存されていない一文。
    candidates: list[mechanism.Mechanism] = field(default_factory=list)
    #: 抽出器を通ったが、すでに保存されていた一文の数。
    already_saved: int = 0
    #: 落ちた理由ごとの文の数。
    reasons: Counter[str] = field(default_factory=Counter)
    #: 取れなかった題と、その理由。
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: 辿れるリンクが尽きて、頼まれた数に届かなかった。
    exhausted: bool = False

    def report(self, top: int = 5) -> list[str]:
        lines = [
            f"記事 {self.articles} / 文 {self.sentences} / 候補 {len(self.candidates)}"
            f"（保存済みで除いた一文 {self.already_saved}）",
        ]
        if self.failed:
            lines.append(f"取れなかった記事 {len(self.failed)}: {self.failed[0][0]} ほか")
        if self.exhausted:
            lines.append("辿れるリンクが尽きたので、頼まれた数より少なく終わりました")
        lines.append(f"落ちた理由（上位 {top}）:")
        lines.extend(f"  {count:6d}  {reason}" for reason, count in self.reasons.most_common(top))
        lines.append("候補は生成に使われません。読んで選んだ文だけを mechanisms.yaml に足します。")
        return lines


def collect(
    count: int,
    *,
    wiki: Wikipedia,
    saved: Iterable[dict],
    visited_path: Path,
    rng: random.Random | None = None,
    per_seed: int = PER_SEED,
    limit: int = 50,
    progress: Callable[[str], None] | None = None,
) -> Harvest:
    """保存済みの記事のリンク先を `count` 記事読み、新しい候補を集める。

    `saved` は `mechanisms.yaml` の項目。`source` が辿る起点になり、`how` が
    「もう持っている一文」になる。**ここからは何も保存しない**（見た題を除く）。
    """
    if count < 1:
        raise ValueError("読む記事の数は 1 以上にします")
    rng = rng or random.Random()
    saved = list(saved)
    known_hows = {item["how"] for item in saved}
    seeds = sorted({t for item in saved if (t := title_from_url(item.get("source", "")))})
    # 起点の記事は前に読んでいる。リンク先として出てきても読み直さない。
    seen = read_visited(visited_path) | set(seeds)
    rng.shuffle(seeds)

    result = Harvest()
    for seed in seeds:
        if result.articles >= count:
            break
        try:
            links = wiki.links(seed)
        except (OSError, ValueError) as exc:
            result.failed.append((seed, f"リンクが取れない: {exc}"))
            continue
        fresh = [t for t in dict.fromkeys(links) if t not in seen and worth_reading(t)]
        rng.shuffle(fresh)
        for title in fresh[:per_seed]:
            if result.articles >= count:
                break
            try:
                article = wiki.article(title)
            except (OSError, ValueError) as exc:
                # 取れなかった題は「見た」に入れない。次の回にもう一度試せる。
                result.failed.append((title, str(exc)))
                continue
            seen.add(title)
            remember(visited_path, title)
            if article is None:
                continue
            if article.title != title:
                # 転送の先。別の題から同じ記事に着くことがある。
                if article.title in seen:
                    continue
                seen.add(article.title)
                remember(visited_path, article.title)
            _read(article, result, known_hows, limit)
            if progress:
                found = len(result.candidates)
                progress(f"{result.articles}/{count} {article.title}: 候補 {found}")
    else:
        result.exhausted = result.articles < count
    return result


def _read(article: Article, result: Harvest, known_hows: set[str], limit: int) -> None:
    """1 記事を文に割り、通った一文のうち新しいものを候補に足す。"""
    result.articles += 1
    pieces = mechanism.split_sentences(article.text)
    result.sentences += len(pieces)
    for piece in pieces:
        reason = mechanism.reject_reason(piece)
        if reason:
            result.reasons[reason] += 1
    source = article_url(article.title)
    for item in mechanism.extract(article.title, article.text, source=source, limit=limit):
        if item.how in known_hows:
            result.already_saved += 1
            continue
        known_hows.add(item.how)
        result.candidates.append(item)


def to_yaml(candidates: Iterable[mechanism.Mechanism]) -> str:
    """候補を `mechanisms.yaml` と同じ形の YAML にする。

    `aspect` と `domains` は空で出す。**どちらも読んで付ける判定**なので、
    機械では埋めない。選んだ項目だけを、埋めてから `mechanisms.yaml` に移す。
    """
    lines = ["mechanisms:"]
    for item in candidates:
        # JSON の文字列は、そのまま YAML の二重引用符の文字列として読める。
        lines.append(f"  - subject: {json.dumps(item.subject, ensure_ascii=False)}")
        lines.append(f"    how: {json.dumps(item.how, ensure_ascii=False)}")
        lines.append(f"    source: {json.dumps(item.source, ensure_ascii=False)}")
        lines.append("    aspect:")
        lines.append("    domains: []")
    if len(lines) == 1:
        lines = ["mechanisms: []"]
    return "\n".join(lines) + "\n"
