"""LLM 仕上げ（任意）。

**ここを通った文字列は、そのままユーザーの目に入る。** API キーが無いと
一度も実行されない経路なので、壊れていても分からない。呼び出し自体は
試せないが、**受け入れ判定は試せる**。

判定できないものは受け入れない側に倒す。素のテンプレート出力のほうが、
前置きや思考のタグが混ざった文章よりまだよい。
"""

from ideaman.engine.refiner import MODEL, is_available, refine, usable

ORIGINAL = "通知の待ち時間を半分に削る。まず15分だけ試して捨てる。"


def test_a_clean_rewrite_is_accepted():
    assert usable("通知の待ち時間を半分に削る。まず15分だけ試そう。", ORIGINAL)


def test_a_leaked_thinking_tag_is_rejected():
    """Claude Opus 5 で thinking を切ると起きうる。だから切っていない。"""
    assert not usable("<thinking>これは…</thinking>通知を削る。", ORIGINAL)
    assert not usable("通知を削る。<br>", ORIGINAL)


def test_a_preamble_is_rejected():
    long_answer = (
        "承知しました。ご要望に沿って整えます。通知の待ち時間を半分に削ります。"
        "これは待ち時間が体験に与える影響を考えると妥当な判断です。"
        "さらに、段階的に試すことで risk を抑えられます。まず15分だけ試しましょう。"
    )
    assert not usable(long_answer, ORIGINAL)


def test_a_list_is_rejected():
    assert not usable("- 通知を削る\n- 15分だけ試す", ORIGINAL)
    assert not usable("1. 通知を削る", ORIGINAL)
    assert not usable("```\n通知を削る\n```", ORIGINAL)


def test_an_explanation_is_rejected():
    """1〜2文と指示している。句点が多ければ説明が始まっている。"""
    assert not usable("あ。い。う。え。お。", ORIGINAL)


def test_nothing_is_rejected():
    assert not usable("", ORIGINAL)
    assert not usable("   ", ORIGINAL)


def test_a_short_original_still_allows_a_reasonable_rewrite():
    """元が極端に短いと、倍率だけでは狭すぎる。下限を設けてある。"""
    assert usable("通知の順番を反転させる。", "通知を反転。")


def test_the_model_is_the_one_we_intend():
    assert MODEL == "claude-opus-5"


def test_without_a_key_it_returns_the_template_untouched(monkeypatch):
    """キーが無ければ素通し。ここで例外を投げてはいけない。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not is_available()

    class Seed:
        tone = "proposal"
        mood = "実務"

        def __init__(self) -> None:
            self.slots = {"material": "通知"}

    text, source = refine(Seed(), ORIGINAL)
    assert (text, source) == (ORIGINAL, "engine")
