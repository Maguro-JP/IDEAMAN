"""FastAPI 層。

ここには判断を置かない。生成は `service` に、保存は `store` に任せる。
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import harvest, service
from .engine.lexicon import all_domains, default_lexicon
from .store import Store

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

def stock_size() -> int:
    """起動時にこの件数まで在庫を作る。0 なら補充しない。"""
    return int(os.environ.get("IDEAMAN_STOCK", service.DEFAULT_STOCK))


def scan_root() -> Path | None:
    """Web から読ませてよいディレクトリの上限。未設定なら Web からは読ませない。

    **Web は公開 HTTPS で動かす前提**なので、`scan` に任意のパスを受け付けると
    誰でもファイルシステムを探れる（実際 `/etc` から語が採れた）。既定では
    塞ぎ、運用者が明示的に開いたときだけ、その下だけを許す。

    CLI と MCP は手元でユーザー自身が実行するので、この制限は掛からない。
    """
    raw = os.environ.get("IDEAMAN_SCAN_ROOT")
    return Path(raw).resolve() if raw else None


def _resolve_scan(raw: str) -> Path:
    """Web から渡されたパスを検証する。

    Raises:
        HTTPException: 読ませてよい場所の外を指している場合。
    """
    root = scan_root()
    if root is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "Web からディレクトリを読む設定になっていません。"
                "IDEAMAN_SCAN_ROOT に読ませてよい場所を指定してください"
                "（手元なら ideaman theme --scan が使えます）"
            ),
        )
    target = Path(raw).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(
            status_code=403, detail=f"{root} の外は読みません"
        )
    return target


def embed_worker() -> bool:
    """ワーカーを Web と同じプロセスで回すか。

    SQLite を使う以上、Web とワーカーは**同じディスクを見ている必要がある**。
    1台のホストに載せるときはプロセスを分ける意味が薄いので、同居させて
    しまうほうが確実（別マシンに分けると DB が共有されない）。

    ローカル開発では既定どおり分けたままにする。
    """
    return os.environ.get("IDEAMAN_EMBED_WORKER", "").lower() in ("1", "true", "yes")


class RateRequest(BaseModel):
    score: int = Field(..., ge=-1, le=1, description="-1 / 0 / 1")
    note: str | None = Field(None, max_length=500)


class ThemeRequest(BaseModel):
    """いま取り組んでいることと、その材料。

    材料（あなた自身の語）が入って初めて、衝突が宙に浮いた警句ではなく
    手元の課題の上に着地する。ここが有益さの入口。
    """

    name: str = Field(..., min_length=1, max_length=100)
    note: str | None = Field(None, max_length=500)
    materials: list[str] = Field(default_factory=list)
    #: 読ませるディレクトリ。指定すると、そこで実際に使われている語を
    #: 材料として自動で集める（手で登録する手間が有益さの最大の障壁）。
    scan: str | None = Field(None, max_length=1000)


class MaterialsRequest(BaseModel):
    """材料だけを載せた要求。いまは取り外しにだけ使う。"""

    materials: list[str] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に保管庫を開き、在庫を用意しておく。"""
    store = Store()
    app.state.store = store
    target = stock_size()
    if target > 0:
        service.prefill(store, target)

    if not os.environ.get("IDEAMAN_TOKEN"):
        log.warning(
            "IDEAMAN_TOKEN が未設定です。公開する場所で動かすなら設定してください"
            "（誰でもテーマ・材料の閲覧と削除ができます）"
        )

    if embed_worker():
        # デーモンスレッドなので、Web が終わるときに道連れで止まる。
        # ワーカー側は例外を自分で飲み込むので、ここで落ちることはない。
        from .notify import QueueNotifier
        from .worker import Worker

        worker = Worker(
            store,
            [QueueNotifier(store, "discord")],
            stock=target if target > 0 else service.DEFAULT_STOCK,
        )
        thread = threading.Thread(target=worker.run, daemon=True, name="ideaman-worker")
        thread.start()
        log.info("ワーカーを同じプロセスで起動した")

    try:
        yield
    finally:
        store.close()


app = FastAPI(title="IDEAMAN", lifespan=lifespan)


def _authorized(header: str, token: str) -> bool:
    """`Authorization` ヘッダが合鍵を持っているか。

    Basic と Bearer の両方を受ける。Basic を受けるのは、ブラウザが
    自分で聞いて覚えてくれるから——UI 側に何も足さずに守れる。
    """
    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        return secrets.compare_digest(value.strip(), token)
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value.strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, password = decoded.partition(":")
        return secrets.compare_digest(password, token)
    return False


