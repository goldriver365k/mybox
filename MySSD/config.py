import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

# 외장 SSD 경로. 드라이브 문자가 바뀌면 여기만 수정하세요. (예: "F:\\")
SSD_ROOT = "E:\\"

# (선택, Windows) 외장 SSD의 볼륨 이름. 예: "MYSSD"
# 설정하면 이 이름의 SSD만 사용하고, 드라이브 문자가 바뀌어도(E: → F:) 같은 이름의 드라이브가 딱 하나면 자동으로 찾습니다.
# 같은 이름이 없거나 여러 개면 다른 디스크를 쓰지 않고 오류를 표시합니다.
SSD_VOLUME_LABEL = ""

PORT = 8000

# 서버 수신 주소. "0.0.0.0" = Tailscale 기기에서 접속 가능 (아래 ALLOWED_NETWORKS 로 제한됨)
#                "127.0.0.1" = 이 PC에서만 접속
# 공유기 포트포워딩은 절대 하지 마세요.
HOST = "0.0.0.0"

# 접속을 허용할 주소 범위: 이 PC 자신 + Tailscale 주소만. 같은 와이파이의 다른 기기도 차단됩니다.
ALLOWED_NETWORKS = ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"]

# 한 번에 올릴 수 있는 최대 크기 (기본 20GB). 예: 5GB = 5 * 1024**3
MAX_UPLOAD_SIZE = 20 * 1024**3
# 업로드 중 이 시간(초) 동안 데이터가 전혀 오지 않으면 끊긴 것으로 보고 중단 (임시 파일 삭제)
UPLOAD_IDLE_SECONDS = 120

# 로그인 연속 실패 LOGIN_MAX_FAILS 회 → LOGIN_LOCK_SECONDS 초 동안 로그인 차단 (접속 기기별)
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 300

SESSION_HOURS = 12

# 최근 파일 화면에 보여줄 개수
RECENT_FILES_LIMIT = 50
# 검색 결과 / 사진·동영상 모아보기 최대 표시 개수
SEARCH_RESULTS_LIMIT = 200
MEDIA_LIST_LIMIT = 300
# 전체 검색·최근 파일·사진/동영상 모아보기가 SSD를 훑는 최대 시간(초). 넘으면 찾은 데까지만 표시
SCAN_TIME_LIMIT = 15

# SSD 남은 공간 경고 기준(%). 경고만 하며 파일을 자동으로 지우지 않음
LOW_DISK_WARNING_PERCENT = 15
CRITICAL_DISK_WARNING_PERCENT = 5

# 업로드 후에도 최소한 이만큼(GB)은 비워 둠. 넘으면 업로드를 시작하지 않음
MIN_FREE_SPACE_GB = 20

# 한 번에(또는 MASS_OPERATION_WINDOW_MINUTES 분 동안 합쳐서) 이 개수 이상의 파일을
# 휴지통 이동/이동/이름 변경하면 "대량 파일 작업" 확인을 한 번 더 받음
MASS_OPERATION_THRESHOLD = 100
MASS_OPERATION_WINDOW_MINUTES = 10

# 큰 파일 찾기에 보여줄 개수 / 폴더 화면에 한 번에 보여줄 항목 수 ([더 보기]로 이어서 표시)
LARGE_FILES_LIMIT = 50
FOLDER_PAGE_SIZE = 300

# 사진 썸네일 크기(px)와 썸네일 캐시 최대 크기(SSD_ROOT/.myssd_cache/thumbnails)
THUMB_SIZE = 256
THUMB_CACHE_MAX_MB = 500

# 계정 정보는 .env 파일(또는 환경변수)에서 읽습니다. 만들기: python config.py
ENV_FILE = Path(__file__).parent / ".env"
PBKDF2_ITERATIONS = 600_000


def load_env():
    values = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip()
    return {k: os.environ.get(k, values.get(k, "")) for k in ("MYSSD_USER", "MYSSD_PASSWORD_HASH", "MYSSD_SECRET_KEY")}


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${b64(salt)}${b64(digest)}"


def verify_password(password, stored):
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


ENV = load_env()


if __name__ == "__main__":
    from getpass import getpass

    if ENV_FILE.exists() and input(".env 파일이 이미 있습니다. 덮어쓸까요? (y/N): ").strip().lower() != "y":
        raise SystemExit("취소했습니다.")
    user = input("아이디: ").strip()
    password = getpass("비밀번호 (화면에 표시되지 않음): ")
    if not user or len(password) < 8:
        raise SystemExit("아이디를 입력하고, 비밀번호는 8자 이상으로 정하세요.")
    if password != getpass("비밀번호 확인: "):
        raise SystemExit("비밀번호가 일치하지 않습니다.")
    ENV_FILE.write_text(
        f"MYSSD_USER={user}\nMYSSD_PASSWORD_HASH={hash_password(password)}\nMYSSD_SECRET_KEY={secrets.token_urlsafe(32)}\n",
        encoding="utf-8",
    )
    print(f"{ENV_FILE} 파일을 만들었습니다. 이제 python server.py 로 실행하세요.")
