"""从本地 .env 注入环境变量（design.md §6.2：凭据不入库、不硬编码）。

进程入口在读配置和访问外部服务之前调用。已存在的环境变量不覆盖，
因此 shell 导出和 `uv run --env-file .env` 优先于文件内容。
文件不存在时直接返回，测试与未配置凭据的环境不受影响。
"""

import os
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> None:
    """把 KEY=VALUE 行写入 os.environ；不覆盖已有变量，忽略空行和注释。"""
    file = Path(path)
    if not file.is_file():
        return
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
