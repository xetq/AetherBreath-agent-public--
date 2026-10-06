#!/usr/bin/env python3
"""
列举本脚本所在文件夹下的所有文件和子文件夹
用于验证技能系统能正常加载并执行附属脚本
"""

import os
from pathlib import Path


def list_files_in_skill_dir():
    """列举 skills/test-skill/ 目录下的所有内容"""
    script_dir = Path(__file__).parent.absolute()
    items = sorted(script_dir.iterdir())

    print(f"📂 技能目录: {script_dir}")
    print("-" * 40)

    for item in items:
        if item.is_dir():
            print(f"  📁 {item.name}/")
        else:
            size = item.stat().st_size
            print(f"  📄 {item.name} ({size} bytes)")

    print("-" * 40)
    print(f"✅ 共 {len(items)} 个项目")


if __name__ == "__main__":
    list_files_in_skill_dir()