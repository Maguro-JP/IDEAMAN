"""FastAPI 層。

ここには判断を置かない。生成は `service` に、保存は `store` に任せる。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import service
from .engine.lexicon import all_domains, default_lexicon
from .store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"

def stock_size() -> int:
    """起動時にこの件数まで在庫を作る。0 なら補充しない。"""
    return int(os.environ.get("IDEAMAN_STOCK", service.DEFAULT_STOCK))


class RateRequest(BaseModel):
    score: int = Field(..., ge=-1, le=1, description="-1 / 0 / 1")
    note: str | None = Field(None, max_length=500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に保管庫を開き、在庫を用意しておく。"""
    store = Store()
    app.state.store = store
    target = stock_size()
    if target > 0:
        service.prefill(store, target)
    try:
        yield
    finally:
        store.close()


app = FastAPI(title="IDEAMAN", lifespan=lifespan)


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


@app.get("/api/domains")
def domains() -> dict:
    """語彙に登場する分野タグの一覧。"""
    return {"domains": sorted(all_domains(default_lexicon()))}
