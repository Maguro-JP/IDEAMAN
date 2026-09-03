"""思考のリズム。

多様性を常に強制するのは間違い。人の思考は一つのきっかけから連鎖的に広がり、
掘り尽くしたところで別の場所へ移り、しばらくして戻ってくると新しい形で
思いつく。ここはその切り替えだけを担当する。

対応する既存研究:
- 連鎖して一気に思いつく          … intensification / spreading activation
- 収束したら離れる                … diversification への切り替え（タブーサーチ）
- 禁じても良いものは通す          … aspiration criterion (Glover 1990)
- 頭の片隅に置いて後で思いつく    … incubation effect (Sio & Ormerod 2009)

判断は全部この場で完結する純粋関数にしてある。DB も乱数も触らない。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

ASSOCIATE = "associate"
WANDER = "wander"

#: 気分 -> 寄せたい語調。連鎖はこの調子で続く。
#: **強制ではなく偏り**（`MOOD_PULL`）。同じ調子だけが続くと単調になる。
MOOD_TONE = {
    "挑発": "question",
    "逆説": "question",
    "実務": "proposal",
    "夢想": "proposal",
}

#: 気分の語調に従う確率。1.0 にすると連鎖が一本調子になる。
MOOD_PULL = 0.75


def tone_for(mood: str, roll: float) -> str | None:
    """その気分が寄せたい語調。従わない回もある。

    Args:
        mood: 連鎖の気分。
        roll: 0〜1 の乱数。`MOOD_PULL` 未満なら気分に従う。

    Returns:
        `proposal` / `question` / どちらでもよければ None。
    """
    tone = MOOD_TONE.get(mood)
    if tone is None or roll >= MOOD_PULL:
        return None
    return tone

#: 一つの連鎖の長さの上限。ここまで来たら鉱脈が太くても一度離れる。
MAX_CHAIN_LENGTH = 7

#: 連鎖の中で何回重複を引いたら「尽きた」と見なすか。
#: 1 だと一度の偶然で連鎖が切れる。2 なら粘る。
STALE_LIMIT = 2

#: 放浪から連想へ移る確率の基礎値。
#: striking な引きでも必ず連鎖が始まるわけではない（そういうものだと思う）。
BASE_SPARK = 0.3

#: 引いた語の重みがこれを超えていたら、掘る価値があると見なして確率を上げる。
SPARK_WEIGHT = 1.3
SPARK_WEIGHT_BONUS = 0.4

#: 語同士がどれだけ離れているか（0〜1）。遠い組ほど「おっ」と思う。
SPARK_NOVELTY = 0.9
SPARK_NOVELTY_BONUS = 0.2

#: どれだけ条件が揃っても、これ以上は連鎖を始めない。
MAX_SPARK = 0.85

#: 休眠の基本の長さ。行き詰まった回数に応じて伸びる。
BASE_DORMANCY = timedelta(hours=6)
MAX_DORMANCY = timedelta(days=7)

#: 休眠中でもこの重みを超えていれば抽選に戻す（aspiration criterion）。
#: 「連続がダメなのではない」— 十分に良いものは禁を破ってよい。
ASPIRATION_WEIGHT = 2.0

#: 休眠明けの語を少しだけ引きやすくする倍率。
#: 「ふとしたときに思いつく」を意図的に作るための下駄。
REVIVAL_BOOST = 1.4

#: 休眠明けからこの時間だけ REVIVAL_BOOST が効く。
REVIVAL_WINDOW = timedelta(hours=12)


@dataclass(frozen=True)
class ThoughtState:
    """いま何を考えているか。"""

    mode: str = WANDER
    chain_id: int | None = None
    chain_length: int = 0
    stale_hits: int = 0
    #: いまの思考の気分。**アイデアではなく連鎖に属する。**
    #: 一つの思考の流れは一つの調子で続くもので、一手ごとに提案と問いが
    #: 入れ替わるのは連鎖ではなく交替。空なら気分なし（従来どおり）。
    mood: str = ""

    @property
    def is_associating(self) -> bool:
        return self.mode == ASSOCIATE


@dataclass(frozen=True)
class Step:
    """次の一手をどう引くかの指示。

    Attributes:
        mode: 引き方。`associate` なら親の語を引き継ぐ。
        inherit: 引き継ぐ語の数。`wander` なら 0。
        state: この一手を反映した後の状態。
    """

    mode: str
    inherit: int
    state: ThoughtState


def plan_step(state: ThoughtState) -> Step:
    """いまの状態から、次の一手の引き方を決める。

    連想中なら語を引き継ぎ、放浪中なら自由に引く。連鎖が上限に達していたら
    ここで放浪へ戻す。
    """
    if state.is_associating and state.chain_length < MAX_CHAIN_LENGTH:
        # 連鎖が進むほど引き継ぎを減らし、少しずつ話題をずらしていく
        inherit = 2 if state.chain_length <= 2 else 1
        return Step(mode=ASSOCIATE, inherit=inherit, state=state)
    return Step(mode=WANDER, inherit=0, state=replace(state, mode=WANDER))


def spark_chance(best_weight: float, novelty: float) -> float:
    """この引きから連想を始める確率。

    育った語（＝過去に評価された）を含むか、語同士が遠いか。どちらも
    「おっ」と思う条件で、揃うほど掘りたくなる。ただし確実ではない。

    重みによる学習がまだ無い立ち上げ直後でも連鎖が起きるよう、基礎値を
    持たせてある（評価ゼロの状態で永久に放浪し続けるのを避けるため）。
    """
    chance = BASE_SPARK
    if best_weight >= SPARK_WEIGHT:
        chance += SPARK_WEIGHT_BONUS
    if novelty >= SPARK_NOVELTY:
        chance += SPARK_NOVELTY_BONUS
    return min(chance, MAX_SPARK)


def after_hit(
    state: ThoughtState,
    *,
    chain_id: int,
    best_weight: float,
    novelty: float = 0.0,
    roll: float = 1.0,
) -> ThoughtState:
    """新しいアイデアが出たときの状態遷移。

    Args:
        chain_id: 今回のアイデアが属する連鎖。
        best_weight: 引いた語の重みの最大値。
        novelty: 引いた語同士の遠さ（0〜1）。
        roll: 0〜1 の乱数。`spark_chance` を下回れば連想を始める。
            乱数はここでは引かない（この関数を決定的に保つため）。
    """
    if state.is_associating:
        return replace(
            state,
            chain_length=state.chain_length + 1,
            stale_hits=0,  # 新しいものが出たので粘りカウントは戻す
        )

    if roll < spark_chance(best_weight, novelty):
        # 何かが引っかかった。ここから掘ってみる。
        return ThoughtState(
            mode=ASSOCIATE, chain_id=chain_id, chain_length=1, stale_hits=0
        )

    # ただの放浪。連鎖はまだ始まっていない。
    return replace(state, mode=WANDER, chain_id=chain_id, chain_length=0, stale_hits=0)


def after_duplicate(state: ThoughtState) -> ThoughtState:
    """同じ組み合わせに再到達したときの状態遷移。

    重複は失敗ではない。同じ場所に何度も辿り着くのは、そこに何かがある
    という信号。ただし出力はしないので、粘りカウントだけ進める。
    """
    return replace(state, stale_hits=state.stale_hits + 1)


def is_exhausted(state: ThoughtState) -> bool:
    """この連鎖は掘り尽くしたか。

    尽きたときだけ休眠と放浪へ切り替える。常時ではない。
    """
    if not state.is_associating:
        return False
    return state.stale_hits >= STALE_LIMIT or state.chain_length >= MAX_CHAIN_LENGTH


def leave_chain(state: ThoughtState) -> ThoughtState:
    """連鎖を離れて放浪へ戻る。"""
    return ThoughtState(mode=WANDER, chain_id=None, chain_length=0, stale_hits=0)


def dormancy_for(stall_count: int, *, now: datetime | None = None) -> datetime:
    """休眠がいつ明けるか。

    同じ語で何度も行き詰まるほど長く寝かせる。ただし上限があり、
    永久に消えることはない。
    """
    now = now or datetime.now(timezone.utc)
    # 指数を先に頭打ちにする。stall_count は際限なく増えるので、
    # 掛けてから min を取ると timedelta が溢れる。
    doublings = min(max(stall_count, 0), 16)
    span = min(BASE_DORMANCY * (2**doublings), MAX_DORMANCY)
    return now + span


def draw_weight(
    weight: float,
    dormant_until: datetime | None,
    *,
    now: datetime | None = None,
) -> float:
    """抽選に使う実効的な重み。

    保存された重み（評価による学習の結果）はここでは書き換えない。
    休眠と復帰の効果を、抽選のときだけ上に被せる。

    Returns:
        0.0 なら抽選から外れる。
    """
    now = now or datetime.now(timezone.utc)

    if dormant_until is None:
        return weight

    if now < dormant_until:
        # 休眠中。ただし十分に良いものは通す（aspiration criterion）
        return weight if weight >= ASPIRATION_WEIGHT else 0.0

    if now - dormant_until <= REVIVAL_WINDOW:
        # 明けたばかり。「ふとしたときに思いつく」ための下駄。
        return weight * REVIVAL_BOOST

    return weight
