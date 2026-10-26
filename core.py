"""killapp 核心逻辑。

本模块分为两层：

- **数据层**：`collect_*` / `find_*` / `terminate_*`，只负责收集和返回结构化数据，
  不做任何 print 或 input，供 GUI 复用。
- **展示层**：`list_*` / `search` / `kill_*`，负责把数据层的结果打印到终端，供 CLI 使用。
"""

import os
import socket
import threading
import time
import unicodedata
from dataclasses import dataclass, replace

import psutil

import winproc

DEV_PROCESS_NAMES = {"node", "python", "java", "go"}

# 图形界面里「只看开发应用」复选框用的关键字
DEV_APP_KEYWORDS = ("node", "python", "java")


def is_dev_app(name):
    """进程名是否属于 node / python / java 这类开发应用。"""
    low = (name or "").lower()
    return any(k in low for k in DEV_APP_KEYWORDS)


# 系统保留进程，不展示、也不允许结束
HIDDEN_PIDS = {0, 4}

# 关键系统进程，即使以管理员运行也应拒绝结束（结束会导致系统不稳定）
PROTECTED_NAMES = {
    "system",
    "system idle process",
    "registry",
    "memory compression",
    "smss",
    "csrss",
    "wininit",
    "winlogon",
    "services",
    "lsass",
    "lsaiso",
    "svchost",
    "fontdrvhost",
    "dwm",
    "spoolsv",
    "secure system",
    "clfs",
    "smss.exe",
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ProcInfo:
    """单个进程的快照。"""

    pid: int
    name: str
    status: str
    memory_bytes: int
    cpu_percent: float = 0.0
    ports: str = ""
    exe: str = ""
    create_time: float = 0.0


@dataclass
class PortInfo:
    """一个监听端口的快照。"""

    proto: str
    ip: str
    port: int
    pid: int = 0
    name: str = ""
    exe: str = ""


@dataclass
class KillResult:
    """结束进程的结果。"""

    pid: int
    name: str
    ok: bool
    error: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def format_size(bytes_val):
    """把字节数格式化成便于阅读的字符串。"""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_val / (1024 * 1024 * 1024):.1f} GB"


# 兼容旧名
_format_size = format_size


def status_cn(status):
    """把 psutil 的状态码翻译成中文。"""
    return {
        "running": "运行中",
        "sleeping": "休眠",
        "disk-sleep": "磁盘休眠",
        "stopped": "已停止",
        "traced": "被跟踪",
        "zombie": "僵尸",
        "dead": "已死亡",
        "waking": "唤醒中",
        "idle": "空闲",
    }.get(status, status or "")


_status_cn = status_cn


def _get_ports_by_pid():
    """返回 {pid: "端口1, 端口2"}，只统计正在监听的 TCP 端口（已去重）。"""
    ports_map = {}
    seen = {}
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status != psutil.CONN_LISTEN:
                continue
            pid = conn.pid
            if not pid:
                continue
            port = conn.laddr.port
            # 同一个进程可能在 IPv4/IPv6 上各监听一次同一个端口，去重
            bucket = seen.setdefault(pid, [])
            if port in bucket:
                continue
            bucket.append(port)
    except psutil.AccessDenied:
        pass
    for pid, ports in seen.items():
        ports_map[pid] = ", ".join(str(p) for p in ports)
    return ports_map


