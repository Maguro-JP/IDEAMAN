"""コマンドライン。

    python -m ideaman idea            一つ受け取る
    python -m ideaman worker          常駐して考え続ける
    python -m ideaman worker --once   1周だけ
    python -m ideaman doctor          設定を診断する
    python -m ideaman diversity       リズムの健康度を見る
    python -m ideaman chain <id>      連鎖を辿る
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import diversity, doctor, harvest, service
from .notify import ConsoleNotifier, QueueNotifier
from .store import Store, clean_material
from .worker import DEFAULT_STOCK, Worker


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_idea(args: argparse.Namespace, store: Store) -> int:
    idea = service.receive_idea(store, domain=args.domain, use_llm=not args.no_llm)
    print(f"\n  {idea.text}\n")
    theme = store.theme(idea.theme_id) if idea.theme_id else None
    label = theme.name if theme else "テーマなし"
    print(f"  #{idea.id}  {label}  在庫 {store.stock_count()}")
    return 0


def cmd_worker(args: argparse.Namespace, store: Store) -> int:
    notifiers = [QueueNotifier(store, "discord")]
    if args.console:
        notifiers.append(ConsoleNotifier())

    worker = Worker(
        store,
        notifiers,
        stock=args.stock,
        use_llm=not args.no_llm,
    )
    if args.once:
        worker.tick()
        print(f"在庫 {store.stock_count()} 件")
        return 0
    try:
        worker.run()
    except KeyboardInterrupt:
        print("\n止めました")
    return 0


def cmd_theme(args: argparse.Namespace, store: Store) -> int:
    """テーマの登録と一覧。

    テーマが無いと、システムは何について考えればいいか分からず、
    汎用の語を衝突させた警句しか作れない。ここが有益さの入口。
    """
    if args.name is None:
        themes = store.themes(active_only=False)
        if not themes:
            print("テーマがまだありません。\n")
            print('  ideaman theme "IDEAMANを伸ばす" --add 通知,語彙,連鎖')
            print("\n材料（あなた自身の語）を入れるほど、出てくる案が具体的になります。")
            return 0
        for theme in themes:
            mark = " " if theme.active else "×"
            materials = store.materials(theme.id)
            print(f"{mark} [{theme.id}] {theme.name}")
            if theme.note:
                print(f"      {theme.note}")
            print(f"      材料 {len(materials)}: {'、'.join(materials) or '（まだ無い）'}")
        return 0

    theme = store.add_theme(args.name, args.note)
    if args.add:
        wanted = args.add.split(",")
        added = store.add_materials(theme.id, wanted)
        print(f"材料を {added} 件足しました")
        # 黙って捨てると「入れたのに出てこない」になる。理由まで書く。
        rejected = [w.strip() for w in wanted if clean_material(w) is None]
        rejected = [w for w in rejected if w]
        if rejected:
            print(f"次は材料になりません: {'、'.join(rejected)}")
            print("  材料は**語**です。文や記号の羅列は、文中に埋め込むと壊れます。")
    if args.drop:
        dropped = store.drop_materials(theme.id, args.drop.split(","))
        print(f"材料を {dropped} 件外しました")
    if args.scan:
        found = harvest.harvest(Path(args.scan), limit=args.scan_limit)
        if not found:
            print(f"{args.scan} から材料になりそうな語が見つかりませんでした")
        else:
            for candidate in found:
                print(f"  {candidate}")
            if args.preview:
                print("\n（--preview なので登録していません）")
            else:
                added = store.add_materials(
                    theme.id,
                    [c.term for c in found],
                    weights=harvest.initial_weights(found),
                )
                print(f"\n材料を {added} 件足しました")
    if args.off:
        store.set_theme_active(theme.id, False)
        print("このテーマについては考えないようにしました")
    elif args.on:
        store.set_theme_active(theme.id, True)

    materials = store.materials(theme.id)
    print(f"[{theme.id}] {theme.name}")
    print(f"材料 {len(materials)}: {'、'.join(materials) or '（まだ無い）'}")
    if not materials:
        print("\n材料がまだありません。あなた自身の語を入れると案が具体的になります:")
        print(f'  ideaman theme "{theme.name}" --add 通知,語彙,連鎖')
    return 0


def cmd_doctor(args: argparse.Namespace, store: Store) -> int:
    """設定の健康診断。

    このシステムは黙って劣化する（テーマ未登録なら警句しか出ない、DB が
    揮発する場所なら毎回記憶を失う）。どれもエラーにならないので、
    動いているように見えたまま役に立たなくなる。
    """
    result = doctor.diagnose(store)
    print(result.report())
    return 1 if result.worst == "bad" else 0


def cmd_diversity(args: argparse.Namespace, store: Store) -> int:
    print(diversity.measure(store, limit=args.limit).report())
    return 0


def cmd_chain(args: argparse.Namespace, store: Store) -> int:
    try:
        chain = store.chain(args.idea_id)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 1
    for idea in chain:
        print(f"  {idea.text}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 共通フラグはサブコマンドの前後どちらに置いても効くようにする。
    # `ideaman worker --no-llm` と書けないのは地味に不便なので。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument(
        "--no-llm",
        action="store_true",
        help="LLM 仕上げを使わない（既定でもキーが無ければ使わない）",
    )

    parser = argparse.ArgumentParser(
        prog="ideaman", description="アイデアを溜める", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_idea = sub.add_parser(
        "idea", help="溜まっている考えを一つ受け取る", parents=[common]
    )
    p_idea.add_argument("--domain")
    p_idea.set_defaults(func=cmd_idea)

    p_worker = sub.add_parser("worker", help="常駐して考え続ける", parents=[common])
    p_worker.add_argument("--once", action="store_true", help="1周だけ回して終わる")
    p_worker.add_argument("--stock", type=int, default=DEFAULT_STOCK)
    p_worker.add_argument(
        "--console", action="store_true", help="配信をターミナルにも出す"
    )
    p_worker.set_defaults(func=cmd_worker)

    p_theme = sub.add_parser(
        "theme", help="いま取り組んでいることを登録する（有益さの入口）", parents=[common]
    )
    p_theme.add_argument("name", nargs="?", help="テーマ名。省略すると一覧")
    p_theme.add_argument("--add", help="材料をカンマ区切りで足す（例: 通知,語彙,連鎖）")
    p_theme.add_argument(
        "--drop", help="材料をカンマ区切りで外す（--scan で入った不要な語を消す）"
    )
    p_theme.add_argument(
        "--scan",
        metavar="PATH",
        help="そのディレクトリを読んで材料を自分で集める（閲覧許可を与える）",
    )
    p_theme.add_argument("--scan-limit", type=int, default=25, help="集める語の数")
    p_theme.add_argument(
        "--preview", action="store_true", help="--scan の結果を見るだけで登録しない"
    )
    p_theme.add_argument("--note", help="補足")
    p_theme.add_argument("--off", action="store_true", help="このテーマを一旦休む")
    p_theme.add_argument("--on", action="store_true", help="再開する")
    p_theme.set_defaults(func=cmd_theme)

    p_doc = sub.add_parser(
        "doctor", help="設定を診断する（うまく出ないと感じたら最初にこれ）",
        parents=[common],
    )
    p_doc.set_defaults(func=cmd_doctor)

    p_div = sub.add_parser("diversity", help="リズムの健康度を見る", parents=[common])
    p_div.add_argument("--limit", type=int, default=500)
    p_div.set_defaults(func=cmd_diversity)

    p_chain = sub.add_parser("chain", help="連鎖を辿る", parents=[common])
    p_chain.add_argument("idea_id", type=int)
    p_chain.set_defaults(func=cmd_chain)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    store = Store()
    try:
        return args.func(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
