import psutil


def _format_size(bytes_val):
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_val / (1024 * 1024 * 1024):.1f} GB"


def list_processes():
    print(f"{'PID':<8}{'名称':<24}{'状态':<12}{'内存':<12}")
    print(f"{'----':<8}{'------------------------':<24}{'--------':<12}{'------------':<12}")
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'status', 'memory_info']):
        try:
            info = p.info
            if info['pid'] in (0, 4):
                continue
            mem = info['memory_info'].rss if info['memory_info'] else 0
            procs.append((info['pid'], info['name'] or "(未知)", info['status'], mem))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x[0])
    for pid, name, status, mem in procs:
        print(f"{pid:<8}{name:<24}{status:<12}{_format_size(mem):<12}")


def list_ports():
    print(f"{'协议':<8}{'本地地址':<24}{'端口':<8}{'PID':<8}{'进程名':<24}")
    print(f"{'----':<8}{'------------------------':<24}{'----':<8}{'----':<8}{'------------------------':<24}")
    try:
        connections = psutil.net_connections(kind='inet')
    except psutil.AccessDenied:
        print("权限不足，请以管理员身份运行")
        return
    pid_name_cache = {}
    entries = []
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN:
            continue
        laddr = conn.laddr
        pid = conn.pid
        if pid not in pid_name_cache:
            try:
                pid_name_cache[pid] = psutil.Process(pid).name() if pid else ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pid_name_cache[pid] = ""
        name = pid_name_cache[pid]
        entries.append((conn.family, laddr.ip, laddr.port, pid or 0, name))
    entries.sort(key=lambda x: x[2])
    for family, ip, port, pid, name in entries:
        proto = "TCP" if family == 2 else "TCP6" if family == 10 else "UDP"
        addr = f"{ip}:{port}" if ':' not in ip else f"[{ip}]:{port}"
        print(f"{proto:<8}{addr:<24}{port:<8}{pid:<8}{name:<24}")


def kill_process(pid):
    try:
        p = psutil.Process(pid)
        p.kill()
        print(f"已终止进程 {pid} ({p.name()})")
    except psutil.NoSuchProcess:
        print(f"未找到 PID 为 {pid} 的进程")
    except psutil.AccessDenied:
        print(f"权限不足，无法终止进程 {pid}，请以管理员身份运行")


def kill_by_name(name):
    matches = []
    name_lower = name.lower()
    for p in psutil.process_iter(['pid', 'name']):
        try:
            if name_lower in p.info['name'].lower():
                matches.append((p.info['pid'], p.info['name']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not matches:
        print(f"未找到名称包含 \"{name}\" 的进程")
        return
    if len(matches) == 1:
        pid, pname = matches[0]
        confirm = input(f"确认终止进程 {pid} ({pname})？[y/N] ").strip().lower()
        if confirm == 'y':
            kill_process(pid)
        else:
            print("已取消")
        return
    print(f"找到 {len(matches)} 个匹配进程：")
    for pid, pname in matches:
        print(f"  PID {pid}: {pname}")
    confirm = input(f"确认终止以上 {len(matches)} 个进程？[y/N] ").strip().lower()
    if confirm == 'y':
        for pid, pname in matches:
            kill_process(pid)
    else:
        print("已取消")


def kill_target(target):
    if target.isdigit():
        kill_process(int(target))
    else:
        kill_by_name(target)


def search(keyword):
    found = False
    keyword_lower = keyword.lower()

    print(f"=== 进程搜索结果（关键词：{keyword}）===")
    print(f"{'PID':<8}{'名称':<24}{'状态':<12}{'内存':<12}")
    print(f"{'----':<8}{'------------------------':<24}{'--------':<12}{'------------':<12}")
    for p in psutil.process_iter(['pid', 'name', 'status', 'memory_info']):
        try:
            info = p.info
            if info['pid'] in (0, 4):
                continue
            if keyword_lower in info['name'].lower():
                mem = info['memory_info'].rss if info['memory_info'] else 0
                print(f"{info['pid']:<8}{info['name']:<24}{info['status']:<12}{_format_size(mem):<12}")
                found = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    print()
    print(f"=== 端口搜索结果（关键词：{keyword}）===")
    print(f"{'协议':<8}{'本地地址':<24}{'端口':<8}{'PID':<8}{'进程名':<24}")
    print(f"{'----':<8}{'------------------------':<24}{'----':<8}{'----':<8}{'------------------------':<24}")
    try:
        connections = psutil.net_connections(kind='inet')
    except psutil.AccessDenied:
        print("权限不足，请以管理员身份运行")
        return
    pid_name_cache = {}
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN:
            continue
        laddr = conn.laddr
        pid = conn.pid
        if pid not in pid_name_cache:
            try:
                pid_name_cache[pid] = psutil.Process(pid).name() if pid else ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pid_name_cache[pid] = ""
        name = pid_name_cache[pid]
        if keyword_lower in name.lower() or keyword in str(laddr.port):
            proto = "TCP" if conn.family == 2 else "TCP6" if conn.family == 10 else "UDP"
            addr = f"{laddr.ip}:{laddr.port}" if ':' not in laddr.ip else f"[{laddr.ip}]:{laddr.port}"
            print(f"{proto:<8}{addr:<24}{laddr.port:<8}{pid or 0:<8}{name:<24}")
            found = True

    if not found:
        print(f"未找到匹配 \"{keyword}\" 的进程或端口")
