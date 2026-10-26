# killapp 桌面 GUI 设计

## 概述

在原有 CLI 工具（见 `2026-05-22-killapp-design.md`）之上做一个 **Windows 桌面应用**：
双击 `dist\killapp.exe` 就能打开一个类似任务管理器的窗口，查看进程、端口、
可执行文件路径，并结束进程 / 进程树。

三条硬性要求：

1. **双击即用** —— 打包成单文件 exe，不弹控制台窗口，不依赖 Python 环境。
2. **信息完整** —— 一屏之内能看到 PID、名称、状态、内存、CPU、监听端口、完整路径。
3. **能杀进程** —— 单个结束、整棵进程树结束；权限不足时给出明确的提权入口。

## 技术选型

| 决策 | 选择 | 理由 |
|---|---|---|
| GUI 工具包 | tkinter (ttk) | 标准库自带，零额外依赖，打包后只多几 MB |
| 打包方式 | PyInstaller `--onefile --windowed` | 单文件 exe，`runw.exe` 引导器不弹控制台 |
| 权限策略 | 普通权限启动，失败时提示并提供「管理员重启」按钮 | 避免每次启动都弹 UAC；只在真的需要时提权 |
| 主题 | `vista`（Windows 原生） | 跟随系统，125%/150% 缩放下不糊 |

本机环境：Windows 11、屏幕 2880×1800、**175% 缩放**、中文字体只有 `Microsoft YaHei UI`。

## 文件结构

```
killapp/
├── killapp.py        # CLI 入口（保留原样）
├── killapp_gui.py    # GUI 入口，PyInstaller entry point
├── gui.py            # tkinter 界面
├── core.py           # 数据层 + 展示层（CLI 与 GUI 共用）
├── winproc.py        # Win32 快速进程采集
├── winadmin.py       # UAC 提权
├── build.py          # 构建脚本
├── build.bat         # 双击构建
└── assets/
    ├── icon.png      # 窗口图标 256×256
    └── killapp.ico   # exe 图标（多尺寸）
```

## core.py：数据层 / 展示层分离

GUI 需要的是**结构化数据**，而原来的 `core.py` 全是 `print()` + `input()`，
没法复用。所以拆成两层。

### 数据层（无 print、无 input）

```python
@dataclass
class ProcInfo:
    pid: int; name: str; status: str; memory_bytes: int
    cpu_percent: float = 0.0; ports: str = ""
    exe: str = ""; create_time: float = 0.0

@dataclass
class PortInfo:
    proto: str; ip: str; port: int; pid: int = 0; name: str = ""; exe: str = ""

@dataclass
class KillResult:
    pid: int; name: str; ok: bool; error: str = ""; message: str = ""
```

| 函数 | 说明 |
|---|---|
| `collect_processes(cpu_sampler=None, only_dev=False)` | 返回 `list[ProcInfo]` |
| `collect_ports(include_udp=False)` | 返回 `list[PortInfo]`（TCP LISTEN，可选 UDP） |
| `collect_ports_safe(include_udp=False)` | `(rows, err_msg)`，权限不足时给文案而不是抛异常 |
| `find_processes_by_name(name)` / `find_pids_by_port(port)` | 查找辅助 |
| `terminate_pid(pid, name="", expected_create_time=None)` | 返回 `KillResult` |
| `terminate_tree(pid)` | 返回 `list[KillResult]`，**先子后父** |

### 展示层（CLI 输出，格式不变）

`list_processes()` / `list_dev_processes()` / `list_ports()` / `search()` /
`kill_process()` / `kill_by_name()` / `kill_by_port()` / `kill_target()`
全部改为「调数据层 → 格式化打印」。

**顺带修的 bug**：原 `list_processes()` 里有 `if not ports: continue`，
把没有监听端口的进程全过滤掉了 —— 「列出所有进程」实际只显示了有端口的那部分。
删除后 CLI `list` 才真的列出全部进程。这是 CLI 唯一一处输出内容变化。

**新增**：`_cell(text, width)` 把过长的进程名截断成 `名字…`，
否则 `GoodixSessionService.exe`（24 字符）会顶到「状态」列里去，表格错位。

**列宽按「显示宽度」算，不按字符数**：中文 / 全角字符在等宽终端里占两列，
如果用 `f"{'名称':<24}"` 这种按字符数补齐的写法，表头会比下面的内容宽两列，
整张表都错位。所以引入三个小工具：

