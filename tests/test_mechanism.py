"""仕組みの一文の抽出。

**ここを通った一文は、そのままユーザーの目に入る。** 取れ高より混ざらない
ことを優先する。判定は意味ではなく文の形でやっているので、形ごとに
通る／落ちるを固定しておく。
"""

import pytest

from ideaman.mechanism import (
    MAX_CLAUSES,
    MAX_LEN,
    extract,
    looks_like_mechanism,
    split_sentences,
)

#: 通ってほしい文。構造の印と操作の動詞の両方を持つ。
GOOD = [
    "改札では、通れなかった人を後ろに溜めるのではなく、脇へ出して個別に捌く",
    "空港では取り違えを防ぐのではなく、取り違えたあとに戻す経路を短くしている",
    "トリアージでは全員を順に診るのではなく、待てる人を先に選んで後回しにする",
    "返却期限を厳しくするのではなく、返しやすい場所を増やすことで回収を早める",
    "まず全体を粗く分けて、次に細かいほうだけを並べ替える",
    "混雑を減らすために、改札の台数ではなく通路の幅を切り替える",
]

#: 落ちてほしい文と、落ちる理由。
BAD = [
    ("トリアージとは、負傷者を重症度に応じて分類することである", "定義文"),
    ("これにより、待ち時間を減らすことができる", "指示語で始まる"),
    ("その後、改札の台数を増やして混雑を減らした", "指示語で始まる"),
    ("1987年に導入され、1995年に全面的に置き換えられた", "沿革（数字が多い）"),
    ("駅員が切符を確認していた", "構造の印が無い"),
    ("この方式はヨーロッパで広く知られていることで有名である", "操作の動詞が無い"),
    ("短い", "短すぎる"),
]


@pytest.mark.parametrize("sentence", GOOD)
def test_mechanism_sentences_pass(sentence):
    assert looks_like_mechanism(sentence), sentence


@pytest.mark.parametrize("sentence,why", BAD)
def test_non_mechanism_sentences_are_dropped(sentence, why):
    assert not looks_like_mechanism(sentence), f"{why}: {sentence}"


def test_footnotes_and_readings_are_stripped():
    """脚注と読みの括弧は落ちること。文中に差し込むので混ざると壊れる。"""
    raw = "改札（かいさつ）では、人を溜めるのではなく脇へ出して捌く[1]。"
    found = extract("改札", raw)
    assert found, raw
    assert "[1]" not in found[0].how
    assert "（かいさつ）" not in found[0].how


def test_a_sentence_with_too_many_clauses_is_refused():
    """印を持っていても節が多い文は落とすこと。

    テンプレートに差し込んだ瞬間に読めなくなる。長さより節の数が効く——
    この文は 67 字で、90 字まで許していたときは通ってしまった。
    """
    many = (
        "混雑を減らすために、改札の台数を増やすのではなく通路の幅を切り替え、"
        "さらに案内の順番を入れ替えて、利用者が迷わないように配り直している"
    )
    assert many.count("、") > MAX_CLAUSES
    assert not looks_like_mechanism(many)


def test_a_very_long_sentence_is_refused():
    """節が少なくても長い文は落とすこと。"""
    long_one = "待っている人を順に呼ぶのではなく" + "とても" * 15 + "長い経路で戻す"
    assert len(long_one) > MAX_LEN
    assert not looks_like_mechanism(long_one)


def test_extract_keeps_the_order_and_stops_at_the_limit():
    text = (
        "図書館の返却について。"
        "返却期限を厳しくするのではなく、返しやすい場所を増やすことで回収を早める。"
        "これは各地で行われている。"
        "督促を出すのではなく、棚の近くに箱を置くことで手数を省く。"
        "まず粗く分けて、次に細かいほうだけを並べ替える。"
    )
    found = extract("図書館の返却", text, source="https://example.invalid/x", limit=2)
    assert len(found) == 2
    assert "回収を早める" in found[0].how
    assert "手数を省く" in found[1].how
    assert found[0].subject == "図書館の返却"
    assert found[0].source == "https://example.invalid/x"


def test_duplicates_within_one_document_are_dropped():
    one = "期限を厳しくするのではなく、返しやすい場所を増やすことで回収を早める。"
    found = extract("x", one * 3)
    assert len(found) == 1


def test_split_sentences_does_not_lose_the_last_sentence():
    """句点で終わらない文章でも最後の一文を落とさないこと。"""
    pieces = split_sentences("一つ目。二つ目")
    assert pieces == ["一つ目。", "二つ目"]
