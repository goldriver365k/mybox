# MySSD — 외장 SSD 개인 클라우드

Windows PC에 USB로 연결한 외장 SSD를 **PC와 스마트폰 브라우저**에서 보고, 올리고, 내려받는 개인 파일 서버입니다.

```
외장 SSD ─ Windows PC (MySSD 서버) ─ Tailscale 암호화 네트워크 ─ 외부 PC / 노트북 / 스마트폰
```

- 파일은 **외장 SSD에만** 있습니다. 클라우드·Firebase·외부 DB를 쓰지 않습니다.
- 보안은 두 겹입니다: **① Tailscale에 등록된 내 기기만 접속** + **② MySSD 로그인**.
- 공유기 포트포워딩·공인 IP 공개는 하지 않습니다.
- **백업 기능은 없습니다.** 지금은 SSD가 1개라서 같은 SSD 안에 복사본을 만들어 "백업"이라고 부르지 않습니다
  (SSD가 고장 나면 복사본도 함께 사라지기 때문). 실수로 지운 파일은 **휴지통**에서 복원할 수 있습니다.

목차: [1 Python 설치](#1-python-설치) · [2 프로젝트 설치](#2-프로젝트-설치) · [3 .env 설정](#3-env-설정-계정) ·
[4 SSD 설정](#4-ssd-설정) · [5 실행](#5-실행) · [6 Tailscale](#6-tailscale-설치) · [7 HTTPS](#7-https-설정-tailscale-serve) ·
[8 자동 실행](#8-windows-자동-실행) · [9 외부 PC](#9-외부-pc에서-접속) · [10 스마트폰](#10-스마트폰에서-접속) ·
[11 진단](#11-문제가-생겼을-때-진단) · [12 서버 중지](#12-서버-중지) · [13 자동 실행 해제](#13-자동-실행-해제) ·
[새 PC로 옮기기](#새-pc로-옮기기-복구용) · [기능 안내](#기능-안내) · [설정](#설정-configpy) · [보안](#보안)

---

## 1. Python 설치

1. https://www.python.org/downloads/ 에서 Windows용 Python 3.11 이상을 받습니다.
2. 설치 첫 화면에서 **"Add python.exe to PATH"** 를 반드시 체크한 뒤 설치합니다.
3. 확인: 명령 프롬프트(cmd)에서 `python --version` → 버전이 나오면 성공.

## 2. 프로젝트 설치

1. GitHub에서 프로그램을 받습니다 (Code → Download ZIP 후 압축 풀기, 또는 `git clone`).
   `MySSD` 폴더를 **C 드라이브 등 PC 안의 폴더**에 둡니다 (외장 SSD가 아니어도 됩니다).
2. `MySSD` 폴더에서 명령 프롬프트를 엽니다 (탐색기 주소창에 `cmd` 입력 후 Enter).
3. 필요한 프로그램 설치:
   ```
   pip install -r requirements.txt
   ```
   | 패키지 | 용도 |
   |---|---|
   | fastapi, uvicorn | 웹 서버 |
   | jinja2 | 화면(HTML) 만들기 |
   | itsdangerous | 로그인 쿠키 서명 |
   | python-multipart | 로그인·폼 입력 받기 |
   | Pillow | 사진 썸네일 (없어도 서버는 동작, 사진은 아이콘으로 표시) |

## 3. .env 설정 (계정)

```
python config.py
```

아이디와 비밀번호(8자 이상)를 입력하면 `MySSD\.env` 파일이 만들어집니다.

- `.env`에는 비밀번호 **원문이 아니라 해시값**과 세션 비밀키(랜덤 32바이트)가 저장됩니다.
- `.env`는 다른 사람에게 보내거나 GitHub에 올리지 마세요 (`.gitignore`로 제외되어 있습니다).
- 비밀번호 변경: `python config.py`를 다시 실행하고 서버를 재시작합니다.

## 4. SSD 설정

`config.py`를 메모장으로 열어 외장 SSD 경로를 확인합니다. 기본값은 `E:\` 입니다.

```python
SSD_ROOT = "E:\\"          # F 드라이브라면 "F:\\"
```

**(권장) 볼륨 이름으로 SSD 찾기** — USB를 다시 꽂을 때 E: 가 F: 로 바뀌어도 올바른 SSD를 찾습니다.

1. 탐색기 → 내 PC → 외장 SSD 오른쪽 클릭 → **이름 바꾸기** → `MYSSD`
2. `config.py`: `SSD_VOLUME_LABEL = "MYSSD"`
3. 서버 재시작

같은 이름의 드라이브가 없거나 2개 이상이면, 다른 디스크를 쓰지 않고 "외장 SSD 연결이 끊어졌습니다"를 표시합니다.
다른 USB 저장장치에 같은 이름을 붙이지 마세요.

## 5. 실행

```
python server.py
```

창에 SSD 연결 여부와 접속 주소가 표시됩니다. 이 PC의 브라우저에서 **http://127.0.0.1:8000** 에 접속해 로그인합니다.

- SSD가 아직 연결되지 않았어도 서버는 시작됩니다. SSD를 연결한 뒤 새로고침하면 됩니다.
- 처음 실행할 때 "Windows 보안 경고" 창이 뜨면 **"개인 네트워크"만 체크** → [액세스 허용] (공용 네트워크는 체크하지 않음).

## 6. Tailscale 설치

Tailscale은 내 기기끼리만 암호화 터널로 연결하는 무료 VPN입니다 (개인 사용은 무료).

1. 메인 PC(SSD가 연결된 PC)에 https://tailscale.com/download 에서 Windows용 Tailscale 설치
2. 작업 표시줄 Tailscale 아이콘 → **Log in** → 본인 계정으로 로그인
3. 권장: Tailscale 메뉴 → Preferences → **Run unattended** 켜기 (Windows 로그인 전에도 연결 유지)
4. 권장: Windows 전원 설정에서 **절전 모드 "안 함"** (PC가 잠들면 외부 접속 불가)
5. (선택) 방화벽 범위 좁히기 — 관리자 PowerShell에서:
   ```powershell
   Get-NetFirewallRule -Direction Inbound | Where-Object { ($_ | Get-NetFirewallApplicationFilter).Program -like "*python*" } | Set-NetFirewallRule -RemoteAddress 100.64.0.0/10
   ```
   이 설정을 안 해도 MySSD 자체가 이 PC와 Tailscale 주소(100.64.0.0/10) 외의 접속은 모두 거부합니다.

Tailscale이 꺼져 있어도 MySSD는 이 PC(http://127.0.0.1:8000)에서 계속 사용할 수 있습니다. 외부 접속만 안 됩니다.
**공유기 포트포워딩은 절대 하지 마세요.**

## 7. HTTPS 설정 (Tailscale Serve)

```
외부 기기 → https://내-pc-이름.tailXXXX.ts.net → Tailscale Serve (메인 PC) → http://127.0.0.1:8000 (MySSD)
```

인증서는 Tailscale이 자동으로 발급·갱신합니다. **Serve**는 내 Tailscale 기기에만 공개합니다.
인터넷 전체에 공개하는 **Funnel은 절대 켜지 마세요** (켜져 있어도 MySSD는 Funnel 요청을 거부합니다).

1. https://login.tailscale.com/admin/dns 에서 **MagicDNS** 켜짐 확인 → **HTTPS Certificates → Enable HTTPS**
2. MySSD 서버를 실행한 상태에서 메인 PC 명령 프롬프트:
   ```
   tailscale serve --bg 8000
   ```
   (재부팅 후에도 유지됩니다. 오류가 나면 명령 프롬프트를 관리자 권한으로 실행)
3. 확인:
   ```
   tailscale serve status
   ```
   `https://내-pc-이름.tailXXXX.ts.net (tailnet only)` 와 `proxy http://127.0.0.1:8000` 이 보이면 정상입니다.
   `Funnel on` 이 보이면 안 됩니다.
4. MySSD → 🩺 시스템 상태 → **외부 접속**에 HTTPS 주소와 **[접속주소 복사]** 버튼이 나옵니다
   (Tailscale에서 실제로 확인한 주소만 표시).
5. (권장) HTTPS로 접속이 확인되면 `config.py`의 `HOST = "127.0.0.1"` 로 바꾸고 재시작 → 외부 접속은 HTTPS 경로만 남습니다.

HTTPS를 끄려면: `tailscale serve --https=443 off`

## 8. Windows 자동 실행

`MySSD` 폴더의 **`autostart_install.bat`** 를 더블클릭합니다 (관리자 권한 불필요).

- Windows **로그인 시** MySSD가 **창 없이**(`pythonw.exe`) 자동으로 시작됩니다.
  현재 사용자의 "시작프로그램" 폴더에 바로가기를 하나 만들 뿐, 다른 Windows 설정은 바꾸지 않습니다.
- 재부팅 순서: Windows 시작 → 로그인 → MySSD 시작 → SSD 확인(늦게 인식되어도 서버는 계속 실행) → Tailscale(서비스로 자동 시작) → 외부 접속 가능.
- 서버 메시지는 `MySSD\logs\console.log`, 작업 기록은 `MySSD\logs\` 에 남습니다.
- 확인: `Win + R` → `shell:startup` → `MySSD` 바로가기가 있으면 등록된 상태. 🩺 시스템 상태의 자동실행도 "설정됨".
- Tailscale 자동 시작 확인: `Win + R` → `services.msc` → **Tailscale** 이 "자동 / 실행 중".
- 자리를 비운 채 재부팅해도 쓰려면 Windows 자동 로그인을 설정하거나, 로그인 후 화면 잠금만 하세요.

## 9. 외부 PC에서 접속

1. 외부 PC에 Tailscale 설치 → **메인 PC와 같은 계정**으로 로그인 → Connect
2. 브라우저에서 HTTPS 주소(7장, 예: `https://내-pc-이름.tailXXXX.ts.net`) 접속 → MySSD 로그인
   - HTTPS를 설정하지 않았다면 `http://100.x.x.x:8000` (메인 PC의 Tailscale IP)

## 10. 스마트폰에서 접속

1. Play 스토어 / App Store에서 **Tailscale** 설치 → 같은 계정으로 로그인 → 연결 켜기
2. 브라우저(Chrome/Safari)에서 HTTPS 주소 접속 → 로그인
3. 편하게 쓰려면 브라우저 메뉴 → **홈 화면에 추가**
4. 하단 메뉴: **내 파일 · 검색 · 올리기 · 최근 · 더보기**. [올리기]를 누르면 사진 보관함·카메라·파일에서 고를 수 있습니다.

기기를 잃어버리면 https://login.tailscale.com/admin/machines 에서 그 기기를 즉시 제거하세요.

## 11. 문제가 생겼을 때 진단

MySSD → ☰ 더보기 → **🩺 시스템 상태** → **[진단 실행]**

- SSD 연결·읽기·쓰기, 남은 공간, 볼륨 이름, 관리 폴더, Tailscale, Serve, HTTPS, 자동 실행을
  **PASS / FAIL / 확인 필요** 로 보여주고 해결 방법을 안내합니다. 설정을 자동으로 바꾸지는 않습니다.
- 확실히 확인하지 못한 항목은 "정상"이 아니라 **"확인 필요"** 로 표시됩니다.
- **최근 오류**: SSD 연결 끊김, 공간 부족·연결 끊김으로 인한 업로드 실패, 서버 오류.

| 증상 / 메시지 | 해결 |
|---|---|
| 외장 SSD 연결이 끊어졌습니다 | SSD 연결 확인 후 새로고침. 드라이브 문자가 바뀌었으면 4장 볼륨 이름 설정 |
| 허용되지 않은 네트워크입니다 | 접속하는 기기의 Tailscale이 켜져 있는지 확인 |
| 외부에서 응답 없음 | 메인 PC 켜짐·절전 여부, MySSD 실행 여부, 양쪽 Tailscale 연결, 7장 `tailscale serve status` |
| SSD 저장공간이 부족하여 업로드할 수 없습니다 | 📊 저장공간 → 큰 파일, 휴지통(큰 파일순)에서 정리 |
| 업로드 실패 — 다시 시도하십시오 | 연결 확인 후 [실패한 파일 다시 시도]. 이미 저장된 파일은 두 번 저장되지 않습니다 |
| 로그인 실패가 반복되어 잠시 차단 | 5번 연속 실패 시 5분 차단. 기다린 뒤 다시 시도 |
| 계정이 설정되지 않았습니다 | 3장 `python config.py` 후 서버 재시작 |
| 'python'은(는) … 명령이 아닙니다 | Python 설치 시 PATH 체크 누락 → 재설치 |
| 포트 8000 사용 중 | 이미 MySSD가 실행 중(자동 실행)인지 확인, 또는 `config.py`의 `PORT` 변경 |

## 12. 서버 중지

- 창에서 실행한 경우: 그 명령 프롬프트 창에서 `Ctrl + C`
- 자동 실행(창 없음)인 경우: 작업 관리자(`Ctrl+Shift+Esc`) → **세부 정보** → `pythonw.exe` → 작업 끝내기

## 13. 자동 실행 해제

`MySSD` 폴더의 **`autostart_uninstall.bat`** 더블클릭 (시작프로그램 바로가기만 삭제). 이미 실행 중인 서버는 12장처럼 종료합니다.

---

## 새 PC로 옮기기 (복구용)

MySSD는 **프로그램**과 **데이터**가 완전히 분리되어 있습니다.

| 구분 | 위치 | 새 PC로 옮기는 방법 |
|---|---|---|
| 프로그램 코드 | PC의 `MySSD` 폴더 (GitHub에 있음) | GitHub에서 다시 받기 (2장) |
| 계정·비밀키 | `MySSD\.env` | 기존 파일 복사 **또는** 새 PC에서 `python config.py` 로 새로 만들기 |
| 설정 | `MySSD\config.py` | 드라이브 문자·볼륨 이름 등 바꾼 값이 있으면 다시 설정 |
| 작업·오류 기록 | `MySSD\logs\` | 필요 없으면 옮기지 않아도 됨 |
| **내 파일** | 외장 SSD | SSD를 새 PC에 꽂기만 하면 됨 |
| 휴지통·즐겨찾기·썸네일·분석 결과 | 외장 SSD의 `.myssd_trash`, `.myssd_meta`, `.myssd_cache` | SSD에 함께 있으므로 그대로 유지 |

새 PC 순서: 1장 → 2장 → 3장(.env) → 4장(SSD 경로) → 5장 → 6~8장.
**`.env` 와 SSD 내용은 GitHub에 절대 올리지 마세요.** GitHub에는 프로그램 코드만 있습니다.

---

## 기능 안내

| 기능 | 설명 |
|---|---|
| 홈 | SSD 연결 상태·용량, 큰 검색창, 빠른 메뉴(내 파일·최근 파일·즐겨찾기·사진·동영상·휴지통), 관리 메뉴(저장공간·최근 작업·시스템 상태) |
| 파일 탐색 | 폴더 이동, 정렬(이름/최근/오래된/크기/종류), 목록·사진 보기, 300개씩 [더 보기] |
| 검색 | SSD 전체의 파일·폴더 **이름** 검색 (한글·영문·부분 일치, 내용 검색 없음) |
| 업로드 | 여러 파일, 파일마다 크기·진행률·상태 표시, 완료 후 목록 자동 갱신, 실패 시 다시 시도, 같은 이름은 `이름 (1)` 로 저장(덮어쓰기 없음), 업로드 중에는 SSD의 `.myssd_temp` 사용 |
| 다운로드 | 스트리밍 전송(서버 메모리 사용 적음), 이어받기(Range) 지원 |
| 사진 | 썸네일, 큰 화면 보기, [← 이전] [다음 →] (스마트폰은 좌우로 밀기), 다운로드, 즐겨찾기 |
| 동영상 | 브라우저 기본 플레이어(재생·일시정지·탐색·전체화면). MP4(H.264)·WEBM |
| 파일 정보 | 이름·크기·종류·수정일·위치·즐겨찾기 여부, [다운로드] [이름 변경] [이동] [휴지통] |
| 여러 항목 선택 | 체크박스·전체 선택 → [이동] [휴지통] |
| 휴지통 | 삭제는 휴지통으로 이동, 복원, 용량·정렬, 영구 삭제는 확인란 체크 필요, 비우기는 2단계 확인. **자동 삭제 없음** |
| 대량 작업 보호 | 100개 이상(또는 10분 동안 합쳐 100개 이상) 파일이 바뀌면 한 번 더 확인 |
| 저장공간 | 폴더별·종류별 용량, 큰 파일 50개, 관리 폴더 용량 (분석 결과는 저장해 두고 [다시 분석]) |
| 최근 작업 | 업로드·폴더 생성·이름 변경·이동·휴지통·복원·영구 삭제 기록 |
| 시스템 상태 | 서버·SSD·읽기/쓰기·저장공간·Tailscale·HTTPS·자동실행·마지막 오류, 외부 접속 주소, 진단 |

`.myssd_temp`, `.myssd_trash`, `.myssd_meta`, `.myssd_cache` 는 MySSD 관리 폴더입니다. 화면·검색에 나오지 않고 주소로도 접근할 수 없습니다.
탐색기에서 `.myssd_trash`, `.myssd_meta` 를 직접 지우지 마세요 (휴지통·즐겨찾기가 사라짐). `.myssd_cache` 는 지워도 됩니다.

## 설정 (`config.py`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `SSD_ROOT` | `"E:\\"` | 외장 SSD 경로 |
| `SSD_VOLUME_LABEL` | `""` | (선택) SSD 볼륨 이름, 예: `"MYSSD"` |
| `PORT` | 8000 | 접속 포트 |
| `HOST` | `"0.0.0.0"` | HTTPS(Serve) 확인 후 `"127.0.0.1"` 권장 |
| `ALLOWED_NETWORKS` | 이 PC + Tailscale | 이 범위 밖의 접속은 모두 거부 |
| `MAX_UPLOAD_SIZE` | 20GB | 파일 1개 최대 업로드 크기 |
| `MIN_FREE_SPACE_GB` | 20 | 업로드 후 최소한 남길 공간 |
| `LOW_DISK_WARNING_PERCENT` / `CRITICAL_DISK_WARNING_PERCENT` | 15 / 5 | 남은 공간 경고 기준(%) |
| `LOGIN_MAX_FAILS` / `LOGIN_LOCK_SECONDS` | 5 / 300 | 로그인 실패 차단 |
| `SESSION_HOURS` | 12 | 로그인 유지 시간 |
| `RECENT_FILES_LIMIT` / `SEARCH_RESULTS_LIMIT` / `MEDIA_LIST_LIMIT` | 50 / 200 / 300 | 최근 파일·검색·사진/동영상 모아보기 개수 |
| `SCAN_TIME_LIMIT` | 15초 | 검색 등이 SSD를 훑는 최대 시간 |
| `LARGE_FILES_LIMIT` / `FOLDER_PAGE_SIZE` | 50 / 300 | 큰 파일 개수 / 폴더 한 번에 보여줄 항목 수 |
| `MASS_OPERATION_THRESHOLD` / `MASS_OPERATION_WINDOW_MINUTES` | 100 / 10 | 대량 작업 추가 확인 기준 |
| `THUMB_SIZE` / `THUMB_CACHE_MAX_MB` | 256 / 500 | 썸네일 크기 / 썸네일 캐시 최대 크기 |

## 보안

- **접속 제한**: 이 PC와 Tailscale 주소만 허용. Tailscale Funnel(인터넷 공개) 요청은 거부.
- **로그인**: 비밀번호는 PBKDF2-SHA256 해시로만 저장, 5번 연속 실패 시 5분 차단, 로그인 때 세션 새로 발급,
  로그아웃 시 서버에서 세션 삭제(복사해 둔 쿠키도 사용 불가), 서버 재시작 시 모두 로그아웃.
- **쿠키**: `HttpOnly`, `SameSite=Strict`, HTTPS 접속 시 `Secure`.
- **CSRF**: 파일을 바꾸는 모든 요청(업로드·폴더 생성·이름 변경·이동·휴지통·복원·영구 삭제·비우기 등)은 POST + 요청마다 토큰 확인,
  다른 사이트에서 온 요청(Origin 불일치)도 거부. 상태를 바꾸는 GET 요청은 없습니다.
- **보안 헤더**: `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Content-Security-Policy`, HTTPS 시 `Strict-Transport-Security`.
- **경로 보호**: `..`, 절대경로, `C:\`, 링크·정션을 통한 SSD 밖 접근 차단. 관리 폴더 직접 접근 차단.
- **오류 화면**: 사용자에게는 "파일을 찾을 수 없습니다 / 접근할 수 없습니다 / 처리 중 문제가 발생했습니다"만 보이고, 자세한 내용은 서버 로그에만.
- **로그**: `logs/` (자동 교체: myssd.log 1MB×6, 기록 파일 2MB 제한). 로그인 성공/실패, 파일 작업, 시스템 오류만 기록하며
  비밀번호·세션·쿠키는 기록하지 않습니다.
- **GitHub**: `.env`, `logs/`, `.myssd_*`, SSD 파일은 `.gitignore` 로 제외. 코드만 올라갑니다.
