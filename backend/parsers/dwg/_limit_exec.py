"""在新进程内施加内存限制后执行命令，避免多线程父进程的 preexec_fn 死锁。"""
import os
import sys


def main():
    limit_mb = int(sys.argv[1])
    if sys.platform.startswith("linux"):
        try:
            import resource

            limit = limit_mb * 1024 * 1024
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (OSError, ValueError) as exc:
            print(f"内存上限设置失败，继续尽力执行：{exc}", file=sys.stderr)
    else:
        print("当前系统仅提供进程隔离与超时；未强制地址空间上限", file=sys.stderr)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    main()
