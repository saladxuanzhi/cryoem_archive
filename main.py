#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CryoTape — 将冷冻电镜（Cryo-EM）原始 Movie 数据归档到 LTO-6 LTFS 磁带的 CLI 工具。

设计约束（来自冷冻电镜数据归档业务需求）：
    1. 零本地中转：直接 tar -cf 写入 LTFS 挂载点
    2. 单盘独立防灾：严禁跨盘切分（不用 split / tar -M）
    3. 严禁 CPU 压缩：不加 -z / -j / -J
    4. 动态容量识别：shutil.disk_usage 读取挂载点剩余空间
    5. 字典序排序：确保照片拍摄编号连续
    6. 双层 CSV 台账：本地总 CSV + 每盘磁带内 CSV
    7. 断点续写：扫描本地 CSV pending 行，磁带识别后恢复
    8. Manifest 索引：盘内纯文本文件清单

用法：
    python main.py /path/to/project
    python main.py /path/to/project --ltfs-mount /mnt/ltfs --dry-run
    python main.py --project-dir /path/to/project --non-interactive --yes
    python -m cryotape /path/to/project

业务逻辑实现位于 cryotape/ 包，本文件只是入口转发。
"""
from __future__ import annotations

import sys

from cryotape.cli import main

if __name__ == "__main__":
    sys.exit(main())