@app.middleware("http")
async def require_token(request: Request, call_next):
    """`IDEAMAN_TOKEN` があれば、合鍵を持つ人しか通さない。

    **無認証のままだと、URL を知った人が何でもできる。** 実際に確かめた:
    テーマと材料（＝あなたのプロジェクトの語彙）が読め、評価を汚染でき、
    材料を消せ、テーマを止められた。Web は公開 HTTPS で動かす前提なので、
    公開するなら鍵が要る。

    設定しなければ素通し。手元で動かすぶんには今までどおり。
    """
    token = os.environ.get("IDEAMAN_TOKEN")
    if not token or _authorized(request.headers.get("authorization", ""), token):
        return await call_next(request)
    return Response(
        status_code=401,
        content="IDEAMAN_TOKEN が要ります",
        headers={"WWW-Authenticate": 'Basic realm="IDEAMAN"'},
    )


def get_store() -> Store:
    return app.state.store


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/idea")
def take_idea(
    domain: str | None = Query(None, description="分野を指定して受け取る"),
    store: Store = Depends(get_store),
) -> dict:
    """溜まっている考えを1つ受け取る。在庫が空ならその場で考える。"""
    idea = service.receive_idea(store, domain=domain)
    return idea.to_dict()


@app.post("/api/idea/{idea_id}/rate")
def rate_idea(
    idea_id: int,
    body: RateRequest,
    store: Store = Depends(get_store),
) -> dict:
    """評価を付ける。付いた評価は次回以降の抽選の重みに反映される。"""
    try:
        idea = store.rate(idea_id, body.score, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return idea.to_dict()


@app.get("/api/ideas")
def list_ideas(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    store: Store = Depends(get_store),
) -> dict:
    """これまでに表に出たアイデアの一覧（新しい順）。"""
    ideas = store.list_seen(limit=limit, offset=offset)
    return {"ideas": [idea.to_dict() for idea in ideas]}


@app.get("/api/stock")
def stock(store: Store = Depends(get_store)) -> dict:
    """在庫件数。UI の「◯個の考えが溜まっている」表示に使う。"""
    return {"count": store.stock_count()}


@app.get("/api/themes")
def list_themes(store: Store = Depends(get_store)) -> dict:
    """登録済みのテーマと、その材料。"""
    return {
        "themes": [
            {**theme.to_dict(), "materials": store.materials(theme.id)}
            for theme in store.themes(active_only=False)
        ]
    }


@app.post("/api/themes")
def create_theme(body: ThemeRequest, store: Store = Depends(get_store)) -> dict:
    """テーマを登録し、材料を足す。同名なら既存のものに足す。"""
    theme = store.add_theme(body.name, body.note)
    store.add_materials(theme.id, body.materials)
    if body.scan:
        try:
            found = harvest.harvest(_resolve_scan(body.scan))
        except (NotADirectoryError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.add_materials(
            theme.id,
            [c.term for c in found],
            weights=harvest.initial_weights(found),
        )
    return {**theme.to_dict(), "materials": store.materials(theme.id)}


@app.post("/api/themes/{theme_id}/materials/drop")
def drop_materials(
    theme_id: int,
    body: MaterialsRequest,
    store: Store = Depends(get_store),
) -> dict:
    """材料を外す。`scan` で入った不要な語を消すため。"""
    if store.theme(theme_id) is None:
        raise HTTPException(status_code=404, detail=f"テーマ {theme_id} は存在しません")
    store.drop_materials(theme_id, body.materials)
    theme = store.theme(theme_id)
    assert theme is not None
    return {**theme.to_dict(), "materials": store.materials(theme_id)}


@app.post("/api/themes/{theme_id}/active")
def set_theme_active(
    theme_id: int,
    active: bool = Query(True),
    store: Store = Depends(get_store),
) -> dict:
    """そのテーマについて考えるかどうか。消さずに休ませる。"""
    if store.theme(theme_id) is None:
        raise HTTPException(status_code=404, detail=f"テーマ {theme_id} は存在しません")
    store.set_theme_active(theme_id, active)
    theme = store.theme(theme_id)
    assert theme is not None
    return {**theme.to_dict(), "materials": store.materials(theme.id)}


@app.get("/api/domains")
def domains() -> dict:
    """語彙に登場する分野タグの一覧。"""
    return {"domains": sorted(all_domains(default_lexicon()))}
