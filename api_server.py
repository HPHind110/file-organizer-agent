"""Local HTTP entry point for File Organization Agent."""
import argparse
from collections import OrderedDict
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
import uvicorn

from organizer.engine import apply, identity, rollback
from organizer.planner import build_plan
from organizer.windows import SafetyError, pin_tree, select_root


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Directory(Input):
    directory: StrictStr
    recursive: StrictBool = False


class Approval(Input):
    approved: StrictBool


class Action(BaseModel):
    source: str
    status: str
    reason: str
    target: str | None
    snapshot: dict[str, int | str] | None


class Preview(BaseModel):
    plan_id: str
    root: str
    items: list[Action]
    duplicates: list[list[str]]


class Move(BaseModel):
    seq: int
    src: str
    dst: str
    state: str
    detail: str


class Operation(BaseModel):
    operation_id: str | None
    moves: list[Move]


def create_app(folder, token):
    if not token or len(token) < 32 or not token.isascii() or not token.isalnum() and not all(
        c.isalnum() or c in "_-" for c in token
    ):
        raise ValueError("Set ORGANIZER_API_TOKEN to at least 32 ASCII letters, digits, _ or -.")
    root = select_root(folder)
    with pin_tree(root):
        selected = identity(root)
    app = FastAPI(title="File Organization Agent", version="1.0")
    bearer = HTTPBearer(auto_error=False)
    cache = OrderedDict()
    lock = Lock()

    def auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})

    @contextmanager
    def guarded():
        try:
            with lock, pin_tree(root):
                if identity(root) != selected:
                    raise HTTPException(409, "Configured directory has changed")
                yield
        except (OSError, SafetyError, sqlite3.Error) as exc:
            raise HTTPException(409, str(exc)) from exc

    def saved(plan_id):
        if plan_id not in cache:
            raise HTTPException(404, "Unknown or expired plan")
        return cache[plan_id]

    def preview(plan):
        return plan.payload() | {"plan_id": plan.token}

    def approve(body):
        if not body.approved:
            raise HTTPException(409, "Explicit approval is required")

    def result(run):
        return {"operation_id": run["id"] if run else None, "moves": run["moves"] if run else []}

    @app.get("/health")
    def health() -> dict[str, Literal["ok"]]:
        return {"status": "ok"}

    @app.post("/plans", dependencies=[Depends(auth)], response_model=Preview, status_code=201)
    def plan(body: Directory):
        if not Path(body.directory).is_absolute() or Path(body.directory) != root:
            raise HTTPException(403, "Directory must match the operator-selected root")
        with guarded():
            proposal = build_plan(root, recursive=body.recursive)
            cache[proposal.token] = (proposal, body.recursive)
            cache.move_to_end(proposal.token)
            if len(cache) > 32:
                cache.popitem(last=False)
            return preview(proposal)

    @app.get("/plans/{plan_id}", dependencies=[Depends(auth)], response_model=Preview)
    def inspect(plan_id: str):
        with lock:
            return preview(saved(plan_id)[0])

    @app.post("/plans/{plan_id}/execute", dependencies=[Depends(auth)], response_model=Operation)
    def execute(plan_id: str, body: Approval):
        approve(body)
        with guarded():
            proposal, recursive = saved(plan_id)
            if build_plan(root, recursive=recursive).token != plan_id:
                raise HTTPException(409, "Plan changed; request a new preview")
            run = apply(proposal, plan_id)
            del cache[plan_id]
            return result(run)

    @app.post("/operations/{operation_id}/rollback", dependencies=[Depends(auth)], response_model=Operation)
    def undo(operation_id: str, body: Approval):
        approve(body)
        with guarded():
            try:
                return result(rollback(root, operation_id))
            except SafetyError as exc:
                if str(exc).startswith("Unknown run "):
                    raise HTTPException(404, "Unknown operation") from exc
                raise

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8101)
    args = parser.parse_args()
    app = create_app(args.root, os.environ.get("ORGANIZER_API_TOKEN"))
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)
