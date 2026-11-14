"""仕組みの一文の抽出。

**ここを通った一文は、そのままユーザーの目に入る。** 取れ高より混ざらない
ことを優先する。判定は意味ではなく文の形でやっているので、形ごとに
通る／落ちるを固定しておく。
"""

from pathlib import Path

import pytest
import yaml

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

#: Wikipedia の本文で落ちていた本物。**手元の文章から作った印と動詞は、
#: 機械や道具の説明を持っていなかった**（「回転させる」「押す」「調節する」）。
#: ここを通すために足した印と動詞を固定する。
GOOD_FROM_ARTICLES = [
    # 使役の「させる」
    "脱穀ドラムを回転させることによりコンケーブの上で作物を脱穀し、穂から穀粒を分離する",
    "唇と歌口の鋭角部との距離を変化させることで、音高を変化させる",
    # 手や機械の動き
    "油圧壁は一定圧で強制開放されるか人為的に操作ボタンを押すことにより移動する",
    "リード管の調律はリードの振動長を調節することで行う",
    "成駒は元の駒を裏返して配置することで表示される",
    # 「ことによって」「目的で、」
    "糸巻きを回すことによって弦の張力を変化させ、開放弦の音高を変える",
    "血液の体外への循環中に血液が凝固することを防ぐ目的で、抗凝固薬を持続注入する",
    # 操作の動詞に直に付く「ため」
    "車両がショートカットして走行することを防ぐためわずかな段差を設ける",
    "安定性を保つために地中にアンカーを設置し、水圧に耐える構造を採っている",
    # 「に」の無い「よう、」
    "屋外で竜巻に遭遇した場合、飛来物に当たらないよう、周囲を囲まれた安全な場所に身を隠す",
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


@pytest.mark.parametrize("sentence", GOOD_FROM_ARTICLES)
def test_mechanism_sentences_from_articles_pass(sentence):
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


def test_article_headings_do_not_stick_to_the_sentence():
    """Wikipedia の見出しが文の頭に貼り付かないこと。

    本文は「== 構造 ==」の次の行からすぐ始まる（空行が無い）。繋いでしまうと
    「===血管因子===末梢の血管が…」がそのまま通って目に入る。
    """
    text = (
        "前の段落。\n\n\n=== 血管因子 ===\n"
        "末梢の血管が収縮したり、周囲へ漏れた血液が血管を圧迫することで出血を止める。"
    )
    found = extract("血液凝固", text)
    assert [item.how for item in found] == [
        "末梢の血管が収縮したり、周囲へ漏れた血液が血管を圧迫することで出血を止める"
    ]


def test_an_item_name_does_not_stick_to_its_description():
    """項目名の行が、次の行の説明に貼り付かないこと。

    「熱放射を防ぐ」という項目名のあとに説明の行が来る。繋ぐと
    「熱放射を防ぐ内容物のエネルギーが…」になって読めない。
    """
    text = (
        "熱放射を防ぐ\n"
        "内容物のエネルギーが電磁波の形をとった放射として逃げるのを防ぐため、"
        "鏡面による反射を利用している。"
    )
    found = extract("魔法瓶", text)
    assert len(found) == 1
    assert found[0].how.startswith("内容物のエネルギーが")


def test_wrapped_prose_is_still_joined():
    """折り返された散文は、今までどおり繋ぐこと。

    項目名を切り離す規則が、Markdown の折り返し（どの行もほぼ同じ幅）まで
    切ってしまうと、文が途中で割れる。
    """
    text = (
        "返却期限を厳しくするのではなく、返しやすい場所を\n"
        "増やすことで回収を早める。\n"
    )
    assert split_sentences(text) == [
        "返却期限を厳しくするのではなく、返しやすい場所を増やすことで回収を早める。"
    ]


def test_split_sentences_does_not_lose_the_last_sentence():
    """句点で終わらない文章でも最後の一文を落とさないこと。"""
    pieces = split_sentences("一つ目。二つ目")
    assert pieces == ["一つ目。", "二つ目"]


#: 実際の文章に当てて通ってしまったもの。**形は満たしているが仕組みではない。**
#: 手で作った例では出なかった——文体の違う実文に当てて初めて出た。
REAL_FALSE_POSITIVES = [
    (
        "作業量を増やすことではなく、プロダクトを良くすること",
        "体言止め。述語が無いので「Xは、〜」に差し込むと壊れる",
    ),
    (
        "出したあとの直しが続いた判定の誤り:出す前の検査を手で選び、"
        "出す内容をユーザーに見せずに進めた",
        "ラベル付きの記録の断片。文のように見えて文ではない",
    ),
    # ここから下は日本語 Wikipedia の本文（133 記事）で通ってしまったもの。
    (
        "チェスにおいても相手が自分をチェックメイトできる戦力がない状態での"
        "時間切れは負けではなく引き分けとなる",
        "「引き分け」の「分け」を操作の動詞として拾っていた",
    ),
    (
        "初場所前の記者会見後みんなで話し合って、最も近い時期を選んでということで、"
        "夏場所から実施することに決めたものだ",
        "「ということで」は手段の「ことで」ではない。談話の引用",
    ),
    (
        "ハンドル・ティンパニでは、同様の作業をハンドルを回すことで行う",
        "「同様の作業」が前の文を指していて、何の作業かが無い",
    ),
    (
        "すなわち、血液の凝固を防ぐために、患者の静脈にドナーの動脈を"
        "外科的に接続する方法で患者を救った",
        "「すなわち」で始まる。前の文の言い換え",
    ),
    (
        "閘門扉が閉じられたときに、水密を保ち扉を支えるために、"
        "底から飛び出している図中cの部分を閘門閾と呼ぶ",
        "図を指している名付けの文",
    ),
    (
        "従来のフライト・ロックや階段形閘門を置き換えることで、"
        "かなりの時間節約となることが期待されている",
        "見込みで終わる。やり方ではなく、やり方への評価",
    ),
    (
        "魔法瓶とは、内びんと外びんの二重構造でその間を真空状態にすることにより"
        "熱の移動を防ぎ長時間保温・保冷できるようにした容器",
        "「Xとは、」で始まる定義。「である」が無くても定義",
    ),
    (
        "自分が打つことによって、相手が打った直前の局面に戻してはならない",
        "禁止で終わる。やり方ではなく規範",
    ),
    (
        "作物に用いても安全にする目的で、しばしば植物毒素を取り除くために"
        "二段階目の中温過程が必要になることがある",
        "「ことがある」で終わる。起こりうることを述べているだけ",
    ),
    (
        "極終盤では寄せる速度が勝負を分けるため、主導権を得ることが重要となる",
        "評価で終わる。「ため」は目的ではなく理由",
    ),
    (
        "ドイツで開発された方式は農作物など有機廃棄物を効果的に堆肥化させることが"
        "できるため、ニッチな需要を保っている",
        "理由の「ため」。操作の動詞に直に付いていない",
    ),
    (
        "献血センターや検査室では、汚染のリスクを減らすために多くの戦略が実施されている",
        "手段を指しているが名指ししていない",
    ),
    (
        "石岡大火-被害の拡大を防ぐため、最終的に破壊消火が行われた",
        "箇条書きの項目名が文の頭に貼り付いている",
    ),
    (
        "缶詰は、一般に水分の多い食品を金属缶に詰めて密封した上で微生物による"
        "腐敗・変敗を防ぐために加熱・殺菌した保存食",
        "漢字で終わる体言止め",
    ),
    (
        "エアポンプの一種）にて火薬を爆発させることでガスを発生させ、"
        "エアバッグを0.01秒の単位で瞬時に膨らませる",
        "括弧が閉じていない切れ端",
    ),
    (
        "オルガンは、加圧した空気を鍵盤で選択したパイプに送ることで発音する"
        "鍵盤楽器であり、パイプオルガンとも呼ばれる",
        "文の途中の「であり、」。分類を述べている",
    ),
    (
        "江戸ではこの押し寿司ではなく握り寿司が発展し、2つの文化に分かれていった",
        "「押し寿司」の「押し」は動詞ではない",
    ),
    (
        "特定外来生物に指定されているヒアリやアルゼンチンアリのように、"
        "人為的に分布を広げている種、すなわち「外来種」も多数ある",
        "「〜のように、」は喩え。目的の「ように、」ではない",
    ),
    (
        "たとえばメスアカムラサキやカバマダラなどが、このようにして出現し、"
        "冬までに世代を重ねる例が知られる",
        "「たとえば」で始まり、「このようにして」が前の文を指す",
    ),
    (
        "しかし時計を机のどちらに置くかで揉めたことから、"
        "後手の右側に配置することでハンデの解消とした",
        "「しかし」で始まる",
    ),
    (
        "濃厚赤血球は、血液の酸素運搬能力を回復させるために使用される",
        "何のためかを言うだけで、どうやるかが無い",
    ),
    (
        "例外として、あえて殺菌をせずに缶の中で発酵させるシュールストレミングという"
        "缶詰も存在する",
        "「例外として」は前の文が無いと何の例外か分からない",
    ),
]


@pytest.mark.parametrize("sentence,why", REAL_FALSE_POSITIVES)
def test_real_false_positives_stay_dropped(sentence, why):
    assert not looks_like_mechanism(sentence), f"{why}: {sentence}"


DATA = Path(__file__).resolve().parent.parent / "ideaman" / "data"


def _saved_mechanisms() -> list[dict]:
    return yaml.safe_load((DATA / "mechanisms.yaml").read_text())["mechanisms"]


def test_saved_mechanisms_have_the_fields_templates_need():
    """保存した一文が、題・一文・出典・分野タグを全部持っていること。"""
    saved = _saved_mechanisms()
    assert len(saved) >= 100
    lexicon = yaml.safe_load((DATA / "lexicon.yaml").read_text())
    known = {tag for item in lexicon["concepts"] for tag in item["domains"]}
    for item in saved:
        assert set(item) == {"subject", "how", "source", "domains"}, item
        assert item["subject"] and item["how"], item
        assert item["source"].startswith("https://ja.wikipedia.org/wiki/"), item
        assert 1 <= len(item["domains"]) <= 2, item
        # 遠さの計算は concepts と同じ語彙でやるので、知らないタグは使えない。
        assert set(item["domains"]) <= known, item


def test_saved_mechanisms_are_not_repeated():
    hows = [item["how"] for item in _saved_mechanisms()]
    assert len(hows) == len(set(hows))


def test_every_saved_mechanism_still_passes_the_extractor():
    """保存した一文は、今の抽出器を通ること。

    規則を厳しくしたとき、**もう通らなくなった文が保存先に残る**と、
    「抽出器を通ったものを人が選んだ」という前提が静かに崩れる。
    手で書き足した文が紛れ込んだときもここで分かる。
    """
    for item in _saved_mechanisms():
        assert looks_like_mechanism(item["how"]), item["how"]
