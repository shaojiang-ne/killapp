"""killapp 图形界面：类似 Windows 任务管理器的进程 / 端口查看器。"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import core
import winadmin

# 列定义：(key, 标题, 宽度, 对齐, 是否随窗口拉伸)
PROC_COLUMNS = [
    ("pid", "PID", 70, "e", False),
    ("name", "名称", 190, "w", False),
    ("status", "状态", 100, "w", False),
    ("memory", "内存", 95, "e", False),
    ("cpu", "CPU%", 70, "e", False),
    ("ports", "端口", 150, "w", False),
    ("exe", "路径", 380, "w", True),
]

PORT_COLUMNS = [
    ("proto", "协议", 60, "w", False),
    ("laddr", "本地地址", 190, "w", False),
    ("port", "端口", 70, "e", False),
    ("pid", "PID", 70, "e", False),
    ("name", "进程名", 200, "w", False),
    ("exe", "路径", 380, "w", True),
]

# 排序取值函数：必须基于原始数值，格式化后的字符串排序是错的
PROC_SORT_KEYS = {
    "pid": lambda r: r.pid,
    "name": lambda r: (r.name or "").lower(),
    "status": lambda r: (r.status or "").lower(),
    "memory": lambda r: r.memory_bytes,
    "cpu": lambda r: r.cpu_percent,
    "ports": lambda r: r.ports or "",
    "exe": lambda r: (r.exe or "").lower(),
}

PORT_SORT_KEYS = {
    "proto": lambda r: r.proto,
    "laddr": lambda r: r.ip or "",
    "port": lambda r: r.port,
    "pid": lambda r: r.pid,
    "name": lambda r: (r.name or "").lower(),
    "exe": lambda r: (r.exe or "").lower(),
}

AUTO_REFRESH_MS = 2000
FILTER_DEBOUNCE_MS = 150

# 每列的宽度上限，单位是「一个全角汉字的宽度」。
# 进程名、端口列表都可能很长（一个进程能占十几个端口），不封顶的话一列就能把
# 「路径」列挤成一条缝，表格还得横向滚动，很难受。
# 用汉字宽度当单位而不是像素，这样在 125% / 150% / 200% 缩放下比例都一致。
COLUMN_CAP_UNITS = {
    "pid": 5,
    "name": 11,
    "status": 6,
    "memory": 7,
    "cpu": 5,
    "ports": 10,
    "proto": 5,
    "laddr": 12,
    "port": 5,
    "exe": 40,
}

# 「路径」列至少留这么宽
EXE_MIN_UNITS = 12

# vista 主题单元格左右各有一圈内边距
CELL_PAD = 16

CJK_FONTS = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "SimHei",
    "SimSun",
    "NSimSun",
    "Microsoft JhengHei",
)


def _resource_dir():
    """打包成 onefile 后资源在临时解包目录里，需要用 sys._MEIPASS 定位。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


