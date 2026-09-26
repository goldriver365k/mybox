"""시스템 상태 확인 (읽기 전용): Tailscale, Serve(HTTPS), 자동 실행.
확실히 확인한 것만 '정상'으로 보고하고, 확인할 수 없으면 None(=확인 필요)을 돌려준다.
어떤 설정도 바꾸지 않는다."""
import json
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

STARTUP_LINK = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/MySSD.lnk"


def tailscale_exe():
    exe = shutil.which("tailscale")
    if exe:
        return exe
    win = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    return str(win) if os.name == "nt" and win.exists() else None


def _run(exe, *args):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: 창 없이 실행 중일 때 콘솔 창이 뜨지 않게
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=6, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def tailscale(port):
    """installed/running: True·False·None(확인 필요). ip, dns, serve, funnel 은 확인된 경우에만 값이 있다."""
    info = {"installed": None, "running": None, "ip": None, "dns": None, "serve": None, "funnel": None, "backend": None}
    exe = tailscale_exe()
    if not exe:
        info["installed"] = False if os.name == "nt" else None
        return info
    info["installed"] = True
    out = _run(exe, "status", "--json")
    try:
        st = json.loads(out) if out else None
    except ValueError:
        st = None
    if st:
        info["backend"] = st.get("BackendState")
        info["running"] = st.get("BackendState") == "Running"
        me = st.get("Self") or {}
        info["ip"] = next((ip for ip in me.get("TailscaleIPs") or [] if "." in ip), None)
        info["dns"] = (me.get("DNSName") or "").rstrip(".") or None
    out = _run(exe, "serve", "status", "--json")
    if out is not None:
        text = out.replace(" ", "")
        info["serve"] = any(f"{h}:{port}" in text for h in ("127.0.0.1", "localhost"))
        info["funnel"] = '"AllowFunnel"' in text and "true" in text.split('"AllowFunnel"', 1)[1][:300]
    return info


def autostart():
    """자동 실행 바로가기가 있는지 (Windows 에서만 확인 가능, 그 외 None)"""
    if os.name != "nt" or not os.environ.get("APPDATA"):
        return None
    return STARTUP_LINK.exists()


def https_probe(url):
    """실제 HTTPS 요청으로 확인. (성공 여부, 설명)"""
    try:
        with urllib.request.urlopen(url, timeout=8, context=ssl.create_default_context()) as r:
            body = r.read(20000).decode("utf-8", "replace")
            if r.status == 200 and "내 SSD" in body:
                return True, f"{url} 응답 확인 (인증서 정상)"
            return False, f"{url} 응답은 왔지만 MySSD 화면이 아닙니다 (HTTP {r.status})"
    except ssl.SSLError as e:
        return False, f"인증서 오류: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"접속 실패: {e.reason}"
    except OSError as e:
        return False, f"접속 실패: {e}"
