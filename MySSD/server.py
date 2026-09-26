import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

import config

HOST = "127.0.0.1"  # 1단계: 이 PC에서만 접속 가능. 0.0.0.0으로 바꾸지 마세요.
BASE_DIR = Path(__file__).parent
HIDDEN = {"system volume information", "$recycle.bin"}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class UserError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


def get_root():
    root = Path(config.SSD_ROOT)
    try:
        if root.is_dir():
            return root.resolve()
    except OSError:
        pass
    raise UserError("외장 SSD를 찾을 수 없습니다. 연결 상태와 config.py의 SSD_ROOT를 확인하세요.", 503)


def is_inside(root, path):
    return path == root or root in path.parents


def safe_path(rel):
    """URL로 받은 상대 경로를 SSD_ROOT 내부의 실제 경로로 변환한다. 루트 밖이면 차단."""
    root = get_root()
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if "\x00" in rel or ":" in rel or ".." in parts:
        raise UserError("허용되지 않은 경로입니다.", 403)
    try:
        target = root.joinpath(*parts).resolve()
    except (OSError, ValueError):
        raise UserError("잘못된 경로입니다.", 400)
    if not is_inside(root, target):
        raise UserError("허용되지 않은 경로입니다.", 403)
    return root, target


def list_dir(root, folder):
    entries = []
    with os.scandir(folder) as it:
        for e in it:
            if e.name.lower() in HIDDEN:
                continue
            try:
                is_dir = e.is_dir()
                if not is_inside(root, Path(e.path).resolve()):
                    continue
            except OSError:
                continue
            rel = Path(e.path).relative_to(root).as_posix()
            entries.append({"name": e.name, "path": rel, "is_dir": is_dir})
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return entries


def render_files(request, status=200, **ctx):
    ctx.setdefault("current", "/")
    ctx.setdefault("parent", None)
    ctx.setdefault("entries", [])
    ctx.setdefault("error", None)
    return templates.TemplateResponse(request, "files.html", ctx, status_code=status)


@app.exception_handler(UserError)
def user_error(request: Request, exc: UserError):
    return render_files(request, exc.status, error=exc.message)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    msg = "페이지를 찾을 수 없습니다." if exc.status_code == 404 else "잘못된 요청입니다."
    return render_files(request, exc.status_code, error=msg)


@app.exception_handler(Exception)
def server_error(request: Request, exc: Exception):
    return render_files(request, 500, error="알 수 없는 오류가 발생했습니다. 다시 시도해 주세요.")


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/files")
def files(request: Request, path: str = ""):
    root, folder = safe_path(path)
    if not folder.exists():
        raise UserError("폴더를 찾을 수 없습니다. 삭제되었거나 이름이 바뀌었을 수 있습니다.", 404)
    if not folder.is_dir():
        raise UserError("폴더가 아닙니다.", 400)
    try:
        entries = list_dir(root, folder)
    except PermissionError:
        raise UserError("이 폴더에 접근할 권한이 없습니다.", 403)
    except OSError:
        raise UserError("폴더를 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    rel = "" if folder == root else folder.relative_to(root).as_posix()
    parent = None if not rel else rel.rpartition("/")[0]
    return render_files(request, current="/" + rel, parent=parent, entries=entries)


@app.get("/download")
def download(path: str = ""):
    _, target = safe_path(path)
    if not target.is_file():
        raise UserError("파일을 찾을 수 없습니다. 삭제되었거나 이름이 바뀌었을 수 있습니다.", 404)
    try:
        with open(target, "rb"):
            pass
    except PermissionError:
        raise UserError("이 파일에 접근할 권한이 없습니다.", 403)
    except OSError:
        raise UserError("파일을 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    return FileResponse(target, filename=target.name)


if __name__ == "__main__":
    print(f"내 SSD 서버 시작: http://{HOST}:{config.PORT}  (종료: Ctrl+C)")
    uvicorn.run(app, host=HOST, port=config.PORT)
