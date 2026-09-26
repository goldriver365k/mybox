"""SSD 공통 기능: 관리 폴더 판별, 폴더 훑기, 볼륨 이름으로 드라이브 찾기.
(나중에 두 번째 SSD 백업을 추가할 때도 이 함수들을 그대로 쓸 수 있게 server.py 와 분리)"""
import os
import stat
import time
from pathlib import Path

MANAGED_PREFIX = ".myssd"  # .myssd_temp, .myssd_trash, .myssd_meta, .myssd_cache 등 MySSD 내부 관리 폴더
HIDDEN = {"system volume information", "$recycle.bin"}


def is_managed(name):
    return name.lower().startswith(MANAGED_PREFIX)


def walk(root, state=None, deadline=None, include_managed=False):
    """root 아래를 훑으며 (이름, 상대경로, 폴더여부, stat) 를 돌려준다.
    관리 폴더·시스템 폴더·링크/정션은 건너뛰고, 읽을 수 없는 폴더는 state["error_dirs"] 에 기록하고 계속한다.
    deadline(time.monotonic 기준)을 넘기면 멈추고 state["partial"]=True."""
    state = {} if state is None else state
    state.setdefault("error_dirs", [])
    stack = [Path(root)]
    while stack:
        if deadline and time.monotonic() > deadline:
            state["partial"] = True
            return
        folder = stack.pop()
        try:
            it = os.scandir(folder)
        except OSError:
            state["error_dirs"].append(folder.relative_to(root).as_posix() if folder != Path(root) else "")
            continue
        with it:
            for e in it:
                if e.name.lower() in HIDDEN or (not include_managed and is_managed(e.name)):
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
                    stack.append(Path(e.path))


# ---------- Windows 볼륨 이름(라벨) ----------

def _kernel32():
    import ctypes

    k = ctypes.windll.kernel32
    k.SetErrorMode(0x0001)  # 빈 카드리더 등에서 "디스크 없음" 창이 뜨지 않게
    return ctypes, k


def volume_label(drive):
    """'E:\\' 의 볼륨 이름. Windows 가 아니거나 읽을 수 없으면 None"""
    if os.name != "nt":
        return None
    ctypes, k = _kernel32()
    buf = ctypes.create_unicode_buffer(261)
    if not k.GetVolumeInformationW(ctypes.c_wchar_p(drive), buf, 261, None, None, None, None, 0):
        return None
    return buf.value


def _drives():
    _, k = _kernel32()
    mask = k.GetLogicalDrives()
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]


def locate(path, label):
    """설정된 경로를 돌려준다. label(볼륨 이름)이 설정되어 있으면(Windows) 그 드라이브의 이름이 맞는지 확인하고,
    드라이브 문자가 바뀐 경우 같은 이름의 드라이브가 정확히 1개일 때만 그 드라이브를 사용한다.
    확실하지 않으면 LookupError (임의로 판단하지 않음)."""
    path = Path(path)
    if not label or os.name != "nt":
        return path
    drive, rest = os.path.splitdrive(str(path))
    if volume_label(drive + "\\") == label:
        return path
    found = [d for d in _drives() if volume_label(d) == label]
    if len(found) == 1:
        return Path(found[0][:2] + (rest or "\\"))
    if not found:
        raise LookupError(f"볼륨 이름이 '{label}'인 드라이브를 찾을 수 없습니다.")
    raise LookupError(f"볼륨 이름이 '{label}'인 드라이브가 {len(found)}개입니다. 하나만 연결하거나 이름을 바꾸세요.")