| 函数 | 说明 |
|---|---|
| `_dwidth(text)` | 文本占的显示列数（`unicodedata.east_asian_width`，`W`/`F` 算 2 列） |
| `_pad(text, width)` | 左对齐到 `width` **显示列** |
| `_cell(text, width)` | 截断 + 补齐到 `width` 显示列；截断时额外留 1 列空隙，避免 `...exe running` 这种粘在一起的输出 |
| `_print_table(headers, widths, rows)` | 统一打印表头 / 分隔线 / 数据行，列间用一个空格隔开 |

表头、分隔线、数据行都走同一套宽度，列宽写成常量
（`PROC_WIDTHS` / `PORT_WIDTHS`），改宽度只需要改一处。

### 终止逻辑

| 场景 | 处理 |
|---|---|
| `pid == os.getpid()` | 拒绝（「不能结束本程序自身的进程」） |
| `pid in {0, 4}` | 拒绝 |
| 名称在 `PROTECTED_NAMES` 里 | 拒绝（「系统进程 PID 4 无法结束」） |
| `expected_create_time` 与当前不符 | 拒绝（PID 被复用，误杀） |
| `kill()` 后进程没退 | `wait(timeout=3)` 超时记为 error |
| 结束进程树 | `children(recursive=True)` **reversed()** 先杀子再杀父 |

### CPU 采样

`CpuSampler` 按**累计 CPU 秒数**算，而不是 `proc.cpu_percent()`：

- psutil 的 `cpu_percent()` 首调恒为 0，且必须对同一个 `Process` 实例调用；
- 累计秒数法只需要 `cpu_times()`，可以用 Win32 `GetProcessTimes` 直接拿；
- 缓存 `pid -> (create_time, last_cpu, last_time)`，用 `create_time` 判断 PID 是否被复用；
- 有锁保护，后台线程和主线程都能安全调用。

## winproc.py：性能关键

**问题**：最初用 `psutil.process_iter(['pid','name','status','memory_info'])`，
每次刷新要 **2.1–4.7 秒**，界面基本卡死。基准测试拆开看：

| 调用 | 317 个进程耗时 |
|---|---|
| `status()` | 1.47 s |
| `memory_info()` | 0.78 s |
| `create_time()` | 0.73 s |
| `name()` / `exe()` | 0.008 s（psutil 内部有缓存） |
| 裸 `OpenProcess` × 316 | 0.001 s |

结论：**不是系统调用慢，是 psutil 每次调用都重新打开进程句柄**。

**解法**：直接用 Win32 API，一个进程只开一次句柄，复用同一个句柄取
时间 / 内存 / 路径：

- `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ)`
- `GetProcessTimes` → 累计 CPU 秒数 + 创建时间
- `GetProcessMemoryInfo` → `WorkingSetSize`（RSS）
- `GetExitCodeProcess` → 状态（`STILL_ACTIVE`）
- `QueryFullProcessImageNameW` → 完整路径

**结果**：317 个进程的快速扫描 **0.005 秒**（原来 2.1 秒，慢 400 倍）。
与 psutil 逐项交叉校验 148 个可访问进程：`mem_bad=0 ct_bad=0 exe_bad=0 cpu mismatches=0`。

### 打不开的进程怎么办

约 170 个系统进程（`smss`、`csrss`、`wininit`…）没有 `PROCESS_QUERY_LIMITED_INFORMATION`
权限，普通用户打不开。处理方式：

- 先用 Win32 快速通道扫一遍，拿到 148 个进程的完整数据；
- 打不开的走 psutil 慢路径（`name`/`exe` 很便宜，`memory_info`/`status`/`cpu_times` 约 3.5ms/个）；
- 慢路径**分批后台跑**，每轮 12 个进程，结果缓存下来，界面完全不阻塞；
- 几轮之后所有进程的数据都会补齐，状态栏会显示「部分进程无法操作」。

## winadmin.py：提权

```python
is_admin() -> bool                    # shell32.IsUserAnAdmin()
relaunch_as_admin() -> (bool, str)    # 成功返回 (True, "")
```

用 `shell32.ShellExecuteW(None, "runas", exe, args, None, 1)`：

