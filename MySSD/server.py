import hashlib
import heapq
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
import unicodedata
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

try:
    from PIL import Image, ImageOps  # 사진 썸네일·크기 (없으면 아이콘으로 대체)
except ImportError:
    Image = None

BASE_DIR = Path(__file__).parent
HIDDEN = {"system volume information", "$recycle.bin"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
BAD_CHARS = set('<>:"/\\|?*')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
MANAGED_PREFIX = ".myssd"  # .myssd_temp, .myssd_trash 등 MySSD 내부 관리 폴더. 웹에서 접근 불가
TEMP_DIR = ".myssd_temp"
TRASH_DIR = ".myssd_trash"
META_DIR = ".myssd_meta"  # favorites.json
THUMB_DIR = ".myssd_cache/thumbnails"
SORTS = {"name": "이름순", "new": "최근 수정순", "old": "오래된순", "size": "파일 크기순", "type": "파일 종류순"}
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
FAV_LOCK = threading.Lock()
SELECTIONS = {}  # 여러 항목 이동용 선택 목록: id -> (만료 시각, 원래 폴더, [경로...])
THUMB_COUNT = [0]

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
templates.env.filters["size"] = lambda n: human_size(n)
templates.env.filters["date"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


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
                st = e.stat()
            except OSError:
                continue
            rel = Path(e.path).relative_to(root).as_posix()
            entries.append(make_entry(e.name, rel, is_dir, st))
    return sort_entries(entries, "name")


def make_entry(name, rel, is_dir, st):
    return {
        "name": name, "path": rel, "is_dir": is_dir, "kind": file_kind(name, is_dir),
        "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime, "parent": rel.rpartition("/")[0],
    }


def sort_entries(entries, sort):
    """폴더 먼저, 그 안에서 선택한 기준으로 정렬. 기본은 이름순."""
    name = lambda e: e["name"].lower()
    keys = {
        "name": lambda e: name(e),
        "new": lambda e: -e["mtime"],
        "old": lambda e: e["mtime"],
        "size": lambda e: (-e["size"], name(e)),
        "type": lambda e: (os.path.splitext(e["name"])[1].lower(), name(e)),
    }
    key = keys.get(sort, keys["name"])
    return sorted(entries, key=lambda e: (not e["is_dir"], key(e)))


def scan_ssd(root, state):
    """SSD_ROOT 전체를 훑으며 (이름, 상대경로, 폴더여부, stat) 를 돌려준다.
    관리 폴더·시스템 폴더·링크/정션은 건너뛰고, 읽을 수 없는 폴더는 무시한다.
    SCAN_TIME_LIMIT 을 넘기면 멈추고 state["partial"]=True.
    (파일이 아주 많아지면 이 함수만 인덱스 조회로 바꾸면 검색/최근/모아보기가 모두 따라간다)"""
    deadline = time.monotonic() + config.SCAN_TIME_LIMIT
    stack = [root]
    while stack:
        if time.monotonic() > deadline:
            state["partial"] = True
            return
        folder = stack.pop()
        try:
            it = os.scandir(folder)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.lower() in HIDDEN or is_managed(e.name):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    st = e.stat(follow_symlinks=False)
                    is_dir = stat.S_ISDIR(st.st_mode)
                    if is_dir and getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        continue  # Windows 정션: SSD 밖을 가리킬 수 있으므로 따라가지 않음
                except (OSError, AttributeError):
                    continue
                yield e.name, Path(e.path).relative_to(root).as_posix(), is_dir, st
                if is_dir:
                    stack.append(e.path)


def fold(text):
    """한글(NFC/NFD)·대소문자 차이를 무시한 비교용 문자열"""
    return unicodedata.normalize("NFC", text).casefold()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


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
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_favorites(root):
    try:
        data = json.loads((root / META_DIR / "favorites.json").read_text(encoding="utf-8"))
        return [p for p in data.get("paths", []) if isinstance(p, str)]
    except (OSError, ValueError, AttributeError):
        return []


def update_favorites(root, change):
    """favorites.json 을 읽어 change(목록)->새 목록 을 적용하고 원자적으로 저장. 경로만 저장(파일 복사 없음)."""
    with FAV_LOCK:
        paths = load_favorites(root)
        new = change(list(paths))
        if new == paths:
            return
        d = managed_dir(root, META_DIR)
        tmp = d / "favorites.json.tmp"
        tmp.write_text(json.dumps({"paths": new}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "favorites.json")


def under(rel, base):
    return rel == base or rel.startswith(base + "/")


def favorites_moved(root, old, new):
    try:
        update_favorites(root, lambda ps: [new + p[len(old):] if under(p, old) else p for p in ps])
    except OSError:
        log.exception("favorites update failed")


def favorites_removed(root, rel):
    try:
        update_favorites(root, lambda ps: [p for p in ps if not under(p, rel)])
    except OSError:
        log.exception("favorites update failed")


def thumb_paths(root, rel, st):
    key = hashlib.sha1(f"{rel}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()
    d = root / THUMB_DIR
    return d / f"{key}.jpg", d / f"{key}.fail"


def drop_thumb(root, target):
    """원본이 휴지통으로 가거나 이름/위치가 바뀌면 그 썸네일 캐시만 지운다 (원본은 건드리지 않음)."""
    try:
        if target.is_file() and file_kind(target.name) == "image":
            for f in thumb_paths(root, to_rel(root, target), target.stat()):
                f.unlink(missing_ok=True)
    except OSError:
        pass


def make_thumb(src, out):
    with Image.open(src) as im:
        if im.width * im.height > 100_000_000:
            raise ValueError("too large")
        im.draft("RGB", (config.THUMB_SIZE * 2, config.THUMB_SIZE * 2))  # JPEG는 디코딩 단계에서 축소 (빠르고 메모리 적게)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((config.THUMB_SIZE, config.THUMB_SIZE))
        if im.mode != "RGB":
            rgba = im.convert("RGBA")
            im = Image.new("RGB", rgba.size, "white")
            im.paste(rgba, mask=rgba.split()[3])
        tmp = out.with_name(out.stem + f".{secrets.token_hex(4)}.tmp")
        im.save(tmp, "JPEG", quality=80)
        os.replace(tmp, out)


def trim_thumb_cache(d):
    """캐시가 THUMB_CACHE_MAX_MB 를 넘으면 오래 안 쓴 썸네일부터 80% 까지 지운다. 캐시 폴더 안만 다룬다."""
    try:
        files = [(f.stat().st_mtime, f.stat().st_size, f) for f in d.iterdir() if f.is_file()]
    except OSError:
        return
    limit = config.THUMB_CACHE_MAX_MB * 1024 * 1024
    total = sum(size for _, size, _ in files)
    if total <= limit:
        return
    for _, size, f in sorted(files):
        try:
            f.unlink()
        except OSError:
            continue
        total -= size
        if total <= limit * 0.8:
            break


def redirect_files(request, path, message=None):
    if message:
        request.session["flash"] = message
    return RedirectResponse(f"/files?path={quote(path)}", status_code=303)


def page(request, template, status=200, **ctx):
    ctx["csrf"] = request.session.get("csrf", "")
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, template, ctx, status_code=status)


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


def disk_info(root):
    try:
        u = shutil.disk_usage(root)
    except OSError:
        return None
    free_pct = u.free * 100 / u.total if u.total else 0
    return {
        "total": human_size(u.total), "used": human_size(u.used), "free": human_size(u.free),
        "pct": round(u.used * 100 / u.total) if u.total else 0, "low": free_pct <= config.LOW_DISK_WARNING_PERCENT,
        "warn_pct": config.LOW_DISK_WARNING_PERCENT,
    }


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
def files(request: Request, path: str = "", sort: str = "", view: str = ""):
    if sort in SORTS:
        request.session["sort"] = sort
    if view in ("list", "grid"):
        request.session["view"] = view
    sort = request.session.get("sort", "name")
    root, folder = open_folder(path)
    try:
        entries = sort_entries(list_dir(root, folder), sort)
    except PermissionError:
        raise UserError("이 폴더에 접근할 권한이 없습니다.", 403)
    except OSError:
        raise UserError("폴더를 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    rel = "" if folder == root else folder.relative_to(root).as_posix()
    parent = None if not rel else rel.rpartition("/")[0]
    return render_files(
        request, current="/" + rel, parent=parent, entries=entries, path=rel, sort=sort, sorts=SORTS,
        view=request.session.get("view", "list"), favorites=set(load_favorites(root)),
        disk=disk_info(root) if not rel else None,
    )


def results_page(request, title, entries, **ctx):
    root = get_root()
    return page(request, "results.html", title=title, entries=entries, favorites=set(load_favorites(root)), **ctx)


@private.get("/search")
def search(request: Request, q: str = ""):
    """파일명/폴더명만 검색 (내용 검색 안 함). SSD_ROOT 안, 관리 폴더 제외."""
    q = q.strip()[:100]
    if not q:
        return results_page(request, "검색", [], q=q)
    root = get_root()
    needle, state, found = fold(q), {}, []
    for name, rel, is_dir, st in scan_ssd(root, state):
        if needle in fold(name):
            found.append(make_entry(name, rel, is_dir, st))
            if len(found) >= config.SEARCH_RESULTS_LIMIT:
                state["partial"] = True
                break
    found.sort(key=lambda e: (not fold(e["name"]).startswith(needle), not e["is_dir"], e["name"].lower()))
    return results_page(request, f"'{q}' 검색 결과", found, q=q, partial=state.get("partial"), show_location=True)


def newest_files(root, limit, kinds=None):
    """SSD 전체에서 수정 시간이 최근인 파일 limit 개 (kinds 로 종류 제한)."""
    state, heap = {}, []
    for name, rel, is_dir, st in scan_ssd(root, state):
        if is_dir or (kinds and file_kind(name) not in kinds):
            continue
        item = (st.st_mtime, rel, name, st)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item[0] > heap[0][0]:
            heapq.heapreplace(heap, item)
    entries = [make_entry(name, rel, False, st) for _, rel, name, st in sorted(heap, reverse=True)]
    return entries, state.get("partial")


def day_label(t):
    today = time.localtime()
    d = time.localtime(t)
    days = (time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(d[:3] + (0, 0, 0, 0, 0, -1))) // 86400
    return "오늘" if days <= 0 else "어제" if days == 1 else time.strftime("%Y-%m-%d", d)


@private.get("/recent")
def recent(request: Request):
    entries, partial = newest_files(get_root(), config.RECENT_FILES_LIMIT)
    for e in entries:
        e["group"] = day_label(e["mtime"])
    return results_page(request, "최근 파일", entries, partial=partial, show_location=True, grouped=True)


@private.get("/media")
def media(request: Request, kind: str = "image"):
    kind = "video" if kind == "video" else "image"
    entries, partial = newest_files(get_root(), config.MEDIA_LIST_LIMIT, {kind})
    title = "사진" if kind == "image" else "동영상"
    return results_page(request, title, entries, partial=partial, show_location=True, grid=kind == "image")


@private.get("/favorites")
def favorites(request: Request):
    root = get_root()
    entries = []
    for rel in load_favorites(root):
        try:
            _, target = safe_path(rel)
            st = target.stat()
            entries.append(make_entry(target.name, rel, target.is_dir(), st))
        except (UserError, OSError):
            entries.append({"name": rel.rpartition("/")[2] or rel, "path": rel, "missing": True, "kind": "file",
                            "parent": rel.rpartition("/")[0], "is_dir": False, "size": 0, "mtime": 0})
    return results_page(request, "즐겨찾기", entries, show_location=True, fav_page=True)


def safe_next(url):
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else "/files"


@private.post("/favorite")
def favorite(request: Request, path: str = Form(""), csrf: str = Form(""), next: str = Form("/files")):
    check_csrf(request, csrf)
    root = get_root()
    rel = path.replace("\\", "/").strip("/")
    current = load_favorites(root)
    if rel in current:
        update_favorites(root, lambda ps: [p for p in ps if p != rel])  # 원본이 없어진 항목도 해제 가능
        audit(request, "favorite_off", path="/" + rel)
    else:
        _, target = open_item(path)
        rel = to_rel(root, target)
        update_favorites(root, lambda ps: ps if rel in ps else ps + [rel])
        audit(request, "favorite_on", path="/" + rel)
    return RedirectResponse(safe_next(next), status_code=303)


@private.get("/thumb")
def thumb(path: str = ""):
    """목록용 작은 썸네일. SSD_ROOT/.myssd_cache/thumbnails 에 캐시. 원본은 읽기만 한다."""
    target = open_file(path)
    if Image is None or file_kind(target.name) != "image":
        raise UserError("썸네일이 없습니다.", 404)
    root = get_root()
    rel = to_rel(root, target)
    out, fail = thumb_paths(root, rel, target.stat())
    headers = {"Cache-Control": "private, max-age=86400"}
    if out.exists():
        try:
            os.utime(out)  # 최근 사용 표시 (캐시 정리 순서용)
        except OSError:
            pass
        return FileResponse(out, media_type="image/jpeg", headers=headers)
    if fail.exists():
        raise UserError("썸네일을 만들 수 없는 사진입니다.", 404)
    try:
        managed_dir(root, THUMB_DIR)
        make_thumb(target, out)
    except Exception:
        log.info("thumb_fail path=%r", "/" + rel)
        try:
            fail.touch()
        except OSError:
            pass
        raise UserError("썸네일을 만들 수 없는 사진입니다.", 404)
    THUMB_COUNT[0] += 1
    if THUMB_COUNT[0] % 50 == 0:
        trim_thumb_cache(out.parent)
    return FileResponse(out, media_type="image/jpeg", headers=headers)


@private.get("/info")
def info(request: Request, path: str = ""):
    root, target = open_item(path)
    with fs_errors("정보를 읽"):
        st = target.stat()
    item = item_info(root, target)
    item.update(size=st.st_size, mtime=st.st_mtime, is_dir=target.is_dir(), ext=target.suffix.lower().lstrip("."))
    if item["kind"] == "image" and Image is not None:
        try:
            with Image.open(target) as im:
                item["dims"] = f"{im.width} × {im.height}"
        except Exception:
            pass
    if item["is_dir"]:
        try:
            item["children"] = sum(1 for e in os.scandir(target) if not is_managed(e.name))
        except OSError:
            pass
    return render_action(request, "info", item=item, favorite=item["path"] in load_favorites(root))


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
    root = get_root()
    rel = to_rel(root, target)
    prev = nxt = None
    if kind == "image":
        try:
            images = [e["path"] for e in list_dir(root, target.parent) if e["kind"] == "image"]
            i = images.index(rel)
            prev = images[i - 1] if i > 0 else None
            nxt = images[i + 1] if i + 1 < len(images) else None
        except (OSError, ValueError):
            pass
    return page(
        request, "view.html", name=target.name, path=rel, parent=rel.rpartition("/")[0], kind=kind,
        prev=prev, next=nxt, favorite=rel in load_favorites(root),
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
    return page(request, "action.html", mode=mode, **ctx)


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
    drop_thumb(root, src)
    with fs_errors("이름을 변경"):
        move_no_overwrite(src, dst)
    favorites_moved(root, to_rel(root, src), to_rel(root, dst))
    audit(request, "rename", path="/" + to_rel(root, src), new_name=new_name)
    return redirect_files(request, parent, f"이름을 바꿨습니다: {src.name} → {new_name}")


def get_selection(sel):
    now = time.time()
    with LOCK:
        for k in [k for k, v in SELECTIONS.items() if v[0] < now]:
            del SELECTIONS[k]
        item = SELECTIONS.get(sel)
    if not item:
        raise UserError("선택이 만료되었습니다. 다시 선택하세요.", 400)
    return item[1], item[2]


@private.get("/move")
def move_page(request: Request, path: str = "", dest: str = None, sel: str = ""):
    if sel:
        origin, paths = get_selection(sel)
        root = get_root()
        items = [item_info(root, src) for src in (open_item(p)[1] for p in paths)]
        start = origin
    else:
        root, src = open_item(path)
        items = [item_info(root, src)]
        start = to_rel(root, src.parent)
    _, folder = open_folder(start if dest is None else dest)
    skip = {i["path"] for i in items}
    try:
        folders = [e for e in list_dir(root, folder) if e["is_dir"] and e["path"] not in skip]
    except OSError:
        raise UserError("폴더를 읽을 수 없습니다. SSD 연결 상태를 확인하세요.", 503)
    dest_rel = to_rel(root, folder)
    return render_action(
        request, "move", item=items[0], items=items, sel=sel, dest=dest_rel, folders=folders,
        dest_parent=None if folder == root else dest_rel.rpartition("/")[0],
    )


def move_item(root, src, folder):
    """src 를 folder 안으로 이동 (SSD 안 rename, 덮어쓰기 없음). 문제가 있으면 UserError."""
    check_not_busy(src)
    if folder == src or src in folder.parents:
        raise UserError("폴더를 자기 자신이나 그 안의 폴더로 옮길 수 없습니다.")
    if folder == src.parent:
        raise UserError("이미 그 폴더에 있습니다.")
    dst = folder / src.name
    if not is_inside(root, dst.resolve()):
        raise UserError("허용되지 않은 경로입니다.", 403)
    drop_thumb(root, src)
    with fs_errors("이동"):
        move_no_overwrite(src, dst)
    favorites_moved(root, to_rel(root, src), to_rel(root, dst))
    return dst


@private.post("/move")
def move(request: Request, path: str = Form(""), dest: str = Form(""), csrf: str = Form(""), sel: str = Form("")):
    check_csrf(request, csrf)
    if not sel:
        root, src = open_item(path)
        _, folder = open_folder(dest)
        dst = move_item(root, src, folder)
        audit(request, "move", path="/" + to_rel(root, src), to="/" + to_rel(root, dst))
        return redirect_files(request, to_rel(root, src.parent), f"'{src.name}'을(를) /{to_rel(root, folder)} 로 옮겼습니다.")
    origin, paths = get_selection(sel)
    root, folder = open_folder(dest)
    moved, failed = 0, []
    for p in paths:
        try:
            _, src = open_item(p)
            dst = move_item(root, src, folder)
            audit(request, "move", path="/" + p, to="/" + to_rel(root, dst))
            moved += 1
        except UserError as e:
            failed.append(f"{p.rpartition('/')[2]}({e.message})")
    with LOCK:
        SELECTIONS.pop(sel, None)
    msg = f"{moved}개 항목을 /{to_rel(root, folder)} 로 옮겼습니다."
    if failed:
        msg += f" 옮기지 못한 항목 {len(failed)}개: " + ", ".join(failed)
    return redirect_files(request, origin, msg[:1500])


@private.post("/delete")
def delete(request: Request, path: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    root, src = open_item(path)
    meta = trash_item(root, src)
    audit(request, "trash", path="/" + to_rel(root, src), trash_id=meta["id"])
    return redirect_files(request, meta["original_path"], f"휴지통으로 옮겼습니다: {src.name} (휴지통에서 복원할 수 있습니다)")


@private.post("/bulk")
def bulk(request: Request, action: str = Form(""), paths: list[str] = Form([]), path: str = Form(""), csrf: str = Form("")):
    """여러 항목 선택 후 [이동] / [휴지통]. 휴지통도 영구 삭제가 아니라 .myssd_trash 로 이동."""
    check_csrf(request, csrf)
    root, _ = open_folder(path)
    paths = list(dict.fromkeys(p for p in paths if p))[:1000]
    if not paths:
        return redirect_files(request, path, "선택한 항목이 없습니다.")
    if action == "move":
        sel = secrets.token_urlsafe(12)
        with LOCK:
            SELECTIONS[sel] = (time.time() + 3600, path, paths)
        return RedirectResponse(f"/move?sel={sel}", status_code=303)
    if action != "trash":
        raise UserError("잘못된 요청입니다.")
    done, failed = 0, []
    for p in paths:
        try:
            _, src = open_item(p)
            meta = trash_item(root, src)
            audit(request, "trash", path="/" + p, trash_id=meta["id"])
            done += 1
        except UserError as e:
            failed.append(f"{p.rpartition('/')[2]}({e.message})")
    msg = f"{done}개 항목을 휴지통으로 옮겼습니다. (휴지통에서 복원할 수 있습니다)"
    if failed:
        msg += f" 옮기지 못한 항목 {len(failed)}개: " + ", ".join(failed)
    return redirect_files(request, path, msg[:1500])


def trash_item(root, src):
    """영구 삭제하지 않고 SSD_ROOT/.myssd_trash 로 옮긴다. 원래 위치는 <id>.json 에 기록."""
    check_not_busy(src)
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    meta = {
        "name": src.name,
        "original_path": to_rel(root, src.parent),
        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "trash_name": f"{tid}/{src.name}",
        "is_dir": src.is_dir(),
    }
    drop_thumb(root, src)
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
    favorites_removed(root, to_rel(root, src))
    meta["id"] = tid
    return meta


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
    return page(request, "trash.html", items=items)


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
        limit = config.MAX_UPLOAD_SIZE if path == "/upload" else 512 * 1024
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
