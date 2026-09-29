"""서버 자원을 여러 Auto Report 프로세스가 나눠 쓰게 하는 병렬도 조정기.

같은 서버에서 Scheduler·수동 bash·일일 서비스가 동시에 Main.py 를 돌려도 전체 렌더링 워커 수가
코어 수를 넘지 않게 한다. 규칙은 두 가지다.

1. **슬롯(OS 파일 잠금)** — 서버 전체 워커 한도 = 쓸 수 있는 코어 − reserve_cores.
   각 Main 프로세스는 워커 1개당 슬롯 파일 1개를 잠근다. 프로세스가 죽으면 OS 가 잠금을 풀어
   슬롯이 새지 않는다(파일 삭제·정리 불필요).
2. **지금 남은 자원** — 다른 프로그램(S3 전송, 다른 bash)이 쓰는 CPU·메모리를 실측해 그만큼 덜 쓴다.
   컨테이너(노트북 서버) cgroup CPU·메모리 한도를 호스트 값보다 우선한다.

슬롯을 하나도 못 얻으면 워커 없이 메인 프로세스에서 직렬로 그린다(멈추지 않는다).
표준 라이브러리만 쓴다(psutil 은 있으면 사용).
"""
import math
import os
import tempfile
import threading
import time

_LOCK = threading.Lock()
_LEASE = []            # [(slot_index, file_object)] — 이 프로세스가 쥔 슬롯
_PLAN = {'at': 0.0, 'plan': None}


