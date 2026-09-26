"""저장공간 분석 (읽기 전용 — 사용자 파일을 바꾸지 않음).

SSD 전체를 한 번 훑어 폴더별·종류별 용량과 큰 파일 목록을 계산하고,
결과를 SSD_ROOT/.myssd_meta/analysis.json 에 저장한다. 이 파일은 언제든 지우고 다시 만들 수 있는 캐시다.
"""
import heapq
import json
import os
import threading
import time
from pathlib import Path

import storage

CACHE_FILE = ".myssd_meta/analysis.json"
CATEGORIES = {
    "사진": {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "tif", "tiff", "raw", "cr2", "nef", "arw", "dng"},
    "동영상": {"mp4", "webm", "mov", "avi", "mkv", "m4v", "wmv", "flv", "mts", "m2ts", "3gp"},
    "문서": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "hwp", "hwpx", "txt", "csv", "md", "rtf", "odt", "ods", "odp"},
    "압축파일": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "alz", "egg"},
}
_EXT = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}

STATUS = {"running": False}
_LOCK = threading.Lock()


def category(name):
    return _EXT.get(os.path.splitext(name)[1].lower().lstrip("."), "기타")


def status():
    with _LOCK:
        return dict(STATUS)


def _set(**kw):
    with _LOCK:
        STATUS.update(kw)


def load(root):
    """저장된 분석 결과. 없거나 손상되었으면 None (사용자 파일에는 영향 없음)."""
    try:
        data = json.loads((Path(root) / CACHE_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(k in data for k in ("time", "folders", "types", "large", "managed")):
            return None
        return data
    except (OSError, ValueError):
        return None


def clear(root):
    (Path(root) / CACHE_FILE).unlink(missing_ok=True)


def run(root, managed, limit):
    """분석 1회. managed = {표시 이름: 관리 폴더 상대경로}. 결과를 저장하고 돌려준다."""
    root = Path(root)
    started = time.time()
    _set(running=True, files=0, current="/", started=started, error=None)
    folders, types, heap = {}, {cat: [0, 0] for cat in (*CATEGORIES, "기타")}, []
    top_files = [0, 0]
    state, files, last = {}, 0, 0.0
    try:
        current = "/"
        for name, rel, is_dir, st in storage.walk(root, state):
            if time.monotonic() - last > 0.5:  # 파일이 아주 많은 폴더 안에서도 진행 상황이 계속 갱신되도록
                last = time.monotonic()
                _set(files=files, current=current)
            if is_dir:
                current = "/" + rel + "/"
                continue
            size = st.st_size
            top, sep, _ = rel.partition("/")
            bucket = folders.setdefault(top, [0, 0]) if sep else top_files
            bucket[0] += size
            bucket[1] += 1
            t = types[category(name)]
            t[0] += size
            t[1] += 1
            item = (size, rel, st.st_mtime)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif size > heap[0][0]:
                heapq.heapreplace(heap, item)
            files += 1
        if not root.is_dir():
            raise OSError("분석 중 SSD 연결이 끊어졌습니다.")
        managed_usage = {}
        for label, rel in managed.items():
            size = count = 0
            for _, _, is_dir, st in storage.walk(root / rel, include_managed=True):
                if not is_dir:
                    size, count = size + st.st_size, count + 1
            managed_usage[label] = [size, count]
        result = {
            "time": time.time(), "duration": round(time.time() - started, 1), "files": files,
            "folders": sorted(([k, *v] for k, v in folders.items()), key=lambda x: -x[1]),
            "top_files": top_files, "types": sorted(([k, *v] for k, v in types.items() if v[1]), key=lambda x: -x[1]),
            "large": [[rel, size, mtime] for size, rel, mtime in sorted(heap, reverse=True)],
            "managed": managed_usage, "unreadable": len(state.get("error_dirs", [])),
        }
        meta = root / ".myssd_meta"
        meta.mkdir(exist_ok=True)
        tmp = meta / "analysis.json.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, root / CACHE_FILE)
        return result
    except OSError as e:
        _set(error=e.args[0] if e.args and isinstance(e.args[0], str) else "분석 중 SSD를 읽을 수 없습니다.")
        return None
    finally:
        _set(running=False, files=files, finished=time.time())
