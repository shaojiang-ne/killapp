# killapp - 进程查看与终止 CLI 工具设计

## 概述

一个 Python CLI 工具，用于在 Windows 上查看用户进程及端口占用，并支持杀死进程。

## 技术栈

- Python 3.8+
- psutil（进程/端口信息获取）
- argparse（CLI 参数解析）

## 命令结构

```
killapp list              # 列出所有用户进程
killapp ports             # 列出所有监听端口
killapp kill <PID|名称>   # 杀死进程
killapp search <关键词>   # 搜索进程或端口
```

## 文件结构

```
killapp/
├── killapp.py       # 入口，CLI 参数解析
├── core.py          # 核心逻辑
└── requirements.txt # 依赖
```

## 核心模块 (core.py)

### list_processes()

- 调用 `psutil.process_iter(['pid', 'name', 'status', 'memory_info'])` 获取进程信息
- 过滤系统进程（PID 0, 4 等 System Idle Process）
- 输出表格：PID、名称、状态、内存占用
- 按 PID 排序

### list_ports()

- 调用 `psutil.net_connections(kind='inet')` 获取网络连接
- 过滤 LISTEN 状态的连接
- 关联 PID 到进程名
- 输出表格：协议、本地地址、端口、PID、进程名
- 按端口号排序

### kill_process(pid)

- 调用 `psutil.Process(pid).kill()`
- 捕获 `psutil.AccessDenied`：提示以管理员身份运行
- 捕获 `psutil.NoSuchProcess`：提示进程不存在
- 成功后输出确认信息

### kill_by_name(name)

- 模糊匹配进程名（不区分大小写）
- 如果匹配到多个进程，列出所有匹配项让用户确认
- 确认后逐个调用 kill_process

### search(keyword)

- 在进程列表中按名称搜索
- 在端口列表中按端口号搜索
- 合并结果展示

## 输出格式

所有列表输出使用等宽表格格式，列头加粗标识：

```
PID    名称              状态      内存
----   ----------------  --------  ----------
1234   chrome.exe        running   256.3 MB
5678   node.exe          running   128.1 MB
```

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| 权限不足 | 提示"请以管理员身份运行" |
| 进程不存在 | 提示"未找到 PID 为 X 的进程" |
| 无匹配结果 | 提示"未找到匹配的进程或端口" |
| kill_by_name 多匹配 | 列出匹配项，要求用户确认 |

## 依赖

- psutil >= 5.9.0
