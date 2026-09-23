"""统一敏感配置入口：`config/platform.env`（dotenv 格式，不进版本库）。

Python 侧与 Go 侧（go_datahub 的 config/datahub.env）**分离**，各管各机器上的
敏感项，适配两端分机部署；本文件只放平台层配置（网关转发地址、Bearer 令牌）。

规则：
- 查找顺序：环境变量 ``MCP_CONFIG_FILE`` 显式指定 → 从本包/可执行文件位置
  逐级向上找 ``config/platform.env``（最多 6 级）。
- 优先级：OS 环境变量 > 配置文件（临时覆盖不必改文件；打包后改文件重启即生效）。
- 文件内容 ``KEY=VALUE``，支持 ``#`` 注释、``export`` 前缀、首尾引号。
- 键约定：``MCP_GODATAHUB_URL`` / ``MCP_GODATAHUB_TOKEN``（网关→Go）。
  仓库只保留 ``config/platform.env.example`` 模板，真实文件被 .gitignore 排除。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_platform_env", "get", "config_path"]

_loaded = False
_found_path: Path | None = None


def _candidate_roots() -> list[Path]:
    roots = [Path.cwd()]
    here = Path(__file__).resolve()
    roots.append(here.parent)
    for parent in here.parents:
        roots.append(parent)
        if (parent / ".git").exists():  # 到仓库根即可停
            break
    return roots


def config_path() -> Path | None:
    """定位配置文件；不存在返回 None（此时仅依赖 OS 环境变量）。"""
    explicit = os.environ.get("MCP_CONFIG_FILE")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for root in _candidate_roots():
        p = root / "config" / "platform.env"
        if p.is_file():
            return p
    return None


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def load_platform_env(override: bool = False) -> dict[str, str]:
    """加载配置文件并注入 os.environ（幂等，只解析一次）。

    默认不覆盖已有 OS 环境变量；override=True 时文件优先（一般不用）。
    返回文件的键值副本，便于测试断言。
    """
    global _loaded, _found_path
    if not _loaded:
        _loaded = True
        path = config_path()
        _found_path = path
        if path is not None:
            for key, val in _parse(path).items():
                if override or key not in os.environ:
                    os.environ[key] = val
    return dict(_parse(_found_path)) if _found_path else {}


def get(key: str, default: str | None = None) -> str | None:
    """读取配置项：OS 环境变量优先，其次配置文件（自动触发加载）。"""
    load_platform_env()
    return os.environ.get(key, default)


def _reset() -> None:
    """清除加载缓存（仅供测试使用）。"""
    global _loaded, _found_path
    _loaded = False
    _found_path = None
