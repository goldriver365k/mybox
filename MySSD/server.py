import hmac
import os
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

import config

HOST = "127.0.0.1"  # 이 PC에서만 접속 가능. 0.0.0.0으로 바꾸지 마세요.
BASE_DIR = Path(__file__).parent
HIDDEN = {"system volume information", "$recycle.bin"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
BAD_CHARS = set('<>:"/\\|?*')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.ENV["MYSSD_SECRET_KEY"] or secrets.token_urlsafe(32),
    session_cookie="myssd_session",
    max_age=12 * 60 * 60,
    same_site="strict",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class LoginRequired(Exception):
    pass


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
            entries.append({"name": e.name, "path": rel, "is_dir": is_dir, "kind": file_kind(e.name, is_dir)})
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return entries


def file_kind(name, is_dir=False):
    ext = os.path.splitext(name)[1].lower()
    return "dir" if is_dir else "image" if ext in IMAGE_TYPES else "video" if ext in VIDEO_TYPES else "file"


def clean_name(name):
    """업로드 파일명/새 폴더명 검증. 경로 구분자, 예약어, Windows 금지 문자를 거부한다."""
    name = (name or "").strip().rstrip(". ")
    if (
        not name
        or len(name) > 200
        or any(c in BAD_CHARS or ord(c) < 32 for c in name)
        or name.split(".")[0].strip().upper() in RESERVED
    ):
        raise UserError("사용할 수 없는 이름입니다. \\ / : * ? \" < > | 문자는 쓸 수 없습니다.")
    return name


def is_logged_in(request):
    return "session" in request.scope and bool(request.session.get("user"))


def require_login(request: Request):
    if not is_logged_in(request):
        raise LoginRequired()


def check_csrf(request, token):
    if not hmac.compare_digest(str(token), str(request.session.get("csrf", ""))):
        raise UserError("요청이 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도하세요.", 403)


def open_folder(path):
    root, folder = safe_path(path)
    if not folder.is_dir():
        raise UserError("폴더를 찾을 수 없습니다. 삭제되었거나 이름이 바뀌었을 수 있습니다.", 404)
    return root, folder


def open_file(path):
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
    return target


def redirect_files(request, path, message=None):
    if message:
        request.session["flash"] = message
    return RedirectResponse(f"/files?path={quote(path)}", status_code=303)


def render_files(request, status=200, **ctx):
    ctx.setdefault("current", "/")
    ctx.setdefault("parent", None)
    ctx.setdefault("entries", [])
    ctx.setdefault("error", None)
    ctx.setdefault("path", "")
    ctx["csrf"] = request.session.get("csrf", "")
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "files.html", ctx, status_code=status)


@app.exception_handler(LoginRequired)
def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/", status_code=303)


@app.exception_handler(UserError)
def user_error(request: Request, exc: UserError):
    return render_files(request, exc.status, error=exc.message)


@app.exception_handler(HTTPException)
@app.exception_handler(RequestValidationError)
def http_error(request: Request, exc: Exception):
    if not is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    status = getattr(exc, "status_code", 400)
    msg = "페이지를 찾을 수 없습니다." if status == 404 else "잘못된 요청입니다."
    return render_files(request, status, error=msg)


@app.exception_handler(Exception)
def server_error(request: Request, exc: Exception):
    if not is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render_files(request, 500, error="알 수 없는 오류가 발생했습니다. 다시 시도해 주세요.")


