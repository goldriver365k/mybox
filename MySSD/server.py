import hmac
import ipaddress
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

import config

BASE_DIR = Path(__file__).parent
HIDDEN = {"system volume information", "$recycle.bin"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
BAD_CHARS = set('<>:"/\\|?*')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
TEMP_PREFIX = ".myssd-upload-"
PUBLIC_PATHS = {"/", "/login", "/logout"}
ALLOWED_NETWORKS = [ipaddress.ip_network(n) for n in config.ALLOWED_NETWORKS]
SECRET_KEY = config.ENV["MYSSD_SECRET_KEY"]
if len(SECRET_KEY) < 32:
    SECRET_KEY = None

# 로그인 세션과 실패 횟수는 메모리에만 보관 (서버를 재시작하면 모두 다시 로그인)
SESSIONS = {}  # sid -> 만료 시각
FAILS = {}  # 접속 IP -> (연속 실패 횟수, 차단 해제 시각)
LOCK = threading.Lock()

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
            if e.name.lower() in HIDDEN or e.name.startswith(TEMP_PREFIX):
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
    return RedirectResponse("/files", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if is_logged_in(request):
        check_csrf(request, csrf)
        with LOCK:
            SESSIONS.pop(request.session.get("sid"), None)
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


def claim_name(tmp, target):
    """임시 파일을 최종 이름으로 확정. 같은 이름이 이미 있으면 FileExistsError (덮어쓰지 않음)."""
    if os.name == "nt":
        os.rename(tmp, target)  # Windows: 대상이 있으면 실패
    else:
        os.link(tmp, target)  # 대상이 있으면 실패. 임시 파일은 finally에서 삭제


def save_upload(root, folder, up, remaining):
    """숨김 임시 파일(.part)에 끝까지 쓴 뒤에만 실제 이름으로 바꾼다. 중간 실패 시 임시 파일 삭제."""
    name = clean_name((up.filename or "").replace("\\", "/").rpartition("/")[2])
    stem, ext = os.path.splitext(name)
    tmp = folder / f"{TEMP_PREFIX}{secrets.token_hex(8)}.part"
    size = 0
    try:
        with open(tmp, "xb") as out:
            while chunk := up.file.read(1024 * 1024):
                size += len(chunk)
                if size > remaining:
                    raise UserError(f"업로드 최대 크기({format_size(config.MAX_UPLOAD_SIZE)})를 넘었습니다.", 413)
                out.write(chunk)
        for n in range(1000):
            target = folder / (name if n == 0 else f"{stem} ({n}){ext}")
            if not is_inside(root, target.resolve()):
                raise UserError("허용되지 않은 경로입니다.", 403)
            try:
                claim_name(tmp, target)
                return target.name, size
            except FileExistsError:
                continue
        raise UserError("같은 이름의 파일이 너무 많습니다.", 409)
    except PermissionError:
        raise UserError("이 폴더에 저장할 권한이 없습니다.", 403)
    except OSError:
        raise UserError("파일 저장 중 오류가 발생했습니다. SSD 연결 상태와 남은 공간을 확인하세요.", 503)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@private.post("/upload")
def upload(request: Request, path: str = Form(""), csrf: str = Form(""), files: list[UploadFile] = File(...)):
    check_csrf(request, csrf)
    root, folder = open_folder(path)
    files = [f for f in files if f.filename]
    if not files:
        raise UserError("올릴 파일을 선택하세요.")
    saved = []
    remaining = config.MAX_UPLOAD_SIZE
    for up in files:
        name, size = save_upload(root, folder, up, remaining)
        remaining -= size
        saved.append(name)
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


async def guard(request: Request, call_next):
    if not ip_allowed(request.client.host if request.client else ""):
        return PlainTextResponse("허용되지 않은 네트워크입니다. Tailscale에 연결한 뒤 접속하세요.", status_code=403)
    path = request.url.path
    # 요청 본문을 읽기 전에 차단: 로그인 안 한 사용자의 업로드가 임시 파일로 쌓이지 않게 함
    if not (path in PUBLIC_PATHS or path.startswith("/static/") or is_logged_in(request)):
        return RedirectResponse("/", status_code=303)
    if request.method == "POST":
        length = request.headers.get("content-length", "")
        limit = config.MAX_UPLOAD_SIZE + 1024 * 1024 if path == "/upload" else 64 * 1024
        if not length.isdigit() or int(length) > limit:
            if path == "/upload":
                msg = f"업로드 최대 크기({format_size(config.MAX_UPLOAD_SIZE)})를 넘었습니다."
                return render_files(request, 413, error=msg)
            return PlainTextResponse("요청이 너무 큽니다.", status_code=413)
    return await call_next(request)


# 나중에 추가한 미들웨어가 바깥쪽: Session → guard → 앱 순서로 실행
app.add_middleware(BaseHTTPMiddleware, dispatch=guard)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY or secrets.token_urlsafe(32),
    session_cookie="myssd_session",
    max_age=config.SESSION_HOURS * 3600,
    same_site="strict",
)


def tailscale_ip():
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return out[0] if out else None


if __name__ == "__main__":
    print("내 SSD 서버 시작 (종료: Ctrl+C)")
    print(f"  이 PC에서:      http://127.0.0.1:{config.PORT}")
    if config.HOST != "127.0.0.1":
        ts = tailscale_ip()
        print(f"  Tailscale 기기: http://{ts}:{config.PORT}" if ts else "  Tailscale IP를 찾지 못했습니다. Tailscale 실행/로그인 상태를 확인하세요.")
    if not SECRET_KEY:
        print("  [경고] .env 설정이 없거나 오래되었습니다. python config.py 를 실행하세요. (로그인 불가)")
    uvicorn.run(app, host=config.HOST, port=config.PORT, proxy_headers=False, server_header=False)
