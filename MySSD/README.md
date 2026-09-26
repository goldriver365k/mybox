# MySSD — 내 SSD 개인 파일 서버 (3단계)

Windows PC에 USB로 연결된 외장 SSD의 파일을 브라우저에서 보고 다운로드하는 프로그램입니다.
**이 PC와, 내 Tailscale 네트워크에 연결된 기기(노트북·스마트폰 등)에서만 접속할 수 있습니다.**

```
외장 SSD → Windows 메인 PC (MySSD) → Tailscale 암호화 네트워크 → 외부 PC / 노트북 / 스마트폰
```

보안은 두 겹입니다: **1차 Tailscale 승인 기기** + **2차 MySSD 로그인**.
공유기 포트포워딩, 공인 IP 노출, DDNS는 사용하지 않습니다. 파일은 외장 SSD에만 있고 클라우드로 올라가지 않습니다.

로그인 후 폴더 탐색, 다운로드, 파일 올리기, 새 폴더 만들기, 사진·동영상 미리보기를 할 수 있습니다.
삭제·이름 변경·이동 기능은 없습니다.

## 1. 준비 (처음 한 번만)

1. Python 설치: https://www.python.org/downloads/
   - 설치 첫 화면에서 **"Add python.exe to PATH"** 를 반드시 체크하세요.
2. 이 `MySSD` 폴더에서 명령 프롬프트(cmd)를 엽니다.
   - 탐색기에서 `MySSD` 폴더를 열고 주소창에 `cmd` 입력 후 Enter
3. 필요한 프로그램 설치:
   ```
   pip install -r requirements.txt
   ```

## 2. SSD 경로 설정

기본값은 `E:\` 입니다. 외장 SSD 드라이브 문자가 다르면 `config.py`를 메모장으로 열어 수정하세요.

```python
SSD_ROOT = "E:\\"     # 예: F 드라이브라면 "F:\\"
```

## 3. 계정 만들기 (처음 한 번만)

```
python config.py
```

아이디와 비밀번호(8자 이상)를 입력하면 같은 폴더에 `.env` 파일이 만들어집니다.

- `.env`에는 비밀번호 **원문이 아니라 해시값**과 세션 비밀키가 저장됩니다.
- `.env`는 절대 다른 사람에게 보내거나 GitHub에 올리지 마세요. (`.gitignore`로 제외되어 있습니다)
- 비밀번호를 바꾸려면 `python config.py`를 다시 실행하고 서버를 재시작하세요.
- `.env` 대신 Windows 환경변수 `MYSSD_USER`, `MYSSD_PASSWORD_HASH`, `MYSSD_SECRET_KEY`로 설정해도 됩니다.

## 4. 실행

```
python server.py
```

이 PC의 브라우저에서 접속: http://127.0.0.1:8000

실행하면 창에 Tailscale 접속 주소도 표시됩니다. 종료하려면 명령 프롬프트 창에서 `Ctrl + C`를 누르세요.

## 5. 외부 기기에서 접속하기 (Tailscale)

Tailscale은 내 기기끼리만 암호화 터널(WireGuard)로 연결해 주는 무료 VPN입니다. 개인 사용은 무료 요금제로 충분합니다.

### 5-1. 메인 PC (SSD가 연결된 PC)

1. https://tailscale.com/download 에서 Windows용 Tailscale 설치
2. 작업 표시줄의 Tailscale 아이콘 → **Log in** → Google/Microsoft 등 **본인 계정**으로 로그인
3. Tailscale 아이콘 메뉴에서 이 PC의 IP(`100.x.x.x`)를 확인할 수 있습니다.
4. 권장: Tailscale 메뉴 → Preferences → **Run unattended** 켜기 (Windows 로그인 전에도 연결 유지)
5. 권장: Windows 전원 설정에서 **절전 모드 "안 함"** (PC가 잠들면 외부에서 접속할 수 없습니다)

### 5-2. Windows 방화벽 (메인 PC, 처음 한 번만)

`python server.py`를 처음 실행하면 "Windows 보안 경고" 창이 뜰 수 있습니다.

- **"개인 네트워크"만 체크**하고 **"공용 네트워크"는 체크하지 마세요** → [액세스 허용]
  (Tailscale 연결은 보통 개인 네트워크로 분류됩니다. 외부에서 접속이 안 되면 아래 확인 명령으로 점검하세요)

그다음 **시작 메뉴 → "PowerShell" 우클릭 → 관리자 권한으로 실행** 후 아래를 붙여넣으면,
Python 서버가 **Tailscale 주소(100.64.0.0/10)에서 오는 접속만** 받도록 방화벽 범위를 좁힙니다.

```powershell
Get-NetFirewallRule -Direction Inbound | Where-Object { ($_ | Get-NetFirewallApplicationFilter).Program -like "*python*" } | Set-NetFirewallRule -RemoteAddress 100.64.0.0/10
```

확인 (RemoteAddress가 `100.64.0.0/255.192.0.0`으로 나오면 정상):

```powershell
Get-NetFirewallRule -Direction Inbound | Where-Object { ($_ | Get-NetFirewallApplicationFilter).Program -like "*python*" } | Get-NetFirewallAddressFilter
```

> 방화벽 설정을 놓치더라도 MySSD 자체가 **이 PC(127.0.0.1)와 Tailscale 주소 외에는 모두 거부**합니다
> (같은 와이파이의 다른 기기도 "허용되지 않은 네트워크입니다"로 차단). 허용 범위는 `config.py`의 `ALLOWED_NETWORKS`.

**공유기 설정(포트포워딩)은 절대 건드리지 마세요.** Tailscale은 포트를 열지 않아도 연결됩니다.

### 5-3. 외부 기기 (노트북, 다른 PC, 스마트폰)

1. Tailscale 설치 — Windows/Mac: https://tailscale.com/download , 스마트폰: Play 스토어 / App Store에서 "Tailscale"
2. **메인 PC와 같은 계정**으로 로그인하고 연결(Connect)을 켭니다.
3. 브라우저에서 접속:
   ```
   http://100.80.20.10:8000      ← 메인 PC의 Tailscale IP로 바꾸세요
   ```
4. MySSD 아이디/비밀번호로 로그인합니다.

**장치 이름으로 접속 (MagicDNS)**: Tailscale 관리 페이지(https://login.tailscale.com/admin/dns)에서 MagicDNS가 켜져 있으면
IP 대신 메인 PC의 장치 이름을 쓸 수 있습니다. 장치 이름은 관리 페이지의 Machines 목록에서 확인하세요.

```
http://내-pc-이름:8000
http://내-pc-이름.tailXXXX.ts.net:8000    ← 짧은 이름이 안 될 때 (관리 페이지에 표시된 전체 이름)
```

### 참고

- 브라우저 주소창에 "주의 요함/안전하지 않음"이 보일 수 있습니다. 주소가 `http://`라서 표시되는 것이며,
  실제 통신은 Tailscale(WireGuard)이 기기 간에 **암호화**합니다.