@app.get("/")
def index(request: Request):
    if is_logged_in(request):
        return RedirectResponse("/files", status_code=303)
    error = None if config.ENV["MYSSD_USER"] and config.ENV["MYSSD_PASSWORD_HASH"] else (
        "계정이 설정되지 않았습니다. 명령 프롬프트에서 python config.py 를 먼저 실행하세요."
    )
    return templates.TemplateResponse(request, "index.html", {"error": error})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    user_ok = hmac.compare_digest(username.encode(), config.ENV["MYSSD_USER"].encode())
    pass_ok = config.verify_password(password, config.ENV["MYSSD_PASSWORD_HASH"])
    if not (user_ok and pass_ok and config.ENV["MYSSD_USER"]):
        time.sleep(1)
        return templates.TemplateResponse(
            request, "index.html", {"error": "아이디 또는 비밀번호가 올바르지 않습니다."}, status_code=401
        )
    request.session.clear()
    request.session.update(user=username, csrf=secrets.token_urlsafe(32))
    return RedirectResponse("/files", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if is_logged_in(request):
        check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


private = APIRouter(dependencies=[Depends(require_login)])


@private.get("/files")
def files(request: Request, path: str = ""):
    root, folder = open_folder(path)
    try:
        entries = list_dir(root, folder)
    except PermissionError:
        raise UserError("이 폴더에 접근할 권한이 없습니다.", 403)
    except OSError:
        raise UserError("폴더를 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    rel = "" if folder == root else folder.relative_to(root).as_posix()
    parent = None if not rel else rel.rpartition("/")[0]
    return render_files(request, current="/" + rel, parent=parent, entries=entries, path=rel)


@private.get("/download")
def download(path: str = ""):
    target = open_file(path)
    return FileResponse(target, filename=target.name)


@private.get("/view")
def view(request: Request, path: str = ""):
    target = open_file(path)
    kind = file_kind(target.name)
    if kind not in ("image", "video"):
        return RedirectResponse(f"/download?path={quote(path)}", status_code=303)
    rel = target.relative_to(get_root()).as_posix()
    return templates.TemplateResponse(
        request, "view.html", {"name": target.name, "path": rel, "parent": rel.rpartition("/")[0], "kind": kind}
    )


@private.get("/raw")
def raw(path: str = ""):
    target = open_file(path)
    ext = target.suffix.lower()
    media_type = IMAGE_TYPES.get(ext) or VIDEO_TYPES.get(ext)
    if not media_type:
        raise UserError("미리보기를 지원하지 않는 파일입니다.", 400)
    return FileResponse(
        target, media_type=media_type, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private"}
    )


@private.post("/upload")
def upload(request: Request, path: str = Form(""), csrf: str = Form(""), files: list[UploadFile] = File(...)):
    check_csrf(request, csrf)
    root, folder = open_folder(path)
    files = [f for f in files if f.filename]
    if not files:
        raise UserError("올릴 파일을 선택하세요.")
    saved = []
    for up in files:
        name = clean_name((up.filename or "").replace("\\", "/").rpartition("/")[2])
        stem, ext = os.path.splitext(name)
        for n in range(1000):
            target = folder / (name if n == 0 else f"{stem} ({n}){ext}")
            if not is_inside(root, target.resolve()):
                raise UserError("허용되지 않은 경로입니다.", 403)
            try:
                out = open(target, "xb")  # x: 이미 있으면 실패 → 기존 파일 절대 덮어쓰지 않음
                break
            except FileExistsError:
                continue
            except PermissionError:
                raise UserError("이 폴더에 저장할 권한이 없습니다.", 403)
            except OSError:
                raise UserError("파일을 저장할 수 없습니다. SSD 연결 상태와 남은 공간을 확인하세요.", 503)
        else:
            raise UserError("같은 이름의 파일이 너무 많습니다.", 409)
        try:
            with out:
                shutil.copyfileobj(up.file, out, 1024 * 1024)
        except OSError:
            target.unlink(missing_ok=True)
            raise UserError("파일 저장 중 오류가 발생했습니다. SSD 연결 상태와 남은 공간을 확인하세요.", 503)
        saved.append(target.name)
    return redirect_files(request, path, f"{len(saved)}개 파일을 올렸습니다: " + ", ".join(saved))


@private.post("/folder")
def make_folder(request: Request, path: str = Form(""), name: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    root, folder = open_folder(path)
    name = clean_name(name)
    target = folder / name
    if not is_inside(root, target.resolve()):
        raise UserError("허용되지 않은 경로입니다.", 403)
    try:
        target.mkdir()
    except FileExistsError:
        raise UserError("같은 이름의 폴더나 파일이 이미 있습니다.", 409)
    except PermissionError:
        raise UserError("이 위치에 폴더를 만들 권한이 없습니다.", 403)
    except OSError:
        raise UserError("폴더를 만들 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    return redirect_files(request, path, f"'{name}' 폴더를 만들었습니다.")


app.include_router(private)


if __name__ == "__main__":
    print(f"내 SSD 서버 시작: http://{HOST}:{config.PORT}  (종료: Ctrl+C)")
    uvicorn.run(app, host=HOST, port=config.PORT)