- **必须显式设 `restype = wintypes.HINSTANCE`** —— 64 位下默认的 `c_int` 会截断返回值；
- 返回值 **> 32 才算成功**，`<= 32` 是错误码，`5` 通常是用户取消了 UAC；
- 参数用 `subprocess.list2cmdline(args)` 转义，不要手拼；
- 冻结后 `sys.executable` 就是 `killapp.exe`；源码运行时用 `pythonw.exe` + `sys.argv[0]`，避免多出黑框；
- **不给 exe 加 `--uac-admin`** —— 那会导致每次启动都强制提权，与设计冲突。

## gui.py：界面

```
┌─ killapp — 进程管理器 ─────────────────────────────────────────────┐
│ [刷新] [x]自动刷新(2秒)  搜索:[______]  [ ]只看有端口 [ ]只看开发应用  │
│                                          [管理员重启][结束进程树][结束任务] │
│ ┌ 进程 ┬ 端口 ┐                                                  │
│ │ PID │ 名称 │ 状态 │ 内存 │ CPU% │ 端口 │ 路径                    │
│ │ ...                                                        ...  │
│ └──────────────────────────────────────────────────────────────┘ │
│ 共 326 个进程 · 58 个监听端口 · 最后刷新 11:24:02   普通用户（部分进程无法操作）│
└────────────────────────────────────────────────────────────────────┘
```

### 关键实现

**线程模型**：后台线程只做采集 + `queue.put()`，**绝不碰任何 widget**；
主线程用 `after(100, _poll_queue)` 轮询队列。Tk 只能在主线程操作。
连 `self._udp_var.get()` 都在主线程读，再把 `include_udp` 传进 worker。

**列宽自适应**：写死的列宽在不同缩放比例下必然截断。这里用 Treeview
**实际使用的字体**（`Style().lookup("Treeview", "font")`）测量内容宽度，
再加上 vista 主题的单元格内边距。

关键点：**上限用「一个全角汉字的宽度」当单位，而不是像素**，
这样 125% / 150% / 175% / 200% 缩放下比例一致。

进程名、端口列表都可能有极端值（一个进程能占十几个端口），不封顶的话
一列就能把「路径」列挤成一条缝。所以每列都有上限，超出的内容用
**省略号截断**（Treeview 自己只会硬切半个字）。

剩下的空间全留给可拉伸的「路径」列；减去纵向滚动条占掉的宽度，
保证表格不会出现横向滚动条。

**窗口尺寸**：进程是 DPI 感知的，`geometry()` 里的数字是**物理像素**。
本机 2880×1800、175% 缩放，写死 `1280x680` 实际只占屏幕一半，
所以按屏幕比例算：`min(屏幕宽 × 0.62, 2200) × min(屏幕高 × 0.72, 1400)`。

**排序**：表头点击切换升/降序，标题加 `▲` / `▼`。
排序作用于 dataclass 的**原始数值**（`memory_bytes` 是 int、`cpu_percent` 是 float），
格式化字符串排序是错的。

**搜索**：`<KeyRelease>` + 150ms 去抖，只过滤缓存快照，不重新调 psutil，瞬时响应。
匹配进程名 / 路径 / 端口 / PID。

**复选框筛选**：工具栏上有两个互不影响的复选框，可以和搜索框叠加使用：

| 复选框 | 进程页 | 端口页 |
|---|---|---|
| **只看有端口** | 只显示有监听端口的进程 | 不适用（端口页本来全是端口） |
| **只看开发应用** | 只显示 node / python / java 进程 | 只显示属于这些进程的端口 |

判定规则放在 `core.is_dev_app(name)` 里，和 CLI `ls` 共用同一份关键字定义。

筛选只作用于**显示**，不改变采集 —— 快照始终是全量的，勾选/取消是瞬时的，
不用等下一次刷新。状态栏会显示「当前/总数」和生效的筛选条件，
免得用户以为进程凭空消失了：

```
共 2/314 个进程 · 2/54 个监听端口（已筛选：只看有端口、只看开发应用） · 最后刷新 14:57:05
```

**右键菜单**（进程行）：结束任务 / 结束进程树 / 复制 PID / 打开文件位置。
复制 PID 用 `clip.exe` 而不是 tkinter 的 clipboard —— 后者随进程退出就失效。

**双击端口行** → 结束占用该端口的进程。

