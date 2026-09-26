import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import ClientDisconnect

import config

BASE_DIR = Path(__file__).parent
HIDDEN = {"system volume information", "$recycle.bin"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
BAD_CHARS = set('<>:"/\\|?*')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
MANAGED_PREFIX = ".myssd"  # .myssd_temp, .myssd_trash 등 MySSD 내부 관리 폴더. 웹에서 접근 불가
TEMP_DIR = ".myssd_temp"
TRASH_DIR = ".myssd_trash"
TRASH_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
CHUNK = 1024 * 1024
PUBLIC_PATHS = {"/", "/login", "/logout"}
ALLOWED_NETWORKS = [ipaddress.ip_network(n) for n in config.ALLOWED_NETWORKS]
SECRET_KEY = config.ENV["MYSSD_SECRET_KEY"]
if len(SECRET_KEY) < 32:
    SECRET_KEY = None

# 로그인 세션과 실패 횟수는 메모리에만 보관 (서버를 재시작하면 모두 다시 로그인)
SESSIONS = {}  # sid -> 만료 시각
FAILS = {}  # 접속 IP -> (연속 실패 횟수, 차단 해제 시각)
LOCK = threading.Lock()
FS_LOCK = threading.Lock()  # 이름 확인 + 이동을 한 번에 처리 (덮어쓰기 방지)
ACTIVE_UPLOADS = {}  # 업로드 토큰 -> 저장될 폴더. 업로드 중인 폴더는 이동/이름 변경/삭제 금지

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("myssd")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_DIR / "myssd.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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


def is_managed(name):
    return name.lower().startswith(MANAGED_PREFIX)


def safe_path(rel):
    """URL로 받은 상대 경로를 SSD_ROOT 내부의 실제 경로로 변환한다. 루트 밖이나 관리 폴더면 차단."""
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
    if any(is_managed(p) for p in parts + list(target.relative_to(root).parts)):
        raise UserError("MySSD 관리용 폴더에는 접근할 수 없습니다.", 403)
    return root, target


def to_rel(root, path):
    return "" if path == root else path.relative_to(root).as_posix()


def list_dir(root, folder):
    entries = []
    with os.scandir(folder) as it:
        for e in it:
            if e.name.lower() in HIDDEN or is_managed(e.name):
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
    """업로드/새 폴더/이름 변경용 이름 검증. 경로 구분자, 예약어, Windows 금지 문자, 관리 폴더명을 거부한다."""
    name = (name or "").strip().rstrip(". ")
    if (
        not name
        or len(name) > 200
        or any(c in BAD_CHARS or ord(c) < 32 for c in name)
        or name.split(".")[0].strip().upper() in RESERVED
        or is_managed(name)
    ):
        raise UserError("사용할 수 없는 이름입니다. \\ / : * ? \" < > | 문자는 쓸 수 없습니다.")
    return name


def is_logged_in(request):
    if "session" not in request.scope:
        return False
    with LOCK:
        expires = SESSIONS.get(request.session.get("sid"))
    return bool(expires and expires > time.time())


def ip_allowed(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in ALLOWED_NETWORKS)


def format_size(n):
    return f"{n / 1024**3:.1f}GB" if n >= 1024**3 else f"{n // 1024**2}MB"


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


def audit(request, event, **info):
    ip = request.client.host if request.client else "-"
    log.info("%s ip=%s %s", event, ip, " ".join(f"{k}={v!r}" for k, v in info.items()))


def open_item(path):
    root, target = safe_path(path)
    if target == root:
        raise UserError("SSD 최상위 폴더는 변경할 수 없습니다.")
    if not target.exists():
        raise UserError("파일이나 폴더를 찾을 수 없습니다. 삭제되었거나 이름이 바뀌었을 수 있습니다.", 404)
    return root, target


def check_not_busy(path):
    with LOCK:
        busy = any(f == path or path in f.parents for f in ACTIVE_UPLOADS.values())
    if busy:
        raise UserError("이 폴더에 파일을 올리는 중입니다. 업로드가 끝난 뒤 다시 시도하세요.", 409)


@contextmanager
def fs_errors(action):
    try:
        yield
    except FileExistsError:
        raise UserError("같은 이름의 파일이나 폴더가 이미 있습니다. 기존 파일은 덮어쓰지 않습니다.", 409)
    except FileNotFoundError:
        raise UserError("파일이나 폴더를 찾을 수 없습니다. 삭제되었거나 이름이 바뀌었을 수 있습니다.", 404)
    except PermissionError:
        raise UserError(f"{action}할 수 없습니다. 파일이 사용 중(다운로드·재생 중)이거나 권한이 없습니다.", 403)
    except OSError:
        raise UserError(f"{action} 중 오류가 발생했습니다. SSD 연결 상태와 남은 공간을 확인하세요.", 503)


def move_no_overwrite(src, dst):
    """같은 SSD 안에서 rename으로 이동 (복사 없음). dst가 이미 있으면 FileExistsError."""
    with FS_LOCK:
        if os.name != "nt" and os.path.lexists(dst) and not (os.path.exists(dst) and os.path.samefile(src, dst)):
            raise FileExistsError(dst)
        os.rename(src, dst)  # Windows: 대상이 있으면 스스로 실패


def move_unique(root, src, folder, name, is_dir):
    """folder/name 으로 이동. 같은 이름이 있으면 'name (1).ext', 'name (2).ext' ... 로 저장."""
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    for n in range(1000):
        target = folder / (name if n == 0 else f"{stem} ({n}){ext}")
        if not is_inside(root, target.resolve()):
            raise UserError("허용되지 않은 경로입니다.", 403)
        try:
            move_no_overwrite(src, target)
            return target
        except FileExistsError:
            continue
    raise UserError("같은 이름의 파일이 너무 많습니다.", 409)


def managed_dir(root, name):
    d = root / name
    d.mkdir(exist_ok=True)
    return d


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
    ctx["max_upload"] = config.MAX_UPLOAD_SIZE
    ctx["max_upload_text"] = format_size(config.MAX_UPLOAD_SIZE)
    return templates.TemplateResponse(request, "files.html", ctx, status_code=status)


@app.exception_handler(LoginRequired)
def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/", status_code=303)


@app.exception_handler(UserError)
def user_error(request: Request, exc: UserError):
    if exc.status == 403:
        audit(request, "denied", url=request.url.path, query=request.url.query, reason=exc.message)
    if request.url.path == "/upload":
        return JSONResponse({"error": exc.message}, status_code=exc.status)
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
    log.exception("server_error url=%s", request.url.path)
    if not is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    if request.url.path == "/upload":
        return JSONResponse({"error": "알 수 없는 오류가 발생했습니다. 다시 시도해 주세요."}, status_code=500)
    return render_files(request, 500, error="알 수 없는 오류가 발생했습니다. 다시 시도해 주세요.")


@app.get("/")
def index(request: Request):
    if is_logged_in(request):
        return RedirectResponse("/files", status_code=303)
    error = None if config.ENV["MYSSD_USER"] and config.ENV["MYSSD_PASSWORD_HASH"] and SECRET_KEY else (
        "계정이 설정되지 않았습니다. 명령 프롬프트에서 python config.py 를 먼저 실행하세요."
    )
    return templates.TemplateResponse(request, "index.html", {"error": error})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else ""
    now = time.time()
    with LOCK:
        fails, locked_until = FAILS.get(ip, (0, 0))
    if locked_until > now:
        audit(request, "login_locked", user=username[:50])
        minutes = int((locked_until - now) // 60) + 1
        return templates.TemplateResponse(
            request, "index.html", {"error": f"로그인 실패가 반복되어 잠시 차단되었습니다. {minutes}분 후 다시 시도하세요."}, status_code=429
        )
    user_ok = hmac.compare_digest(username.encode(), config.ENV["MYSSD_USER"].encode())
    pass_ok = config.verify_password(password, config.ENV["MYSSD_PASSWORD_HASH"])
    if not (user_ok and pass_ok and config.ENV["MYSSD_USER"] and SECRET_KEY):
        fails += 1
        with LOCK:
            if fails >= config.LOGIN_MAX_FAILS:
                FAILS[ip] = (0, now + config.LOGIN_LOCK_SECONDS)
            else:
                FAILS[ip] = (fails, 0)
        audit(request, "login_fail", user=username[:50], fails=fails)
        time.sleep(1)
        return templates.TemplateResponse(
            request, "index.html", {"error": "아이디 또는 비밀번호가 올바르지 않습니다."}, status_code=401
        )
    sid = secrets.token_urlsafe(32)
    with LOCK:
        FAILS.pop(ip, None)
        for old in [k for k, v in SESSIONS.items() if v <= now]:
            del SESSIONS[old]
        SESSIONS[sid] = now + config.SESSION_HOURS * 3600
    request.session.clear()
    request.session.update(sid=sid, csrf=secrets.token_urlsafe(32))
    audit(request, "login_ok", user=username)
    return RedirectResponse("/files", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if is_logged_in(request):
        check_csrf(request, csrf)
        with LOCK:
            SESSIONS.pop(request.session.get("sid"), None)
        audit(request, "logout")
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


def new_temp_file(root, token):
    """SSD_ROOT/.myssd_temp 안에 임시 파일 경로를 만든다. 1시간 넘게 방치된 임시 파일은 정리."""
    d = managed_dir(root, TEMP_DIR)
    with LOCK:
        active = set(ACTIVE_UPLOADS)
    for f in d.glob("*.part"):
        try:
            if f.stem not in active and time.time() - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:
            pass
    return d / f"{token}.part"


@private.post("/upload")
async def upload(request: Request, path: str = "", name: str = ""):
    """파일 1개를 요청 본문으로 받아 SSD 임시 폴더에 조금씩 쓴 뒤, 끝까지 받은 경우에만 최종 위치로 옮긴다."""
    check_csrf(request, request.headers.get("x-csrf-token", ""))
    root, folder = await run_in_threadpool(open_folder, path)
    name = clean_name(name.replace("\\", "/").rpartition("/")[2])
    expected = int(request.headers.get("content-length", "-1"))
    token = secrets.token_hex(8)
    tmp = None
    out = None
    size = 0
    try:
        with fs_errors("저장"):
            tmp = await run_in_threadpool(new_temp_file, root, token)
            with LOCK:
                ACTIVE_UPLOADS[token] = folder
            out = await run_in_threadpool(open, tmp, "xb")
            buf = bytearray()
            async for chunk in request.stream():
                size += len(chunk)
                if size > config.MAX_UPLOAD_SIZE:
                    raise UserError(f"업로드 최대 크기({format_size(config.MAX_UPLOAD_SIZE)})를 넘었습니다.", 413)
                buf += chunk
                if len(buf) >= CHUNK:
                    await run_in_threadpool(out.write, bytes(buf))
                    buf.clear()
            await run_in_threadpool(out.write, bytes(buf))
            await run_in_threadpool(out.close)
            if size != expected:
                raise UserError("업로드가 끝까지 완료되지 않았습니다. 다시 올려 주세요.", 400)
            target = await run_in_threadpool(move_unique, root, tmp, folder, name, False)
    except ClientDisconnect:
        audit(request, "upload_cancel", folder="/" + to_rel(root, folder), name=name, received=size)
        return JSONResponse({"error": "업로드가 취소되었습니다."}, status_code=400)
    except UserError as e:
        audit(request, "upload_fail", folder="/" + to_rel(root, folder), name=name, reason=e.message)
        raise
    finally:
        if out and not out.closed:
            out.close()
        with LOCK:
            ACTIVE_UPLOADS.pop(token, None)
        if tmp:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    audit(request, "upload", path="/" + to_rel(root, target), size=size)
    flash = request.session.get("flash") or ""
    flash = (flash + ", " if flash.startswith("올린 파일:") else "올린 파일: ") + target.name
    request.session["flash"] = flash if len(flash) < 400 else flash[:400] + "…"
    return JSONResponse({"name": target.name})


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
    audit(request, "mkdir", path="/" + to_rel(root, target))
    return redirect_files(request, path, f"'{name}' 폴더를 만들었습니다.")


def item_info(root, target):
    rel = to_rel(root, target)
    return {"name": target.name, "path": rel, "parent": rel.rpartition("/")[0], "kind": file_kind(target.name, target.is_dir())}


def render_action(request, mode, **ctx):
    ctx.update(mode=mode, csrf=request.session.get("csrf", ""))
    return templates.TemplateResponse(request, "action.html", ctx)


@private.get("/rename")
def rename_page(request: Request, path: str = ""):
    root, src = open_item(path)
    return render_action(request, "rename", item=item_info(root, src))


@private.post("/rename")
def rename(request: Request, path: str = Form(""), name: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    root, src = open_item(path)
    check_not_busy(src)
    new_name = clean_name(name)
    parent = to_rel(root, src.parent)
    if new_name == src.name:
        return redirect_files(request, parent)
    dst = src.parent / new_name
    if not is_inside(root, dst.resolve()):
        raise UserError("허용되지 않은 경로입니다.", 403)
    with fs_errors("이름을 변경"):
        move_no_overwrite(src, dst)
    audit(request, "rename", path="/" + to_rel(root, src), new_name=new_name)
    return redirect_files(request, parent, f"이름을 바꿨습니다: {src.name} → {new_name}")


@private.get("/move")
def move_page(request: Request, path: str = "", dest: str = None):
    root, src = open_item(path)
    _, folder = open_folder(to_rel(root, src.parent) if dest is None else dest)
    try:
        folders = [e for e in list_dir(root, folder) if e["is_dir"] and e["path"] != to_rel(root, src)]
    except OSError:
        raise UserError("폴더를 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    dest_rel = to_rel(root, folder)
    return render_action(
        request, "move", item=item_info(root, src), dest=dest_rel, folders=folders,
        dest_parent=None if folder == root else dest_rel.rpartition("/")[0],
    )


@private.post("/move")
def move(request: Request, path: str = Form(""), dest: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    root, src = open_item(path)
    check_not_busy(src)
    _, folder = open_folder(dest)
    if folder == src or src in folder.parents:
        raise UserError("폴더를 자기 자신이나 그 안의 폴더로 옮길 수 없습니다.")
    if folder == src.parent:
        raise UserError("이미 그 폴더에 있습니다.")
    dst = folder / src.name
    if not is_inside(root, dst.resolve()):
        raise UserError("허용되지 않은 경로입니다.", 403)
    with fs_errors("이동"):
        move_no_overwrite(src, dst)
    audit(request, "move", path="/" + to_rel(root, src), to="/" + to_rel(root, dst))
    return redirect_files(request, to_rel(root, src.parent), f"'{src.name}'을(를) /{to_rel(root, folder)} 로 옮겼습니다.")


@private.post("/delete")
def delete(request: Request, path: str = Form(""), csrf: str = Form("")):
    """영구 삭제하지 않고 SSD_ROOT/.myssd_trash 로 옮긴다. 원래 위치는 <id>.json 에 기록."""
    check_csrf(request, csrf)
    root, src = open_item(path)
    check_not_busy(src)
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    meta = {
        "name": src.name,
        "original_path": to_rel(root, src.parent),
        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "trash_name": f"{tid}/{src.name}",
        "is_dir": src.is_dir(),
    }
    with fs_errors("휴지통으로 이동"):
        trash = managed_dir(root, TRASH_DIR)
        box = trash / tid
        box.mkdir()
        meta_file = trash / f"{tid}.json"
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            move_no_overwrite(src, box / src.name)
        except OSError:
            meta_file.unlink(missing_ok=True)
            box.rmdir()
            raise
    audit(request, "trash", path="/" + to_rel(root, src), trash_id=tid)
    return redirect_files(request, meta["original_path"], f"휴지통으로 옮겼습니다: {src.name} (휴지통에서 복원할 수 있습니다)")


def read_trash(root, tid):
    trash = root / TRASH_DIR
    if not TRASH_ID.match(tid or ""):
        raise UserError("잘못된 휴지통 항목입니다.", 400)
    try:
        meta = json.loads((trash / f"{tid}.json").read_text(encoding="utf-8"))
        name = clean_name(meta["name"])
        if name != meta["name"]:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, UserError):
        raise UserError("휴지통 항목을 찾을 수 없거나 손상되었습니다.", 404)
    meta.update(id=tid, box=trash / tid, item=trash / tid / name, kind=file_kind(name, bool(meta.get("is_dir"))))
    return meta


@private.get("/trash")
def trash_page(request: Request):
    root = get_root()
    items = []
    trash = root / TRASH_DIR
    try:
        names = [f.stem for f in trash.glob("*.json")] if trash.is_dir() else []
    except OSError:
        raise UserError("휴지통을 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    for tid in names:
        try:
            items.append(read_trash(root, tid))
        except UserError:
            continue
    items.sort(key=lambda m: m.get("deleted_at", ""), reverse=True)
    return templates.TemplateResponse(
        request, "trash.html",
        {"items": items, "csrf": request.session.get("csrf", ""), "flash": request.session.pop("flash", None)},
    )


@private.post("/trash/restore")
def trash_restore(request: Request, id: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    root = get_root()
    meta = read_trash(root, id)
    _, parent = safe_path(meta.get("original_path", ""))
    with fs_errors("복원"):
        if not os.path.lexists(meta["item"]):
            raise UserError("휴지통 항목이 손상되었습니다.", 404)
        parent.mkdir(parents=True, exist_ok=True)
        target = move_unique(root, meta["item"], parent, meta["name"], meta["kind"] == "dir")
        (root / TRASH_DIR / f"{id}.json").unlink(missing_ok=True)
    try:
        meta["box"].rmdir()
    except OSError:
        pass
    rel = to_rel(root, target)
    audit(request, "restore", trash_id=id, path="/" + rel)
    note = "" if target.name == meta["name"] else f" (같은 이름이 있어서 '{target.name}'(으)로 복원)"
    request.session["flash"] = f"복원했습니다: /{rel}{note}"
    return RedirectResponse("/trash", status_code=303)


@private.get("/trash/purge")
def purge_page(request: Request, id: str = ""):
    meta = read_trash(get_root(), id)
    return render_action(request, "purge", item=meta)


def _force_remove(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)  # Windows 읽기 전용 파일
    func(path)


@private.post("/trash/purge")
def purge(request: Request, id: str = Form(""), csrf: str = Form(""), confirm: str = Form("")):
    check_csrf(request, csrf)
    if confirm != "yes":
        raise UserError("영구 삭제 확인란을 체크해야 삭제됩니다.")
    root = get_root()
    meta = read_trash(root, id)
    with fs_errors("영구 삭제"):
        shutil.rmtree(meta["box"], onerror=_force_remove)
        (root / TRASH_DIR / f"{id}.json").unlink(missing_ok=True)
    audit(request, "purge", trash_id=id, name=meta["name"], original="/" + meta.get("original_path", ""))
    request.session["flash"] = f"영구 삭제했습니다: {meta['name']}"
    return RedirectResponse("/trash", status_code=303)


app.include_router(private)


async def guard(request: Request, call_next):
    # Tailscale Funnel(인터넷 공개)로 들어온 요청은 항상 거부. MySSD는 Serve(내 기기 전용)만 사용
    if not ip_allowed(request.client.host if request.client else "") or "tailscale-funnel-request" in request.headers:
        log.warning("blocked_network ip=%s url=%s", request.client.host if request.client else "-", request.url.path)
        return PlainTextResponse("허용되지 않은 네트워크입니다. Tailscale에 연결한 뒤 접속하세요.", status_code=403)
    path = request.url.path
    # 요청 본문을 읽기 전에 차단: 로그인 안 한 사용자의 업로드가 임시 파일로 쌓이지 않게 함
    if not (path in PUBLIC_PATHS or path.startswith("/static/") or is_logged_in(request)):
        if path == "/upload" and request.method == "POST":
            return JSONResponse({"error": "로그인이 만료되었습니다. 다시 로그인하세요."}, status_code=401)
        return RedirectResponse("/", status_code=303)
    if request.method == "POST":
        length = request.headers.get("content-length", "")
        limit = config.MAX_UPLOAD_SIZE if path == "/upload" else 64 * 1024
        if not length.isdigit() or int(length) > limit:
            if path == "/upload":
                msg = f"업로드 최대 크기({format_size(config.MAX_UPLOAD_SIZE)})를 넘었습니다."
                return JSONResponse({"error": msg}, status_code=413)
            return PlainTextResponse("요청이 너무 큽니다.", status_code=413)
    return await call_next(request)


class SecureCookieOnHttps:
    """HTTPS(Tailscale Serve)로 들어온 요청에만 세션 쿠키에 Secure 속성을 붙인다. http://127.0.0.1 로그인은 그대로 동작."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("scheme") != "https":
            return await self.app(scope, receive, send)

        async def send_secure(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v + b"; secure" if k.lower() == b"set-cookie" and b"secure" not in v.lower() else v)
                    for k, v in message["headers"]
                ]
            await send(message)

        await self.app(scope, receive, send_secure)


# 나중에 추가한 미들웨어가 바깥쪽: SecureCookie → Session → guard → 앱 순서로 실행
app.add_middleware(BaseHTTPMiddleware, dispatch=guard)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY or secrets.token_urlsafe(32),
    session_cookie="myssd_session",
    max_age=config.SESSION_HOURS * 3600,
    same_site="strict",
)
app.add_middleware(SecureCookieOnHttps)


def tailscale_ip():
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return out[0] if out else None


if __name__ == "__main__":
    windowless = sys.stdout is None  # pythonw.exe(자동 실행)는 콘솔이 없음 → 출력은 logs/console.log
    if windowless:
        sys.stdout = sys.stderr = open(LOG_DIR / "console.log", "a", encoding="utf-8", buffering=1)
    log.info("server_start host=%s port=%s ssd_root=%s", config.HOST, config.PORT, config.SSD_ROOT)
    print("내 SSD 서버 시작 (종료: Ctrl+C)")
    print(f"  이 PC에서:      http://127.0.0.1:{config.PORT}")
    if config.HOST != "127.0.0.1":
        ts = tailscale_ip()
        print(f"  Tailscale 기기: http://{ts}:{config.PORT}" if ts else "  Tailscale IP를 찾지 못했습니다. Tailscale 실행/로그인 상태를 확인하세요.")
    if not SECRET_KEY:
        print("  [경고] .env 설정이 없거나 오래되었습니다. python config.py 를 실행하세요. (로그인 불가)")
    # proxy_headers: 이 PC의 Tailscale Serve(127.0.0.1)가 보낸 X-Forwarded-For/Proto만 신뢰
    uvicorn.run(
        app, host=config.HOST, port=config.PORT, server_header=False, access_log=not windowless,
        proxy_headers=True, forwarded_allow_ips=["127.0.0.1", "::1"],
    )
