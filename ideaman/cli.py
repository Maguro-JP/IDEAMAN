"""コマンドライン。

    python -m ideaman idea            一つ受け取る
    python -m ideaman worker          常駐して考え続ける
    python -m ideaman worker --once   1周だけ
    python -m ideaman diversity       リズムの健康度を見る
    python -m ideaman chain <id>      連鎖を辿る
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import diversity, service
from .notify import ConsoleNotifier, QueueNotifier
from .store import Store
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
    print(f"  #{idea.id}  {idea.mode or '-'}  在庫 {store.stock_count()}")
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