class CpuSampler:
    """跨刷新周期采样 CPU 占用率。

    输入是「进程累计占用的 CPU 秒数」，输出是两次采样之间占用的百分比。
    语义与 Windows 任务管理器一致：100% 表示占满一个逻辑核心，多线程可以超过 100%。

    create_time 变化说明 PID 被复用，此时旧的基线作废，需要重新建立。
    """

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def sample_seconds(self, pid, cpu_seconds, create_time=0.0, now=None):
        """返回该进程自上次采样以来的 CPU 占用百分比。"""
        if now is None:
            now = time.monotonic()
        with self._lock:
            entry = self._cache.get(pid)
            stale = entry is None or (
                create_time and entry[0] and entry[0] != create_time
            )
            if stale:
                # 新进程或 PID 被复用：丢弃旧基线，建立新基线
                self._cache[pid] = (create_time or 0.0, cpu_seconds, now)
                return 0.0
            _, last_cpu, last_time = entry
            wall = now - last_time
            self._cache[pid] = (create_time or 0.0, cpu_seconds, now)
            if wall <= 0:
                return 0.0
            delta = cpu_seconds - last_cpu
            if delta <= 0:
                return 0.0
            return min(delta / wall * 100.0, 999.0)

    def prune(self, alive_pids):
        """清理已经消失的 PID，避免缓存无限增长。"""
        with self._lock:
            for pid in list(self._cache):
                if pid not in alive_pids:
                    self._cache.pop(pid, None)


# 慢速回退（打不开句柄的系统进程）的缓存有效期
# 慢速回退（打不开句柄的系统进程）的刷新参数
FALLBACK_TTL = 15.0        # 同一个进程两次慢速刷新之间的最短间隔（秒）
FALLBACK_BATCH = 12        # 每个刷新周期最多慢速刷新多少个进程