def _read(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return stream.read().strip()
    except OSError:
        return None


def _getter(settings):
    """dict·My_config(.get: 제품 설정→코드 기본값) 둘 다 받는다."""
    if settings is None:
        return lambda key, default: default
    if hasattr(settings, 'get'):
        return lambda key, default: settings.get(key, default)
    return lambda key, default: getattr(settings, key, default)


def usable_cores():
    """이 프로세스가 실제로 쓸 수 있는 코어 수: affinity → cgroup CPU quota 순으로 좁힌다."""
    cores = os.cpu_count() or 1
    try:
        cores = len(os.sched_getaffinity(0)) or cores
    except (AttributeError, OSError):
        pass
    quota = None
    v2 = _read('/sys/fs/cgroup/cpu.max')
    if v2 and not v2.startswith('max'):
        try:
            limit, period = v2.split()[:2]
            quota = int(limit) / int(period)
        except ValueError:
            quota = None
    else:
        q, p = _read('/sys/fs/cgroup/cpu/cpu.cfs_quota_us'), _read('/sys/fs/cgroup/cpu/cpu.cfs_period_us')
        try:
            if q and p and int(q) > 0:
                quota = int(q) / int(p)
        except ValueError:
            quota = None
    if quota:
        cores = min(cores, max(1, int(math.floor(quota))))
    return max(1, cores)


def _cgroup_available_bytes():
    """컨테이너 메모리 한도 안에서 남은 양(없으면 None). page cache(inactive_file)는 회수 가능으로 본다."""
    limit = _read('/sys/fs/cgroup/memory.max')
    usage = _read('/sys/fs/cgroup/memory.current')
    stat_path = '/sys/fs/cgroup/memory.stat'
    if limit is None:
        limit = _read('/sys/fs/cgroup/memory/memory.limit_in_bytes')
        usage = _read('/sys/fs/cgroup/memory/memory.usage_in_bytes')
        stat_path = '/sys/fs/cgroup/memory/memory.stat'
    try:
        limit_value = int(limit) if limit and limit != 'max' else None
        used = int(usage) if usage else None
    except ValueError:
        return None
    if not limit_value or limit_value >= 1 << 60 or used is None:
        return None
    reclaimable = 0
    stats = _read(stat_path) or ''
    for line in stats.splitlines():
        name, _, value = line.partition(' ')
        if name in ('inactive_file', 'total_inactive_file'):
            try:
                reclaimable = int(value)
            except ValueError:
                pass
    return max(0, limit_value - used + reclaimable)


def available_memory_gb():
    """지금 새로 쓸 수 있는 메모리(GB). 호스트 가용과 컨테이너 한도 중 작은 값. 측정 불가면 None."""
    host = None
    try:
        import psutil
        host = psutil.virtual_memory().available
    except Exception:
        try:
            with open('/proc/meminfo') as stream:
                for line in stream:
                    if line.startswith('MemAvailable:'):
                        host = int(line.split()[1]) * 1024
                        break
        except OSError:
            pass
        if host is None and os.name == 'nt':
            try:
                import ctypes

                class _Status(ctypes.Structure):
                    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
                status = _Status()
                status.dwLength = ctypes.sizeof(_Status)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    host = status.ullAvailPhys
            except Exception:
                pass
    container = _cgroup_available_bytes()
    values = [v for v in (host, container) if v is not None]
    return min(values) / 1024 ** 3 if values else None


def busy_cores(cores, sample_sec=0.25):
    """지금 다른 작업(이 프로세스의 워커 포함)이 쓰고 있는 코어 수 추정. 측정 불가면 0."""
    try:
        import psutil
        return psutil.cpu_percent(interval=sample_sec) / 100.0 * (os.cpu_count() or cores)
    except Exception:
        pass
    try:
        return min(float(os.getloadavg()[0]), float(os.cpu_count() or cores))
    except (AttributeError, OSError):
        return 0.0


def slot_dir():
    """서버 전체가 공유하는 슬롯 폴더. 설치 폴더가 달라도 같은 사용자 계정이면 같은 곳을 본다."""
    path = os.getenv('AUTO_REPORT_SLOT_DIR') or os.path.join(tempfile.gettempdir(), 'auto_report_worker_slots')
    os.makedirs(path, exist_ok=True)
    return path


def _try_lock(path):
    stream = open(path, 'a+b')
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError:
        stream.close()
        return None


def _unlock(stream):
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        stream.close()


def held_slots():
    return len(_LEASE)


def _resize_lease(target, total):
    """슬롯을 target 개가 되도록 더 잠그거나 푼다. 실제로 쥔 개수를 돌려준다."""
    with _LOCK:
        while len(_LEASE) > target:
            _index, stream = _LEASE.pop()
            _unlock(stream)
        if len(_LEASE) < target:
            owned = {index for index, _ in _LEASE}
            root = slot_dir()
            for index in range(total):
                if len(_LEASE) >= target:
                    break
                if index in owned:
                    continue
                stream = _try_lock(os.path.join(root, f'slot_{index:03d}.lock'))
                if stream is not None:
                    _LEASE.append((index, stream))
        return len(_LEASE)


def trim_lease(count):
    """이미 쥔 슬롯 중 count 개만 남기고 반납(풀을 다시 만들지 않을 때 남는 슬롯 정리)."""
    return _resize_lease(min(len(_LEASE), max(0, int(count))), 0)


def release_all():
    _resize_lease(0, 0)
    with _LOCK:
        _PLAN.update(at=0.0, plan=None)


def slots_in_use(total):
    """다른 프로세스가 쥐고 있는 슬롯 수(표시용). 잠깐 잠가 보고 바로 푼다."""
    busy = 0
    root = slot_dir()
    owned = {index for index, _ in _LEASE}
    for index in range(total):
        if index in owned:
            continue
        stream = _try_lock(os.path.join(root, f'slot_{index:03d}.lock'))
        if stream is None:
            busy += 1
        else:
            _unlock(stream)
    return busy


def plan_workers(settings=None, force=False):
    """렌더링 워커 수를 정하고 그만큼 슬롯을 잡는다.

    settings(dict 또는 속성 객체): parallel_workers(요청 상한), parallel_max_workers, parallel_reserve_cores,
    parallel_mem_per_worker_gb, parallel_reserve_gb, parallel_replan_sec.
    반환 dict: workers(1=직렬), cores, busy, avail_gb, slots_total, slots_other, reason.
    """
    get = _getter(settings)
    now = time.time()
    replan = float(get('parallel_replan_sec', 20) or 20)
    with _LOCK:
        cached = _PLAN['plan']
        if cached and not force and now - _PLAN['at'] < replan:
            if cached.get('forced') or cached['workers'] <= 1:
                return dict(cached)
            return dict(cached, workers=max(1, len(_LEASE)))
    try:
        forced = int(get('parallel_workers', 0) or 0)
    except (TypeError, ValueError):
        forced = 0
    cores = usable_cores()
    reserve_cores = max(0, int(get('parallel_reserve_cores', 1) or 0))
    total = max(1, cores - reserve_cores)
    cap = max(1, int(get('parallel_max_workers', 8) or 8))
    per_gb = float(get('parallel_mem_per_worker_gb', 1.2) or 1.2)
    reserve_gb = float(get('parallel_reserve_gb', 3.0) or 3.0)
    own = len(_LEASE)
    avail = available_memory_gb()
    busy = busy_cores(cores)
    # 우리 워커가 이미 돌고 있으면 그 몫은 '남은 자원'으로 되돌려 센다.
    free_cpu = max(0.0, cores - reserve_cores - busy) + own
    # 메모리: 우리 워커가 이미 쓰는 몫(대략)도 같은 방식으로 되돌린다.
    mem_cap = total if avail is None else int(max(0.0, avail + own * per_gb - reserve_gb) // per_gb)
    want = min(cap, total, mem_cap, int(math.floor(free_cpu + 0.5)))
    reason = f'코어 {cores}(예비 {reserve_cores}) · 사용 중 {busy:.1f} · 가용 메모리 ' + (
        f'{avail:.1f}GB' if avail is not None else '측정 불가')
    if forced > 0:
        want = min(want, forced)
        reason += f' · 요청 상한 parallel_workers={forced}'
    workers = 1
    if want >= 2:
        got = _resize_lease(want, max(total, want))
        workers = got if got >= 2 else 1
        if workers == 1:
            _resize_lease(0, total)
        if got < want:
            reason += f' · 다른 Auto Report가 슬롯 사용 중(요청 {want} → 확보 {got})'
    else:
        _resize_lease(0, total)
        if avail is not None and mem_cap < 2:
            reason += ' · 메모리 여유 부족'
        elif free_cpu < 2:
            reason += ' · CPU 여유 부족'
    plan = dict(workers=workers, forced=forced > 0, cores=cores, busy=round(busy, 1), avail_gb=None if avail is None else round(avail, 1),
                slots_total=total, reason=reason)
    with _LOCK:
        _PLAN.update(at=now, plan=plan)
    return plan


def duckdb_settings(settings=None):
    """DuckDB 스레드·메모리 한도. 기본값(모든 코어·RAM 80%)은 동시 실행 시 서로 밀어낸다."""
    get = _getter(settings)
    cores = usable_cores()
    busy = busy_cores(cores, sample_sec=0.1)
    threads = int(get('duckdb_threads', 0) or 0) or max(1, min(cores, int(round(cores - busy)) or 1))
    avail = available_memory_gb()
    fraction = float(get('duckdb_memory_fraction', 0.5) or 0.5)
    limit_gb = float(get('duckdb_memory_limit_gb', 0) or 0) or (max(1.0, avail * fraction) if avail else 4.0)
    return dict(threads=threads, memory_limit=f'{limit_gb:.1f}GB')


def release_memory():
    """랏 1건이 끝난 뒤: 순환 참조 정리 + (glibc) 비어 있는 힙을 OS 에 돌려준다."""
    import gc
    gc.collect()
    if os.name != 'nt':
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
        except Exception:
            pass