- Tailscale 계정에는 **본인 기기만** 추가하세요. 기기를 잃어버리면 관리 페이지(Machines)에서 즉시 제거하세요.
- 더 엄격하게 하려면 Tailscale 관리 페이지 Settings → **Device approval**을 켜서, 새 기기는 직접 승인해야만 연결되게 할 수 있습니다.
- 외부 접속을 잠시 끄고 싶으면 `config.py`의 `HOST`를 `"127.0.0.1"`로 바꾸고 서버를 재시작하세요.

## 사용법

- 아이디/비밀번호로 **로그인**하면 SSD 목록이 표시됩니다. 12시간 후 또는 **로그아웃** 시 다시 로그인해야 합니다.
- 📁 폴더를 누르면 안으로 들어가고, **← 상위 폴더**로 되돌아갑니다.
- 🖼 사진(JPG, JPEG, PNG, WEBP, GIF) → 미리보기 화면 ([다운로드] [뒤로가기])
- 🎬 동영상(MP4, WEBM) → 브라우저에서 재생 ([다운로드] [뒤로가기])
- 📄 그 밖의 파일 → 바로 다운로드
- **파일 올리기**: 버튼을 누르고 파일을 고르면 바로 *지금 보고 있는 SSD 폴더*에 저장됩니다. 여러 개를 한 번에 고를 수 있습니다.
  스마트폰에서는 사진 보관함·카메라·파일 중에서 고를 수 있습니다. 올리는 동안 화면을 닫지 마세요.
  같은 이름이 있으면 덮어쓰지 않고 `photo (1).jpg`, `photo (2).jpg` 처럼 새 이름으로 저장합니다.
  한 번에 올릴 수 있는 크기는 기본 20GB이며 `config.py`의 `MAX_UPLOAD_SIZE`로 바꿀 수 있습니다.
  업로드 중 끊기면 미완성 파일은 남지 않습니다. (올리는 동안에는 `.myssd-upload-….part` 숨김 임시 파일에 쓰고, 끝까지 받은 뒤에만 실제 이름으로 바뀝니다)
