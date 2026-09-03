"""許可された場所を読んで、材料を自分で集める。

**手で材料を登録する限り、このシステムは使われない。** 登録は面倒で、
面倒なことは放置され、放置されたテーマからは汎用の警句しか出てこない。

だから読む。ユーザーが許可したディレクトリを走査し、そこで実際に使われて
いる語を材料にする。抽出は完全にオフライン——LLM も API キーも要らない。

考え方は素朴な TF-IDF に近い:

- **たくさん出てくる語**はその人の関心事
- ただし**どのプロジェクトにも出てくる語**（`data`, `value`, `テスト`）は
  材料にならない。止め語で落とす
- **複数のファイルに散らばっている語**を優先する。1ファイルにしか出て
  こない語は、その場限りの変数名であることが多い
- **散文の語を、識別子より優先する。** 識別子は言語の文法と慣習に縛られて
  付く名前（`conn`, `execute`, `get_store`）だが、コメントや README の語は
  その人が自由に選んだ語で、何を考えているかをそのまま表す。実測でも
  `store` / `conn` より「連鎖」「休眠」「放浪」のほうが遥かに良い材料だった

集めた語は「候補」であって、確定ではない。呼び出し側が見せて選ばせる。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

#: 読むファイルの拡張子。バイナリと巨大なデータファイルは見ない。
READABLE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs", ".java",
        ".kt", ".swift", ".c", ".h", ".cpp", ".cs", ".php", ".sh",
        ".md", ".txt", ".rst", ".org",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql", ".html", ".css",
    }
)

#: 入らないディレクトリ。生成物や他人のコードから材料を採っても意味がない。
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        "env", "dist", "build", "target", ".next", ".nuxt", "vendor",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage", ".idea",
        ".vscode", "migrations", "locale", "site-packages",
    }
)

#: 1ファイルの読み込み上限（バイト）。生成された巨大ファイルを避ける。
MAX_FILE_BYTES = 200_000

#: 走査するファイル数の上限。大きなリポジトリで止まらなくなるのを防ぐ。
MAX_FILES = 2_000

#: 散文として読むファイル。ここに出てくる語は自由に選ばれた語。
PROSE_SUFFIXES = frozenset({".md", ".txt", ".rst", ".org"})

#: 散文由来の語に掛ける下駄。識別子より優先させるため。
PROSE_BONUS = 3.0

#: 漢字・カタカナの連なり。ひらがなだけの語は助詞混じりになるので採らない。
_JA = re.compile(r"[一-龥々〆ヵヶ]{2,10}|[ァ-ヴー]{3,12}")

#: 識別子。camelCase / snake_case はそのまま材料にせず、単語に割る。
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,40}")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]+|[a-z0-9]+")

#: どのプロジェクトにも出てくる語。材料としての情報量がない。
STOPWORDS = frozenset(
    """
    self cls this true false null none void int str bool float list dict set
    tuple map array object string number boolean type types kind value values
    key keys item items data result results return returns args kwargs params
    param arg option options config configs setting settings env var let const
    function func def class new delete import export from require module
    default async await yield lambda print log logger debug info warn warning
    error errors exception raise throw catch try except finally assert test
    tests testing spec mock fixture setup teardown init main run start stop
    get set add remove update create delete find search filter sort list all
    any not and or if else elif for while break continue pass with as in is
    name names id ids index idx count total sum max min len size length
    path paths file files dir directory folder root base src lib util utils
    helper helpers common core api http url uri request response req res
    server client app application service handler view model schema table
    column row query db database sql select insert where order group limit
    text content body html css div span class style script link href
    time date datetime now today year month day hour minute second timestamp
    version build release deploy docker node npm pip python javascript
    todo fixme note warning copyright license readme
    テスト データ ファイル ディレクトリ リスト エラー コード システム
    サーバー クライアント メソッド クラス オブジェクト パラメータ
    デフォルト オプション インストール バージョン アップデート
    """.split()
)

#: 日本語のコメントに必ず出る機能語。中身のある語のように見えて、材料に
#: すると「場合の待ち時間を反転させる」のような無意味な文になる。
#: （ひらがなだけの語は抽出側で弾いているのでここには要らない）
STOPWORDS = STOPWORDS | frozenset(
    """
    場合 処理 実行 必要 使用 指定 取得 設定 引数 戻り値 以下 以上 未満
    対象 方法 内容 情報 確認 追加 削除 変更 修正 作成 生成 利用 出力
    入力 参照 定義 説明 注意 一覧 全体 自分 今回 現在 上記 時間 数値
    文字 文字列 関数 変数 定数 属性 要素 単純 簡単 複雑 以外 同様 一部
    """.split()
)


@dataclass(frozen=True)
class Candidate:
    """材料の候補。"""

    term: str
    #: 出現回数の合計。
    count: int
    #: いくつのファイルに出てきたか。散らばっているほど本物らしい。
    files: int
    score: float

    def __str__(self) -> str:  # デバッグと CLI 表示用
        return f"{self.term}（{self.count}回 / {self.files}ファイル）"


def _is_japanese(word: str) -> bool:
    return bool(_JA.fullmatch(word))


def _plural_of(term: str, counts: Counter[str]) -> bool:
    """`terms` のように、単数形も出てくる複数形かどうか。

    両方を材料に入れても、同じものを二度指しているだけ。
    """
    if not term.endswith("s") or len(term) < 4:
        return False
    return term[:-1] in counts or (term.endswith("es") and term[:-2] in counts)


def _words(text: str) -> list[str]:
    """1つの文書から、材料になりうる語を取り出す。"""
    found = _JA.findall(text)
    for ident in _IDENT.findall(text):
        parts = _CAMEL.findall(ident)
        # 分割できた語を採る。分割できない1語ものはそのまま。
        found.extend(p.lower() for p in parts if len(p) >= 3)
    return found


def _readable_files(root: Path) -> list[Path]:
    """走査対象のファイル。隠しディレクトリと生成物は避ける。"""
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= MAX_FILES:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.parts):
            continue
        if path.suffix.lower() not in READABLE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def harvest(root: Path, limit: int = 25, min_files: int = 2) -> list[Candidate]:
    """許可された場所を読んで、材料の候補を返す。

    Args:
        root: 読むディレクトリ。**ここより上は決して見に行かない。**
        limit: 返す候補の数。
        min_files: 何ファイル以上に出てくる語を採るか。1 にすると、その場
            限りの変数名まで拾ってしまう。ただし小さなプロジェクトでは
            候補が空になるので、空になったら自動で 1 に緩める。

    Returns:
        点数の高い順。候補が無ければ空リスト。

    Raises:
        NotADirectoryError: `root` がディレクトリでない場合。
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} はディレクトリではありません")

    counts: Counter[str] = Counter()
    prose_counts: Counter[str] = Counter()
    doc_freq: defaultdict[str, int] = defaultdict(int)
    files = _readable_files(root)

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        words = _words(text)
        counts.update(words)
        # コメントも文字列も散文なので、コードファイル中の日本語も散文扱い。
        # 英語の識別子だけは、散文ファイルに出たときに限って散文とみなす。
        if path.suffix.lower() in PROSE_SUFFIXES:
            prose_counts.update(words)
        else:
            prose_counts.update(w for w in words if _is_japanese(w))
        for word in set(words):
            doc_freq[word] += 1

    if not counts:
        return []

    total_files = max(len(files), 1)

    def scored(threshold: int) -> list[Candidate]:
        out = []
        for term, count in counts.items():
            if term in STOPWORDS or len(term) < 2:
                continue
            if term.isdigit():
                continue
            if _plural_of(term, counts):
                # term と terms の両方は要らない。単数側に寄せる。
                continue
            spread = doc_freq[term]
            if spread < threshold:
                continue
            # 散らばり（idf の逆）× 出現量。全ファイルに出る語は伸びない。
            rarity = math.log(total_files / spread + 1.0)
            base = math.log(count + 1.0) * rarity * math.log(spread + 1.0)
            prose_share = prose_counts[term] / count if count else 0.0
            out.append(
                Candidate(
                    term=term,
                    count=count,
                    files=spread,
                    score=base * (1.0 + (PROSE_BONUS - 1.0) * prose_share),
                )
            )
        out.sort(key=lambda c: (-c.score, c.term))
        return out[:limit]

    found = scored(min_files)
    return found or scored(1)
