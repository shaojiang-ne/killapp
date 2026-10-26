import argparse
import core


def main():
    parser = argparse.ArgumentParser(
        prog="killapp",
        description="进程查看与终止工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="列出所有用户进程")
    subparsers.add_parser("ls", help="列出 node/python/java/go 用户进程")
    subparsers.add_parser("ports", help="列出所有监听端口")

    kill_parser = subparsers.add_parser("kill", help="杀死进程")
    kill_parser.add_argument("target", help="PID 或进程名称")

    search_parser = subparsers.add_parser("search", help="搜索进程或端口")
    search_parser.add_argument("keyword", help="搜索关键词")

    args = parser.parse_args()

    dispatch = {
        "list": lambda: core.list_processes(),
        "ls": lambda: core.list_dev_processes(),
        "ports": lambda: core.list_ports(),
        "kill": lambda: core.kill_target(args.target),
        "search": lambda: core.search(args.keyword),
    }
    dispatch[args.command]()


if __name__ == "__main__":
    main()