class KillApp(tk.Tk):
    """主窗口。"""

    def _set_initial_geometry(self):
        """按屏幕大小开一个合适的窗口。

        进程是 DPI 感知的，``geometry`` 里的数字是**物理像素**；本机是 2880x1800
        的屏幕、175% 缩放，写死 1280x680 实际只占了屏幕的一半。所以按屏幕比例算。
        """
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        width = max(1100, min(int(sw * 0.62), 2200))
        height = max(700, min(int(sh * 0.72), 1400))
        self.geometry(f"{width}x{height}")
        self.minsize(760, 460)

    def __init__(self):
        super().__init__()
        self.title("killapp — 进程管理器")
        self._set_initial_geometry()

        self._family = self._pick_font()
        self._apply_theme()
        self._apply_icon()

        # 数据
        self._collector = core.default_collector
        self._proc_snapshot = []
        self._port_snapshot = []
        self._ports_error = ""
        self._proc_rows = {}
        self._port_rows = {}
        self._filter_kw = ""
        self._filter_after_id = None
        self._auto_id = None
        self._poll_id = None
        self._closing = False
        self._gen = 0
        self._queue = queue.Queue()
        self._collect_lock = threading.Lock()

        # 排序状态
        self._sort_state = {}
        self._col_titles = {}
        self._font_obj = None
        self._measure_cache = {}

        self._build_toolbar()
        self._build_statusbar()
        self._build_notebook()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._poll_queue()
        self._refresh()
        self._schedule_auto()

    # ------------------------------------------------------------------
    # 外观
    # ------------------------------------------------------------------

    def _pick_font(self):
        """挑一个支持中文的字体，本机只有 Microsoft YaHei UI。"""
        try:
            available = {f.lower() for f in tkfont.families(self)}
        except tk.TclError:
            return "TkDefaultFont"
        for family in CJK_FONTS:
            if family.lower() in available:
                return family
        return "TkDefaultFont"

    def _apply_theme(self):
        self.option_add("*Font", (self._family, 9))
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "xpnative", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        style.configure("Treeview", font=(self._family, 9), rowheight=22)
        style.configure("Treeview.Heading", font=(self._family, 9, "bold"))
        style.configure("TButton", padding=(10, 4))
        style.configure("Kill.TButton", padding=(12, 5))

    def _apply_icon(self):
        png = os.path.join(_resource_dir(), "assets", "icon.png")
        if not os.path.exists(png):
            return
        try:
            # 必须保留引用，否则 PhotoImage 会被回收，窗口图标变空白
            self._icon_image = tk.PhotoImage(file=png)
            self.iconphoto(True, self._icon_image)
        except (tk.TclError, OSError):
            pass

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side="top", fill="x")

        # 先摆右侧的操作按钮，保证窗口再窄也不会把它们挤掉
        ttk.Button(
            bar, text="结束任务", style="Kill.TButton",
            command=self._on_kill_selected,
        ).pack(side="right")
        ttk.Button(
            bar, text="结束进程树", style="Kill.TButton",
            command=self._on_kill_tree,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            bar, text="管理员重启", style="Kill.TButton",
            command=self._on_restart_admin,
        ).pack(side="right", padx=(0, 8))

        ttk.Button(bar, text="刷新", command=self._refresh).pack(side="left")

        self._auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bar, text="自动刷新(2秒)", variable=self._auto_var,
            command=self._on_auto_toggle,
        ).pack(side="left", padx=(12, 0))

        ttk.Label(bar, text="搜索:").pack(side="left", padx=(16, 4))
        self._filter_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self._filter_var, width=20)
        entry.pack(side="left")
        entry.bind("<KeyRelease>", self._on_filter_change)

        # 复选框筛选
        self._only_ports_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="只看有端口", variable=self._only_ports_var,
            command=self._on_filter_change,
        ).pack(side="left", padx=(16, 0))
        self._only_dev_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="只看开发应用", variable=self._only_dev_var,
            command=self._on_filter_change,
        ).pack(side="left", padx=(12, 0))

    def _build_notebook(self):
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(side="top", fill="both", expand=True, padx=8)

        proc_tab = ttk.Frame(self._notebook)
        self._notebook.add(proc_tab, text="进程")
        proc_frame, self.proc_tree = self._make_tree(
            proc_tab, PROC_COLUMNS, PROC_SORT_KEYS
        )
        proc_frame.grid(row=0, column=0, sticky="nsew")
        proc_tab.rowconfigure(0, weight=1)
        proc_tab.columnconfigure(0, weight=1)
        self.proc_tree.bind("<Button-3>", self._on_proc_right_click)
        self.proc_tree.bind("<Double-Button-1>", self._on_proc_double_click)

        port_tab = ttk.Frame(self._notebook)
        self._notebook.add(port_tab, text="端口")

        port_bar = ttk.Frame(port_tab, padding=(6, 4))
        port_bar.grid(row=0, column=0, sticky="ew")
        self._udp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            port_bar, text="包含 UDP", variable=self._udp_var,
            command=self._refresh,
        ).pack(side="left")
        ttk.Button(port_bar, text="刷新", command=self._refresh).pack(side="left", padx=(10, 0))

        self._ports_msg = ttk.Label(port_tab, text="", foreground="#b00020", padding=8)
        self._ports_msg.grid(row=1, column=0, sticky="ew")

        port_tree_frame, self.port_tree = self._make_tree(
            port_tab, PORT_COLUMNS, PORT_SORT_KEYS
        )
        port_tree_frame.grid(row=2, column=0, sticky="nsew")
        port_tab.rowconfigure(2, weight=1)
        port_tab.columnconfigure(0, weight=1)
        self.port_tree.bind("<Double-Button-1>", self._on_port_double_click)
        self.port_tree.bind("<Button-3>", self._on_port_right_click)

    def _build_statusbar(self):
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(side="bottom", fill="x")
        self._status_var = tk.StringVar(value="正在加载...")
        ttk.Label(bar, textvariable=self._status_var).pack(side="left")
        self._priv_var = tk.StringVar(
            value="管理员模式" if winadmin.is_admin() else "普通用户（部分进程无法操作）"
        )
        ttk.Label(bar, textvariable=self._priv_var).pack(side="right")

    def _make_tree(self, parent, columns, sort_keys):
        """构造一个带排序表头和滚动条的 Treeview，返回 ``(frame, tree)``。

        frame 的布局由调用方决定（内部固定用 grid，不能和 pack 混用）。
        """
        frame = ttk.Frame(parent)

        tree = ttk.Treeview(
            frame,
            columns=[c[0] for c in columns],
            show="headings",
            selectmode="extended",
        )
        for key, title, width, anchor, stretch in columns:
            tree.heading(
                key, text=title, anchor=anchor,
                command=lambda k=key: self._toggle_sort(k, tree),
            )
            tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=stretch)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self._col_titles[tree] = {c[0]: c[1] for c in columns}
        self._sort_state[tree] = {"key": None, "reverse": False}
        tree._sort_keys = sort_keys  # 供排序时取原始数值
        tree._spec = columns
        tree._vsb = vsb
        tree.bind("<Configure>", lambda e, t=tree: self._on_tree_resize(t))
        return frame, tree

    def _on_tree_resize(self, tree):
        """窗口尺寸变了要重新分配列宽（去抖，避免拖动时反复重排）。"""
        after_id = getattr(tree, "_resize_after", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        tree._resize_after = self.after(120, lambda: self._relayout(tree))

    def _relayout(self, tree):
        """按新的窗口宽度重算列宽并重新渲染（内容本身没变）。"""
        tree._resize_after = None
        if tree is self.proc_tree:
            rows = self._filtered_procs()
            values_fn, iid_fn = self._proc_values, (lambda r: str(r.pid))
        else:
            rows = self._filtered_ports()
            values_fn = self._port_values
            iid_fn = lambda r: f"{r.proto}:{r.ip}:{r.port}:{r.pid}"
        state = self._sort_state[tree]
        getter = tree._sort_keys.get(state["key"]) if state["key"] else None
        if getter is not None:
            rows = sorted(rows, key=getter, reverse=state["reverse"])
        self._render_rows(tree, rows, values_fn, iid_fn)

    def _autosize(self, tree, values_list):
        """按实际渲染字体调整列宽，返回每列的最终宽度。

        不同缩放比例下字体宽度不一样，写死的列宽会把内容截断。这里用 Treeview
        真正使用的字体测量，再按 :data:`COLUMN_CAP_UNITS` 封顶 —— 否则一个超长进程名
        就能把「路径」列挤没。剩下的空间全留给可拉伸的「路径」列。
        """
        font = self._tree_font()
        unit = self._measure_text(font, "中") or 12
        widths = {}
        stretch_key = None

        for index, (key, title, _width, _anchor, stretch) in enumerate(tree._spec):
            cap = COLUMN_CAP_UNITS.get(key, 12) * unit + CELL_PAD
            need = self._measure_text(font, title) + CELL_PAD + 2
            for values in values_list:
                w = self._measure_text(font, str(values[index])) + CELL_PAD + 2
                if w > need:
                    need = w
            widths[key] = min(need, cap)
            if stretch:
                stretch_key = key

        if stretch_key is not None:
            cap = COLUMN_CAP_UNITS.get(stretch_key, 30) * unit + CELL_PAD
            low = EXE_MIN_UNITS * unit + CELL_PAD
            fixed = sum(w for k, w in widths.items() if k != stretch_key)
            # 纵向滚动条占掉的那一条也要算进去，否则最后一列会被挤出可视区
            sb = getattr(tree, "_vsb", None)
            sb_w = sb.winfo_width() if sb is not None else 0
            avail = tree.winfo_width() - sb_w
            if avail > fixed + low:
                widths[stretch_key] = max(low, min(avail - fixed, cap))
            else:
                widths[stretch_key] = low

        for key, w in widths.items():
            tree.column(key, width=w, minwidth=w, stretch=0)
        return widths

    def _measure_text(self, font, text):
        """测量文本像素宽度，带缓存。"""
        cached = self._measure_cache.get(text)
        if cached is None:
            cached = font.measure(text)
            self._measure_cache[text] = cached
        return cached

    def _fit_text(self, text, max_px, font):
        """把文本裁到 ``max_px`` 像素以内，超出的部分用省略号收尾。

        Treeview 自己只会硬生生切掉半个字，加省略号看起来更完整。
        """
        text = str(text)
        if max_px <= 0:
            return text
        if self._measure_text(font, text) <= max_px:
            return text
        ell = self._measure_text(font, "…")
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._measure_text(font, text[:mid]) + ell <= max_px:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo] + "…") if lo > 0 else ""

    def _tree_font(self):
        """Treeview 实际使用的字体对象。"""
        if self._font_obj is None:
            spec = ttk.Style(self).lookup("Treeview", "font") or self._family
            try:
                self._font_obj = tkfont.Font(root=self, font=spec)
            except tk.TclError:
                self._font_obj = tkfont.Font(root=self, family=self._family, size=9)
        return self._font_obj

    # ------------------------------------------------------------------
    # 排序 / 过滤
    # ------------------------------------------------------------------

    def _toggle_sort(self, key, tree):
        state = self._sort_state[tree]
        if state["key"] == key:
            state["reverse"] = not state["reverse"]
        else:
            state["key"] = key
            state["reverse"] = False
        self._update_headings(tree)
        self._refresh_table(tree)

    def _update_headings(self, tree):
        state = self._sort_state[tree]
        for key, title in self._col_titles[tree].items():
            if key == state["key"]:
                tree.heading(key, text=f"{title} {'▼' if state['reverse'] else '▲'}")
            else:
                tree.heading(key, text=title)

    def _on_filter_change(self, *_args):
        # 去抖：连续敲键时只在停顿后过滤一次
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(FILTER_DEBOUNCE_MS, self._apply_filter)

    def _apply_filter(self):
        self._filter_after_id = None
        kw = self._filter_var.get().strip().lower()
        self._filter_kw = kw
        self._refresh_table(self.proc_tree)
        self._refresh_table(self.port_tree)
        self._update_status()

    def _filter_flags(self):
        """当前生效的复选框筛选条件。"""
        return (
            bool(self._only_ports_var.get()),
            bool(self._only_dev_var.get()),
        )

    def _filtered_procs(self):
        kw = self._filter_kw
        only_ports, only_dev = self._filter_flags()
        out = []
        for r in self._proc_snapshot:
            if only_ports and not (r.ports or "").strip():
                continue
            if only_dev and not core.is_dev_app(r.name):
                continue
            if kw and not (kw in (r.name or "").lower()
                           or kw in (r.exe or "").lower()
                           or kw in (r.ports or "").lower()
                           or kw in str(r.pid)):
                continue
            out.append(r)
        return out

    def _filtered_ports(self):
        kw = self._filter_kw
        _only_ports, only_dev = self._filter_flags()
        out = []
        for r in self._port_snapshot:
            # 端口页天生就是「有端口」的，只有开发应用这一条对它有意义
            if only_dev and not core.is_dev_app(r.name):
                continue
            if kw and not (kw in (r.name or "").lower()
                           or kw in (r.exe or "").lower()
                           or kw in str(r.port)
                           or kw in (r.ip or "").lower()
                           or kw in r.proto.lower()):
                continue
            out.append(r)
        return out

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    def _refresh(self, *_args):
        """启动一次后台刷新。"""
        if self._closing:
            return
        self._gen += 1
        gen = self._gen
        # Tk 只能在主线程访问，所以在这里就把选项读出来传给后台线程
        include_udp = self._udp_var.get()
        threading.Thread(
            target=self._worker, args=(gen, include_udp), daemon=True
        ).start()

    def _worker(self, gen, include_udp):
        """后台线程：只收集数据并入队，绝不触碰任何 widget。"""
        try:
            with self._collect_lock:
                procs = self._collector.snapshot()
                ports, err = core.collect_ports_safe(include_udp=include_udp)
        except Exception as exc:  # noqa: BLE001 - 兜底，别让线程静默死掉
            procs, ports, err = [], [], f"采集失败：{exc}"
        self._queue.put(("snapshot", gen, procs, ports, err))

    def _poll_queue(self):
        if self._closing:
            return
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "snapshot":
                _, gen, procs, ports, err = msg
                if gen == self._gen:  # 丢弃过期结果
                    self._apply_snapshot(procs, ports, err)
        self._poll_id = self.after(100, self._poll_queue)

    def _apply_snapshot(self, procs, ports, err):
        self._proc_snapshot = procs
        self._port_snapshot = ports
        self._ports_error = err
        self._refresh_table(self.proc_tree)
        self._refresh_table(self.port_tree)
        self._update_status()

    def _update_status(self):
        """状态栏：显示当前看到的数量，有筛选时同时标出总数。"""
        total_p, total_o = len(self._proc_snapshot), len(self._port_snapshot)
        shown_p, shown_o = len(self._filtered_procs()), len(self._filtered_ports())
        only_ports, only_dev = self._filter_flags()

        marks = []
        if self._filter_kw:
            marks.append(f"搜索「{self._filter_kw}」")
        if only_ports:
            marks.append("只看有端口")
        if only_dev:
            marks.append("只看开发应用")
        suffix = f"（已筛选：{'、'.join(marks)}）" if marks else ""

        if shown_p == total_p and shown_o == total_o:
            counts = f"共 {total_p} 个进程 · {total_o} 个监听端口"
        else:
            counts = (
                f"共 {shown_p}/{total_p} 个进程 · {shown_o}/{total_o} 个监听端口"
            )
        self._status_var.set(
            f"{counts}{suffix} · 最后刷新 {time.strftime('%H:%M:%S')}"
        )

    def _refresh_table(self, tree):
        """按当前过滤 + 排序条件重建表格内容（保留选中行与滚动位置）。"""
        if tree is self.proc_tree:
            rows = self._filtered_procs()
            values_fn = self._proc_values
            iid_fn = lambda r: str(r.pid)
            self._proc_rows = {str(r.pid): r for r in rows}
        else:
            rows = self._filtered_ports()
            values_fn = self._port_values
            iid_fn = lambda r: f"{r.proto}:{r.ip}:{r.port}:{r.pid}"
            self._port_rows = {
                f"{r.proto}:{r.ip}:{r.port}:{r.pid}": r for r in rows
            }

        state = self._sort_state[tree]
        getter = tree._sort_keys.get(state["key"]) if state["key"] else None
        if getter is not None:
            rows = sorted(rows, key=getter, reverse=state["reverse"])

        self._render_rows(tree, rows, values_fn, iid_fn)

        if tree is self.port_tree:
            if self._ports_error:
                self._ports_msg.configure(text=self._ports_error)
                self._ports_msg.grid()
            else:
                self._ports_msg.grid_remove()

    def _render_rows(self, tree, rows, values_fn, iid_fn):
        """把行写进表格：先按内容定列宽，再把超长的内容裁成省略号。"""
        raw = [values_fn(r) for r in rows]
        widths = self._autosize(tree, raw)
        font = self._tree_font()
        keys = [spec[0] for spec in tree._spec]

        selected = set(tree.selection())
        yview = tree.yview()
        children = tree.get_children("")
        if children:
            tree.delete(*children)
        for r, values in zip(rows, raw):
            shown = tuple(
                self._fit_text(v, widths.get(k, 200) - CELL_PAD, font)
                for v, k in zip(values, keys)
            )
            tree.insert("", "end", iid=iid_fn(r), values=shown)
        still = [i for i in selected if tree.exists(i)]
        if still:
            tree.selection_set(still)
        if yview and yview[0] > 0:
            tree.yview_moveto(yview[0])

    def _proc_values(self, r):
        return (
            r.pid,
            r.name,
            core.status_cn(r.status),
            core.format_size(r.memory_bytes),
            f"{r.cpu_percent:.1f}",
            r.ports or "-",
            r.exe or "(无权限)",
        )

    def _port_values(self, r):
        addr = f"[{r.ip}]:{r.port}" if ":" in (r.ip or "") else f"{r.ip}:{r.port}"
        return (
            r.proto,
            addr,
            r.port,
            r.pid if r.pid else "-",
            r.name or "(未知)",
            r.exe or "(无权限)",
        )

    def _on_auto_toggle(self):
        if self._auto_var.get():
            self._schedule_auto()
        else:
            self._cancel_auto()

    def _schedule_auto(self):
        if self._closing:
            return
        self._auto_id = self.after(AUTO_REFRESH_MS, self._auto_tick)

    def _auto_tick(self):
        if self._closing:
            return
        self._auto_id = None
        self._refresh()
        if self._auto_var.get():
            self._schedule_auto()

    def _cancel_auto(self):
        if self._auto_id is not None:
            try:
                self.after_cancel(self._auto_id)
            except tk.TclError:
                pass
            self._auto_id = None

    # ------------------------------------------------------------------
    # 选择与操作
    # ------------------------------------------------------------------

    def _current_tab_tree(self):
        return self.port_tree if self._notebook.index(self._notebook.select()) == 1 else self.proc_tree

    def _selected_targets(self):
        """返回当前页签里选中的 [(pid, name, create_time)]。"""
        tree = self._current_tab_tree()
        targets = []
        for iid in tree.selection():
            if tree is self.proc_tree:
                row = self._proc_rows.get(iid)
                if row and row.pid:
                    targets.append((row.pid, row.name, row.create_time))
            else:
                row = self._port_rows.get(iid)
                if row and row.pid:
                    targets.append((row.pid, row.name, 0.0))
        return targets

    def _on_kill_selected(self):
        targets = self._selected_targets()
        if not targets:
            messagebox.showinfo("提示", "请先在表格中选择要结束的进程")
            return
        self._confirm_and_kill(targets)

    def _on_kill_tree(self):
        targets = self._selected_targets()
        if not targets:
            messagebox.showinfo("提示", "请先在表格中选择要结束的进程")
            return
        self._confirm_and_kill(targets[:1], tree=True)

    def _confirm_and_kill(self, targets, tree=False):
        if not targets:
            return
        own_pid = os.getpid()
        if any(pid == own_pid for pid, _, _ in targets):
            messagebox.showwarning("警告", "不能结束本程序自身的进程")
            return

        detail = "\n".join(f"PID {pid}    {name}" for pid, name, _ in targets)
        verb = "结束进程树（含所有子进程）" if tree else "结束任务"
        if not messagebox.askyesno(
            "确认", f"确定要{verb}以下进程吗？\n\n{detail}"
        ):
            return

        results = []
        if tree:
            results = core.terminate_tree(targets[0][0])
        else:
            for pid, name, create_time in targets:
                results.append(core.terminate_pid(
                    pid, name=name, expected_create_time=create_time or None
                ))

        ok = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]
        denied = [r for r in failed if r.error == "access_denied"]

        if ok:
            messagebox.showinfo(
                "完成",
                f"已结束 {len(ok)} 个进程。\n" + "\n".join(r.message for r in ok[:10]),
            )
        if denied:
            self._show_access_denied(
                "权限不足，以下进程无法结束：\n"
                + "\n".join(f"PID {r.pid}  {r.name}" for r in denied)
                + "\n\n部分系统进程需要管理员权限才能操作。"
            )
        elif failed:
            messagebox.showwarning(
                "部分失败", "\n".join(r.message for r in failed[:10])
            )
        self._refresh()

    def _show_access_denied(self, message):
        """权限不足弹窗，带「以管理员身份重启」按钮。"""
        win = tk.Toplevel(self)
        win.title("权限不足")
        win.transient(self)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, justify="left", wraplength=420).pack(anchor="w")
        btns = ttk.Frame(frame)
        btns.pack(anchor="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left")
        ttk.Button(
            btns, text="以管理员身份重启", command=self._on_restart_admin
        ).pack(side="left", padx=(0, 8))
        win.grab_set()

    def _on_restart_admin(self):
        ok, err = winadmin.relaunch_as_admin()
        if ok:
            self._closing = True
            self.destroy()
        else:
            messagebox.showwarning("以管理员身份重启", err)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------

    def _on_proc_right_click(self, event):
        iid = self.proc_tree.identify_row(event.y)
        if iid:
            self.proc_tree.selection_set(iid)
            self.proc_tree.focus(iid)
        row = self._proc_rows.get(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="结束任务", command=self._on_kill_selected)
        menu.add_command(label="结束进程树", command=self._on_kill_tree)
        menu.add_separator()
        menu.add_command(
            label="复制 PID",
            command=lambda: self._copy_pid(row.pid if row else None),
        )
        menu.add_command(
            label="打开文件位置",
            command=lambda: self._open_file_location(row.exe if row else ""),
        )
        menu.post(event.x_root, event.y_root)
        menu.bind("<FocusOut>", lambda _e: menu.unpost())

    def _on_port_right_click(self, event):
        iid = self.port_tree.identify_row(event.y)
        if iid:
            self.port_tree.selection_set(iid)
            self.port_tree.focus(iid)
        row = self._port_rows.get(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="结束占用进程", command=self._on_kill_selected)
        menu.add_separator()
        menu.add_command(
            label="复制 PID",
            command=lambda: self._copy_pid(row.pid if row else None),
        )
        menu.add_command(
            label="打开文件位置",
            command=lambda: self._open_file_location(row.exe if row else ""),
        )
        menu.post(event.x_root, event.y_root)
        menu.bind("<FocusOut>", lambda _e: menu.unpost())

    def _on_proc_double_click(self, event):
        """双击进程行：定位到可执行文件。"""
        iid = self.proc_tree.identify_row(event.y)
        row = self._proc_rows.get(iid)
        if row:
            self._open_file_location(row.exe)

    def _on_port_double_click(self, event):
        """双击端口行：结束占用该端口的进程。"""
        iid = self.port_tree.identify_row(event.y)
        row = self._port_rows.get(iid)
        if not row:
            return
        if not row.pid:
            messagebox.showinfo("提示", "该端口没有关联进程（PID 未知）")
            return
        self._confirm_and_kill([(row.pid, row.name, 0.0)])

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def _copy_pid(self, pid):
        if not pid:
            messagebox.showinfo("提示", "没有可复制的 PID")
            return
        self.clipboard_clear()
        self.clipboard_append(str(pid))
        self.update_idletasks()
        # tkinter 的剪贴板绑定进程生命周期，关掉程序就没了，用 clip.exe 补一份
        try:
            subprocess.Popen(
                "clip", text=True, stdin=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).communicate(str(pid))
        except (OSError, ValueError):
            pass

    def _open_file_location(self, path):
        if not path or not os.path.exists(path):
            messagebox.showinfo("提示", "无法获取该进程的可执行文件路径")
            return
        try:
            # explorer 的 /select 需要把路径和逗号放在同一个参数里
            subprocess.Popen(["explorer", f'/select,"{os.path.normpath(path)}"'])
        except OSError as exc:
            messagebox.showwarning("打开失败", f"无法打开文件位置：{exc}")

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------

    def _on_close(self):
        self._closing = True
        self._cancel_auto()
        for after_id in (self._poll_id, self._filter_after_id):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        self.destroy()


def main():
    app = KillApp()
    app.mainloop()


if __name__ == "__main__":
    main()
