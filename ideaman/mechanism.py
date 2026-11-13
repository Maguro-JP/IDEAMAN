"""外の世界から「仕組みの一文」を採る。

`concepts` が裸の語（改札、回覧板、検針）だと、出てくるのは

    改札のやり方を通知に持ち込む。見るのは待ち時間。

で止まる。**どのやり方かは書いていない**ので、読み手が全部補うことになる。
持ってくるべきなのは語ではなく、何をどうしているかが名指しされた一文:

    改札は、通れなかった人を後ろに溜めず、脇へ出して個別に捌く。
    通知で同じことをするなら、何が要る?

ここはその一文を文章から抜く部分。**LLM は使わない**（キー無しで動くのが
Phase 1 からの方針）。手掛かりは意味ではなく**文の形**に置く:

- 構造の印 —「〜ではなく〜」「〜せずに〜」「〜ことで〜」「まず〜次に〜」。
  仕組みの説明は、何かと引き比べるか、手段と目的を結ぶ形になりやすい
- 操作の動詞 —「捌く」「防ぐ」「戻す」「分ける」。やり方を持っている文は
  必ず何かを操作している

**片方だけでは足りない。** 「〜ことで知られている」は構造の印を持つが操作が
無く、「ゴミを出す」は操作だけで仕組みを説明していない。両方を要求する。

落とすものも形で決まる:

- 定義文（「Xとは〜である」）— 分類は仕組みではない
- 指示語で始まる文（「これは〜」「その際〜」）— 前の文が無いと読めない
- 年号や数字の羅列 — 沿革であって仕組みではない
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 仕組みの一文として短すぎる／長すぎる境界（文字数）。
#: 短い文は「〜を行う」で終わって中身が無く、長い文はテンプレートに
#: 差し込むと読めない。**手で書いた良い例は 26〜31 字**だったので、
#: そこを基準に取る（90 字まで許すと、節が4つある文が通ってしまった）。
MIN_LEN = 20
MAX_LEN = 60

#: 読点の数の上限。節が多い文は、長さが収まっていても読めない。
#: 長さより効く——「〜し、さらに〜して、〜ている」は字数が足りていても
#: 一息で読めない。
MAX_CLAUSES = 2

#: 構造の印。仕組みの説明は、引き比べるか、手段と目的を結ぶ。
_STRUCTURE = re.compile(
    r"ではなく"
    r"|ではなしに"
    r"|せずに"
    r"|ずに済"
    r"|ことで"
    r"|ことにより"
    r"|ようにして"
    r"|代わりに"
    r"|一方で"
    r"|まず.{0,40}(?:次に|その後|そのあと|最後に)"
    r"|(?:ため|よう)に、"
)

#: 操作の動詞。**やり方を持っている文は必ず何かを操作している。**
#: 「ある」「なる」「言う」のような状態や言及の動詞は入れない。
_OPERATION = re.compile(
    r"捌(?:く|き|いて)"
    r"|防(?:ぐ|ぎ|いで)"
    r"|戻(?:す|し|して)"
    r"|分(?:ける|け|けて)"
    r"|減ら(?:す|し|して)"
    r"|増や(?:す|し|して)"
    r"|早め(?:る|て)"
    r"|遅ら(?:せる|せ|せて)"
    r"|選(?:ぶ|び|んで)"
    r"|並べ(?:る|て)"
    r"|入れ替え(?:る|て)"
    r"|省(?:く|き|いて)"
    r"|溜め(?:る|て)"
    r"|流(?:す|し|して)"
    r"|配(?:る|り|って)"
    r"|束ね(?:る|て)"
    r"|切り替え(?:る|て)"
    r"|止め(?:る|て)"
    r"|保(?:つ|ち|って)"
    r"|回(?:す|し|して)"
    r"|置き換え(?:る|て)"
    r"|待(?:つ|ち|って)"
    r"|知らせ(?:る|て)"
    r"|取り除(?:く|き|いて)"
    r"|割り当て(?:る|て)"
    r"|打ち切(?:る|り|って)"
    r"|使い回(?:す|し|して)"
)

#: 定義文。分類を述べているだけで、やり方が無い。
_DEFINITION = re.compile(r"(?:とは|、)[^。]{0,60}(?:である|です|のことで|の一種|を指す)")

#: 前の文が無いと読めない出だし。単独で出すと意味が通らない。
_ANAPHORA = re.compile(
    r"^(?:これ|それ|その|この|こう|そう|また|さらに"
    r"|同様|一方|他方|そのため|したがって|なお)"
)

#: 沿革。年月日や数字が多い文は仕組みではない。
_DIGITS = re.compile(r"[0-9０-９]")
MAX_DIGITS = 6

#: 出典の脚注。本文に混ざると文末が壊れる。
_FOOTNOTE = re.compile(r"\[\s*\d+\s*\]|\[(?:要出典|誰|いつ)[^\]]*\]")

#: 括弧の中の読みや原語。仕組みの説明には要らないので落とす。
_PARENS = re.compile(r"[（(][^（()）]{0,40}[)）]")


@dataclass(frozen=True)
class Mechanism:
    """外の世界の、名指しできる仕組み。

    `subject` が「何の」、`how` が「どうしているか」。出典を持たせるのは、
    刺さったときにユーザーが自分で辿れるようにするため。
    """

    #: 何の仕組みか（記事の題など）。
    subject: str
    #: 何をどうしているか。そのまま文中に差し込める一文。
    how: str
    #: どこから採ったか。URL でも書名でもよい。
    source: str = ""
    #: 分野タグ。遠さの計算に使う。
    domains: tuple[str, ...] = field(default=())


def _clean(sentence: str) -> str:
    """文から、仕組みの説明に要らないものを落とす。"""
    sentence = _FOOTNOTE.sub("", sentence)
    sentence = _PARENS.sub("", sentence)
    return re.sub(r"\s+", "", sentence).strip("。 　")


#: 散文ではない行。見出し・箇条書き・表・コードは仕組みの説明ではない。
_NOT_PROSE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\||>|```|~~~|\d+\.\s)")


def split_sentences(text: str) -> list[str]:
    """文に割る。

    **行で先に割ってはいけない。** Markdown も Wikipedia の本文も1文が複数行に
    折り返されるので、行で割ると文が途中で切れる。実際、手元の散文に当てたとき
    唯一通った一文が「〜弱かった——「給水塔にあって」という断片だった。

    空行を段落の区切りとして、段落の中の改行は繋ぐ。そのうえで「。」で割る。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [
            line.strip()
            for line in block.split("\n")
            if line.strip() and not _NOT_PROSE.match(line)
        ]
        if not lines:
            continue
        joined = "".join(lines)
        for piece in re.split(r"(?<=。)", joined):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def looks_like_mechanism(sentence: str) -> bool:
    """その一文が「やり方を名指ししている」か。

    **意味は見ない。形だけで決める。** 構造の印と操作の動詞の両方を要求し、
    定義・指示語・沿革を落とす。取れ高は下がるが、混ざるほうが困る——
    ここを通った一文は、そのままユーザーの目に入る。
    """
    body = _clean(sentence)
    if not (MIN_LEN <= len(body) <= MAX_LEN):
        return False
    if body.count("、") > MAX_CLAUSES:
        return False
    if _ANAPHORA.match(body):
        return False
    if len(_DIGITS.findall(body)) > MAX_DIGITS:
        return False
    if _DEFINITION.search(body):
        return False
    return bool(_STRUCTURE.search(body) and _OPERATION.search(body))


def extract(
    subject: str,
    text: str,
    *,
    source: str = "",
    domains: tuple[str, ...] = (),
    limit: int = 3,
) -> list[Mechanism]:
    """一つの文章から、仕組みの一文を取り出す。

    同じ記事から何本も採ると偏るので、既定では上位 `limit` 本だけ。
    順番は文章に出てきた順（前のほうが概説で、仕組みの核に近いことが多い）。
    """
    found: list[Mechanism] = []
    seen: set[str] = set()
    for sentence in split_sentences(text):
        # **文として閉じていないものは出さない。** 段落の末尾や箇条書きの
        # 途中は句点で終わらない断片になり、そのまま差し込むと途切れる。
        if not sentence.rstrip().endswith("。"):
            continue
        if not looks_like_mechanism(sentence):
            continue
        body = _clean(sentence)
        if body in seen:
            continue
        seen.add(body)
        found.append(
            Mechanism(subject=subject, how=body, source=source, domains=domains)
        )
        if len(found) >= limit:
            break
    return found