- **새 폴더**: 버튼을 누르고 이름을 입력한 뒤 [만들기]를 누르면 지금 위치에 폴더가 생깁니다.
  `\ / : * ? " < > |` 문자와 `CON`, `NUL` 같은 Windows 예약어는 쓸 수 없습니다.
- 동영상은 필요한 부분만 나눠 보내므로(Range 요청) 큰 파일도 바로 재생·탐색됩니다. 변환은 하지 않습니다.
- MP4는 H.264 코덱인 경우 대부분 브라우저에서 재생됩니다. 재생되지 않는 파일은 다운로드해서 보세요.

## 자주 묻는 문제

| 화면 메시지 | 해결 방법 |
|---|---|
| 허용되지 않은 네트워크입니다 | 접속하는 기기에서 Tailscale이 연결(Connected)되어 있는지 확인. 주소는 `100.x.x.x` 또는 장치 이름 |
| 로그인 실패가 반복되어 잠시 차단되었습니다 | 5번 연속 틀리면 5분간 차단됩니다. 기다린 뒤 다시 시도 |
| 업로드 최대 크기를 넘었습니다 | 나눠서 올리거나 `config.py`의 `MAX_UPLOAD_SIZE`를 늘리기 |
| 외부 기기에서 접속이 안 됨 (응답 없음) | 메인 PC가 켜져 있는지, 서버 실행 중인지, 양쪽 Tailscale 연결 상태, 5-2 방화벽 설정 확인 |
| 계정이 설정되지 않았습니다 | `python config.py` 실행 후 서버 재시작 |
| 요청이 만료되었습니다 | 페이지를 새로고침한 뒤 다시 시도 (로그인 시간이 지났으면 다시 로그인) |
| 외장 SSD를 찾을 수 없습니다 | SSD 연결 확인, `config.py`의 드라이브 문자 확인 후 새로고침 |
| 이 폴더/파일에 접근할 권한이 없습니다 | Windows 권한으로 막힌 항목입니다 (시스템 폴더 등) |
| 'python'은(는) 내부 또는 외부 명령이 아닙니다 | Python 설치 시 PATH 체크를 빠뜨림 → 재설치 |
| 포트 8000이 이미 사용 중 | `config.py`의 `PORT`를 8001 등으로 변경 |

## 설정 (`config.py`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `SSD_ROOT` | `"E:\\"` | 외장 SSD 경로 |
| `PORT` | `8000` | 접속 포트 |
| `HOST` | `"0.0.0.0"` | `"127.0.0.1"`로 바꾸면 이 PC에서만 접속 |
| `ALLOWED_NETWORKS` | 이 PC + Tailscale 주소 | 이 범위 밖의 접속은 모두 거부 |
| `MAX_UPLOAD_SIZE` | 20GB | 한 번에 올릴 수 있는 최대 크기 |
| `LOGIN_MAX_FAILS` / `LOGIN_LOCK_SECONDS` | 5회 / 300초 | 로그인 연속 실패 시 차단 |
| `SESSION_HOURS` | 12 | 로그인 유지 시간 |

## 보안

- 공유기 포트포워딩을 쓰지 않습니다. 접속은 이 PC와 Tailscale 주소(`100.64.0.0/10`)에서만 허용됩니다.
- 로그인하지 않으면 SSD 내용을 볼 수도, 올릴 수도 없습니다. (로그인 전 업로드 요청은 파일 내용을 받기 전에 거부)
- 같은 기기에서 5번 연속 로그인에 실패하면 5분간 로그인이 차단됩니다.
- 로그아웃하면 서버에서 세션이 삭제되어, 이전 쿠키를 복사해 두었더라도 다시 쓸 수 없습니다. 서버를 재시작해도 모두 로그아웃됩니다.
- 세션 비밀키는 `python config.py`가 만든 32바이트 랜덤값이며 `.env`에만 저장됩니다.
- 비밀번호는 PBKDF2-SHA256(60만 회) 해시로만 저장·검증합니다.
- URL이나 업로드 파일명을 조작해도 `SSD_ROOT` 밖(`..`, `C:\`, Windows 폴더 등)에는 읽거나 저장할 수 없습니다.