class ProcessCollector:
    """进程采集器。

    快速路径用 Win32 API 直接读句柄（见 :mod:`winproc`），几百个进程只要几毫秒。
    少数打不开句柄的系统进程（未提权时）退回 psutil 慢速采集 —— 那个路径每个进程要
    十几毫秒，所以改成后台线程分批轮转刷新，不占用界面线程，缓存结果直接显示。
    """

    def __init__(self):
        self._cpu = CpuSampler()
        self._fallback = {}          # pid -> (时间戳, ProcInfo)
        self._pending = []           # 待慢速刷新的 pid（轮转队列）
        self._pending_set = set()
        self._lock = threading.Lock()
        self._refreshing = False

    # -- 对外接口 ---------------------------------------------------------

    def snapshot(self, only_dev=False):
        """返回当前进程快照（快速路径 + 缓存的慢速回退数据）。"""
        ports_map = _get_ports_by_pid()
        now = time.monotonic()
        wall = time.time()

        rows = []
        slow_pids = []
        alive = set()

        for p in psutil.process_iter(['pid', 'name']):
            try:
                pid = p.pid
                if pid in HIDDEN_PIDS:
                    continue
                name = p.info['name'] or "(未知)"
                if only_dev and not any(
                    dev in name.lower() for dev in DEV_PROCESS_NAMES
                ):
                    continue
                alive.add(pid)

                fast = winproc.read_process(pid) if winproc.is_supported() else None
                if fast is not None:
                    cpu = self._cpu.sample_seconds(
                        pid, fast.cpu_seconds, fast.create_time, now
                    )
                    rows.append(ProcInfo(
                        pid=pid,
                        name=name,
                        status=fast.status,
                        memory_bytes=fast.rss,
                        cpu_percent=cpu,
                        ports=ports_map.get(pid, ""),
                        exe=fast.exe,
                        create_time=fast.create_time,
                    ))
                else:
                    slow_pids.append(pid)
                    cached = self._fallback.get(pid)
                    if cached is not None:
                        rows.append(replace(cached[1], ports=ports_map.get(pid, "")))
                    else:
                        rows.append(ProcInfo(
                            pid=pid,
                            name=name,
                            status="running",
                            memory_bytes=0,
                            ports=ports_map.get(pid, ""),
                        ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        with self._lock:
            # 清掉已经消失的进程
            for pid in list(self._fallback):
                if pid not in alive:
                    self._fallback.pop(pid, None)
            # 需要慢速刷新的进程排进轮转队列
            for pid in slow_pids:
                entry = self._fallback.get(pid)
                stale = entry is None or (wall - entry[0]) >= FALLBACK_TTL
                if stale and pid not in self._pending_set:
                    self._pending.append(pid)
                    self._pending_set.add(pid)

        self._cpu.prune({r.pid for r in rows})
        self._schedule_fallback_refresh()

        rows.sort(key=lambda r: r.pid)
        return rows

    # -- 慢速回退 ---------------------------------------------------------

    def _schedule_fallback_refresh(self):
        """取一小批待刷新进程丢给后台线程，避免慢速采集拖慢界面。"""
        with self._lock:
            if self._refreshing or not self._pending:
                return
            self._refreshing = True
            batch = self._pending[:FALLBACK_BATCH]
            del self._pending[:len(batch)]
            self._pending_set.difference_update(batch)
        threading.Thread(
            target=self._refresh_batch, args=(batch,), daemon=True
        ).start()

    def _refresh_batch(self, batch):
        try:
            for pid in batch:
                info = self._read_slow(pid)
                if info is not None:
                    with self._lock:
                        self._fallback[pid] = (time.time(), info)
                # 让出 GIL，别和界面刷新抢时间片
                time.sleep(0.002)
        finally:
            with self._lock:
                self._refreshing = False

    def _read_slow(self, pid):
        """用 psutil 采集单个进程（慢，但能拿到受保护进程的数据）。"""
        try:
            p = psutil.Process(pid)
            name = p.name() or "(未知)"
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

        mem = 0
        try:
            mem = p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

        status = "running"
        try:
            status = p.status()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

        cpu_seconds = 0.0
        try:
            times = p.cpu_times()
            cpu_seconds = times.user + times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

        create_time = 0.0
        try:
            create_time = p.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

        exe = ""
        try:
            exe = p.exe() or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

        cpu = self._cpu.sample_seconds(
            pid, cpu_seconds, create_time, time.monotonic()
        )
        return ProcInfo(
            pid=pid,
            name=name,
            status=status,
            memory_bytes=mem,
            cpu_percent=cpu,
            ports="",
            exe=exe,
            create_time=create_time,
        )


# GUI 使用的默认采集器
default_collector = ProcessCollector()



# ---------------------------------------------------------------------------
# 数据层：收集
# ---------------------------------------------------------------------------


def collect_processes(cpu_sampler=None, only_dev=False):
    """收集进程快照，返回 :class:`ProcInfo` 列表（按 PID 升序）。

    这条路径全部走 psutil，能拿到受保护进程的数据，但比较慢（几秒）。
    图形界面请改用 :meth:`ProcessCollector.snapshot`，走 Win32 快速路径。

    :param cpu_sampler: 可选的 :class:`CpuSampler`，用于跨刷新周期计算 CPU%
    :param only_dev: 只返回 node/python/java/go 进程
    """
    ports_map = _get_ports_by_pid()
    now = time.monotonic()
    rows = []
    for p in psutil.process_iter(
        ['pid', 'name', 'status', 'memory_info', 'exe', 'create_time']
    ):
        try:
            info = p.info
            pid = info['pid']
            if pid in HIDDEN_PIDS:
                continue
            name = info['name'] or "(未知)"
            if only_dev and not any(dev in name.lower() for dev in DEV_PROCESS_NAMES):
                continue
            mem_info = info['memory_info']
            mem = mem_info.rss if mem_info else 0
            cpu = 0.0
            if cpu_sampler is not None:
                cpu_seconds = 0.0
                try:
                    times = p.cpu_times()
                    cpu_seconds = times.user + times.system
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        psutil.ZombieProcess):
                    pass
                cpu = cpu_sampler.sample_seconds(
                    pid, cpu_seconds, info['create_time'] or 0.0, now
                )
            rows.append(ProcInfo(
                pid=pid,
                name=name,
                status=info['status'] or "",
                memory_bytes=mem,
                cpu_percent=cpu,
                ports=ports_map.get(pid, ""),
                exe=info['exe'] or "",
                create_time=info['create_time'] or 0.0,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if cpu_sampler is not None:
        cpu_sampler.prune({r.pid for r in rows})
    rows.sort(key=lambda r: r.pid)
    return rows


def collect_ports(include_udp=False):
    """收集监听端口快照，返回 :class:`PortInfo` 列表（按端口升序）。

    :param include_udp: 同时包含已绑定的 UDP 端口
    :raises psutil.AccessDenied: 权限不足时无法读取其他用户的连接
    """
    rows = []
    name_cache = {}
    exe_cache = {}
    for conn in psutil.net_connections(kind='inet'):
        is_tcp = conn.type == socket.SOCK_STREAM
        if is_tcp:
            if conn.status != psutil.CONN_LISTEN:
                continue
        else:
            # UDP 连接的状态恒为 CONN_NONE，端口为 0 表示未绑定
            if not include_udp or conn.status != psutil.CONN_NONE:
                continue
        laddr = conn.laddr
        if laddr is None or not laddr.port:
            continue
        pid = conn.pid or 0
        if pid not in name_cache:
            try:
                proc = psutil.Process(pid)
                name_cache[pid] = proc.name()
                exe_cache[pid] = proc.exe() or ""
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                name_cache[pid] = ""
                exe_cache[pid] = ""
        proto = "TCP" if is_tcp else "UDP"
        if conn.family == socket.AF_INET6:
            proto += "6"
        rows.append(PortInfo(
            proto=proto,
            ip=laddr.ip,
            port=laddr.port,
            pid=pid,
            name=name_cache.get(pid, ""),
            exe=exe_cache.get(pid, ""),
        ))
    rows.sort(key=lambda r: (r.port, r.proto))
    return rows


def collect_ports_safe(include_udp=False):
    """收集端口，权限不足时返回 ``(rows, error_message)`` 而不抛异常。"""
    try:
        return collect_ports(include_udp=include_udp), ""
    except psutil.AccessDenied:
        return [], "权限不足，无法读取网络连接信息，请以管理员身份运行"
    except OSError as exc:
        return [], f"读取网络连接信息失败：{exc}"


def find_processes_by_name(name):
    """按名称模糊查找进程（不区分大小写），返回 :class:`ProcInfo` 列表。"""
    keyword = (name or "").lower()
    if not keyword:
        return []
    matches = []
    for p in psutil.process_iter(['pid', 'name', 'status', 'memory_info', 'exe', 'create_time']):
        try:
            info = p.info
            if keyword in (info['name'] or "").lower():
                mem_info = info['memory_info']
                matches.append(ProcInfo(
                    pid=info['pid'],
                    name=info['name'] or "(未知)",
                    status=info['status'] or "",
                    memory_bytes=mem_info.rss if mem_info else 0,
                    exe=info['exe'] or "",
                    create_time=info['create_time'] or 0.0,
                ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    matches.sort(key=lambda r: r.pid)
    return matches


def find_pids_by_port(port):
    """查找正在监听指定端口的 PID 列表。"""
    pids = []
    try:
        connections = psutil.net_connections(kind='inet')
    except psutil.AccessDenied:
        return pids
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN:
            continue
        if conn.laddr and conn.laddr.port == port and conn.pid:
            if conn.pid not in pids:
                pids.append(conn.pid)
    return pids


# ---------------------------------------------------------------------------
# 数据层：结束进程
# ---------------------------------------------------------------------------


def terminate_pid(pid, name="", expected_create_time=None):
    """结束单个进程，返回 :class:`KillResult`。不打印、不询问。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return KillResult(pid, name, False, "error", f"无效的 PID：{pid}")

    if pid in HIDDEN_PIDS:
        return KillResult(pid, name, False, "protected", f"系统进程 PID {pid} 无法结束")
    if pid == os.getpid():
        return KillResult(pid, name, False, "self", "不能结束本程序自身的进程")

    try:
        proc = psutil.Process(pid)
        if expected_create_time:
            # 重新读取当前占用该 PID 的进程的创建时间，检测 PID 复用
            now_ct = proc.create_time()
            if abs(now_ct - expected_create_time) > 0.001:
                return KillResult(
                    pid, name, False, "not_found",
                    f"PID {pid} 已被其他进程复用，已放弃操作",
                )
        real_name = name or proc.name()
        if real_name.lower() in PROTECTED_NAMES:
            return KillResult(
                pid, real_name, False, "protected",
                f"{real_name} 是关键系统进程，无法结束",
            )
        proc.kill()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            return KillResult(pid, real_name, False, "error", f"进程 {pid} 未在 3 秒内退出")
        return KillResult(pid, real_name, True, "", f"已结束进程 {pid} ({real_name})")
    except psutil.NoSuchProcess:
        return KillResult(pid, name, False, "not_found", f"未找到 PID 为 {pid} 的进程")
    except psutil.AccessDenied:
        return KillResult(
            pid, name, False, "access_denied",
            f"权限不足，无法结束进程 {pid}，请以管理员身份运行",
        )
    except psutil.ZombieProcess:
        return KillResult(pid, name, False, "not_found", f"进程 {pid} 已不存在")
    except Exception as exc:  # noqa: BLE001 - 兜底，避免 GUI 因单个进程异常而中断
        return KillResult(pid, name, False, "error", f"结束进程 {pid} 失败：{exc}")


def terminate_tree(pid):
    """结束进程及其所有子进程，返回 :class:`KillResult` 列表。

    ``children(recursive=True)`` 返回的是父进程在前，所以这里必须逆序处理，
    先杀子进程再杀父进程，否则父进程先退出会留下孤儿进程。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return [KillResult(pid, "", False, "error", f"无效的 PID：{pid}")]

    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return [KillResult(pid, "", False, "not_found", f"未找到 PID 为 {pid} 的进程")]
    except (psutil.AccessDenied, psutil.ZombieProcess):
        children = []

    results = []
    for child in reversed(children):  # 子进程优先
        if child.pid == os.getpid() or child.pid in HIDDEN_PIDS:
            continue
        results.append(terminate_pid(child.pid))

    results.append(terminate_pid(pid))
    return results


# ---------------------------------------------------------------------------
# 展示层：CLI 输出
# ---------------------------------------------------------------------------


def _dwidth(text):
    """文本在等宽终端里占的**列数**。

    中文、全角字符在终端里占两列。如果按字符个数算列宽，表头里的「名称」
    就会比下面的内容宽两列，整张表都错位。
    """
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text, width):
    """左对齐到 ``width`` **显示列**。"""
    text = str(text)
    return text + " " * max(0, width - _dwidth(text))


def _cell(text, width):
    """把文本截断到 ``width`` 显示列再左对齐补齐。

    进程名可能很长（例如 ``GoodixSessionService.exe``），不截断的话会挤到
    下一列里去，表格就错位了。截断时额外留出 1 列空隙，免得和后面的列粘
    在一起变成 ``...exe running`` 这种看不清的样子。
    """
    text = str(text)
    if _dwidth(text) > width:
        limit = width - 2  # 1 列给省略号，1 列留作分隔
        out = ""
        for ch in text:
            if _dwidth(out) + _dwidth(ch) > limit:
                break
            out += ch
        text = out + "…"
    return _pad(text, width)


def _print_table(headers, widths, rows):
    """打印一张对齐的表格。

    ``headers`` 是表头文字，``widths`` 是每列的显示宽度，``rows`` 是每行的
    单元格（已经由 ``_pad`` / ``_cell`` 处理好的字符串）。列与列之间用一个
    空格隔开，行尾的空格去掉。
    """
    print(" ".join(_pad(h, w) for h, w in zip(headers, widths)).rstrip())
    print(" ".join("-" * w for w in widths).rstrip())
    for row in rows:
        print(" ".join(row).rstrip())


# 各张表的表头与列宽（列宽单位是显示列）
PROC_HEADERS = ("PID", "名称", "状态", "内存", "端口")
PROC_WIDTHS = (8, 24, 12, 12, 20)
PORT_HEADERS = ("协议", "本地地址", "端口", "PID", "进程名")
PORT_WIDTHS = (8, 24, 8, 8, 24)


def _proc_row(r):
    """一个进程 → 一行表格单元格。"""
    return (
        _pad(r.pid, 8),
        _cell(r.name, 24),
        _pad(r.status, 12),
        _pad(format_size(r.memory_bytes), 12),
        str(r.ports),
    )


def _port_row(r):
    """一个端口 → 一行表格单元格。"""
    addr = f"[{r.ip}]:{r.port}" if ":" in r.ip else f"{r.ip}:{r.port}"
    return (
        _pad(r.proto, 8),
        _cell(addr, 24),
        _pad(r.port, 8),
        _pad(r.pid, 8),
        _cell(r.name, 24),
    )


def list_processes():
    """列出所有用户进程。"""
    _print_table(
        PROC_HEADERS, PROC_WIDTHS, [_proc_row(r) for r in collect_processes()]
    )


def list_dev_processes():
    """列出 node/python/java/go 用户进程。"""
    _print_table(
        PROC_HEADERS,
        PROC_WIDTHS,
        [_proc_row(r) for r in collect_processes(only_dev=True)],
    )


def list_ports():
    """列出所有监听端口。"""
    _print_table(
        PORT_HEADERS, PORT_WIDTHS, [_port_row(r) for r in collect_ports()]
    )


def kill_process(pid):
    """结束单个进程并打印结果。"""
    result = terminate_pid(pid)
    print(result.message)


def kill_by_name(name):
    matches = find_processes_by_name(name)
    if not matches:
        print(f"未找到名称包含 \"{name}\" 的进程")
        return
    if len(matches) == 1:
        pid, pname = matches[0].pid, matches[0].name
        confirm = input(f"确认终止进程 {pid} ({pname})？[y/N] ").strip().lower()
        if confirm == 'y':
            kill_process(pid)
        else:
            print("已取消")
        return
    print(f"找到 {len(matches)} 个匹配进程：")
    for r in matches:
        print(f"  PID {r.pid}: {r.name}")
    confirm = input(f"确认终止以上 {len(matches)} 个进程？[y/N] ").strip().lower()
    if confirm == 'y':
        for r in matches:
            kill_process(r.pid)
    else:
        print("已取消")


def kill_by_port(port):
    pids = find_pids_by_port(port)
    if not pids:
        print(f"未找到监听端口 {port} 的进程")
        return
    if len(pids) == 1:
        kill_process(pids[0])
    else:
        print(f"端口 {port} 对应多个进程：")
        for pid in pids:
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                name = "(未知)"
            print(f"  PID {pid}: {name}")
        confirm = input(f"确认终止以上 {len(pids)} 个进程？[y/N] ").strip().lower()
        if confirm == 'y':
            for pid in pids:
                kill_process(pid)
        else:
            print("已取消")


def kill_target(target):
    if target.isdigit():
        num = int(target)
        try:
            psutil.Process(num)
            kill_process(num)
        except psutil.NoSuchProcess:
            kill_by_port(num)
    else:
        kill_by_name(target)


def search(keyword):
    found = False
    keyword_lower = keyword.lower()

    print(f"=== 进程搜索结果（关键词：{keyword}）===")
    proc_rows = []
    for r in collect_processes():
        if keyword_lower in r.name.lower():
            proc_rows.append(
                (
                    _pad(r.pid, 8),
                    _cell(r.name, 24),
                    _pad(r.status, 12),
                    _pad(format_size(r.memory_bytes), 12),
                )
            )
    _print_table(PROC_HEADERS[:4], PROC_WIDTHS[:4], proc_rows)
    found = bool(proc_rows)

    print()
    print(f"=== 端口搜索结果（关键词：{keyword}）===")
    port_rows = []
    try:
        ports = collect_ports()
    except psutil.AccessDenied:
        print("权限不足，请以管理员身份运行")
        return
    for r in ports:
        if keyword_lower in r.name.lower() or keyword in str(r.port):
            port_rows.append(_port_row(r))
    _print_table(PORT_HEADERS, PORT_WIDTHS, port_rows)
    found = found or bool(port_rows)

    if not found:
        print(f"未找到匹配 \"{keyword}\" 的进程或端口")
