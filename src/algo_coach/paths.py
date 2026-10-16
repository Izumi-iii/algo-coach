"""Reject linked/reparse-point paths rather than silently following them for writes."""

import re
import stat
from pathlib import Path, PurePosixPath


def reject_links(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            attributes = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attributes.st_mode) or (
            getattr(attributes, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("归档路径包含符号链接或 junction，请选择实际目录")


def practice_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    reject_links(path)
    root = path.resolve()
    if not root.is_dir() or root.parent == root:
        raise ValueError("请先创建并选择独立的练习目录，不能使用磁盘根目录")
    source_root = next(
        (p for p in Path(__file__).parents if (p / "pyproject.toml").is_file()), None
    )
    if source_root and root.is_relative_to(source_root):
        raise ValueError("练习目录应与 algo-coach 应用仓库分开")
    return root


def safe_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or "\\" in relative
        or any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts)
        or PurePosixPath(relative).is_absolute()
    ):
        raise ValueError("归档相对路径不合法")
    target = root.joinpath(*parts)
    reject_links(target)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("归档路径越出练习目录")
    return target