**权限不足弹窗**：自定义 `Toplevel`，带「以管理员身份重启」按钮。
非管理员下进程页照常工作，只是部分路径列显示「(无权限)」。

## killapp_gui.py：入口

```python
if sys.stdout is None:              # --windowed 下 stdout/stderr 是 None，print 会崩
    sys.stdout = sys.stderr = open(os.devnull, "w")
ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 必须在 tk.Tk() 之前
from gui import main
```

DPI 感知必须在建 Tk 根窗口**之前**设置，否则 175% 缩放下整个界面会被
系统拉伸，字体发糊。

## 构建

`build.py`：

1. 建一个干净的 `.venv` —— 本机默认 Python 是 Anaconda，直接打包会把
   numpy / pandas / matplotlib 全拖进 exe，体积能到几百 MB；
2. `pip install -r requirements.txt -r requirements-build.txt`；
3. 关掉正在运行的 `killapp.exe`（否则 `dist\killapp.exe` 被占用，覆盖不了）；
4. 清理 `__pycache__` / `build` / `dist` / `*.spec`；
5. 调 PyInstaller：

   ```
   python -m PyInstaller --noconfirm --clean --onefile --windowed
       --name killapp --icon assets\killapp.ico
       --add-data "assets\icon.png;assets"
       --hidden-import psutil
       --exclude-module numpy --exclude-module pandas --exclude-module matplotlib
       --exclude-module PIL --exclude-module IPython --exclude-module pytest
       killapp_gui.py
   ```

产物：**`dist\killapp.exe`，11.5 MB**，PE subsystem = 2（GUI，不弹控制台）。

`build.bat` 是双击即可的包装脚本。

## 边界情况

| 场景 | 处理 |
|---|---|
| `p.exe()` 权限不足 | 显示「(无权限)」，不裸调 |
| 刷新中进程消失 | 每个进程单独 try/except `NoSuchProcess` / `AccessDenied` / `ZombieProcess` |
| `net_connections()` 权限不足 | 端口页显示错误文案 + 提权按钮，进程页照常 |
| 无管理员时端口 PID 未知 | 端口行 pid 显示「-」，双击提示「该端口没有关联进程」 |
| PID 复用 | 采集记 `create_time`，kill 前比对 |
| 结束自己 | 直接拒绝 |
| 关键系统进程 | `PROTECTED_NAMES` 白名单直接拒绝 |
| CPU% 首刷全 0 | 累计秒数法，第二次刷新起有值 |
| 超长进程名 / 路径 | 按列宽上限用省略号截断 |
| 超多端口 | 同上，端口列有上限 |
| 窗口缩放 | `<Configure>` 去抖 120ms 后重排列宽 |
| 后台线程碰 Tk | worker 只 `queue.put()`，主线程 after 轮询 |
| onefile 资源找不到 | 用 `sys._MEIPASS` 定位 |
| `--windowed` 下 print 崩 | 入口把 stdout/stderr 重定向到 `os.devnull` |

## 验证结果

| 项 | 结果 |
|---|---|
| 快速扫描耗时 | 317 进程 **0.005 s** |
| 与 psutil 交叉校验 | 148 进程：内存 / 创建时间 / 路径 / CPU **0 处不一致** |
| CLI 回归 | `list` / `ls` / `ports` / `search` / `kill` 全部正常 |
| CLI 表格对齐 | 表头与数据按**显示列**对齐，中文列不错位；超长进程名截断后仍留有分隔 ✓ |
| `kill` 真实进程 | 退出码 15 ✓ |
| `kill 4`（系统进程） | 拒绝 ✓ |
| 排序 | 内存 / CPU / PID 升序降序均**按数值**排 ✓ |
| 搜索过滤 | 85 条 svchost，全部匹配 ✓ |
| 「只看有端口」 | 314 → 28 条，无端口的行 0 条 ✓ |
| 「只看开发应用」 | 只剩 node.exe / python.exe ✓ |
| 端口页跟随筛选 | 54 → 2 条，全部属于开发应用 ✓ |
| 状态栏筛选提示 | 「2/314 个进程（已筛选：…）」✓ |
| 表格溢出检查 | 0 个单元格超出列宽 ✓ |
| 横向滚动 | 两个页签都 `xview=(0.000, 1.000)`，不需要滚动 ✓ |
| exe 体积 | 11.5 MB，PE subsystem = 2（无控制台）✓ |
