# CryoTape

将海量冷冻电镜（Cryo-EM）原始数据归档到 LTO-6 LTFS 磁带的 CLI 工具。

## 设计要点

- **零本地中转**：通过 `tar -C <parent_dir> -T <list> -cf <tape_archive>` 直接从原始阵列流式写入磁带挂载点，本地不生成大 tar 包。
- **单盘独立防灾**：严禁跨盘切分（不用 `split` 或 `tar -M`），每盘磁带是独立可解压的 tar 归档。
- **严禁 CPU 压缩**：电镜原始图像已压缩或高熵数据，`tar` 不带 `-z/-j/-J`，跑满 LTO-6 原生 160+ MB/s 速度，避免磁带机频繁启停。
- **动态容量识别**：使用 `shutil.disk_usage()` 自动读取挂载点剩余空间；已有数据的磁带 = 当前剩余 − 安全余量；空磁带 = 2.25 TB − 安全余量。
- **字典序排序**：所有文件按相对路径严格字典序排序后再切分，确保照片拍摄编号连续。
- **双层台账**：本地总 CSV 累计所有项目所有磁带，每盘磁带根目录也保存一份单盘 CSV，无需本地台账即可离线查询。
- **断点续写**：启动时扫描本地 CSV 的 `pending` 行；插入磁带后自动识别是否对应未完成的归档，提供恢复 / 丢弃 / 取消。
- **Manifest 索引**：每盘磁带根目录生成 `Manifest_Part{nn}.txt`，纯文本文件列表。
- **交互式参数确认**：执行前打印所有运行参数，允许用户逐项确认或修改后再执行。

## 安装

仅依赖 Python 3.9+ 标准库。测试需要 `pytest`：

```bash
pip install -r requirements.txt
```

## 使用方法

### 推荐：交互式

```bash
python main.py /data/cryoem/Session_2026Q3
```

执行后会先打印当前所有参数，让用户选择：

- `Y` (默认)：确认配置，开始执行
- `e`：进入修改模式，列出所有可改参数，按编号选择后输入新值
- `n`：取消

### 命令行参数完整指定

```bash
python main.py \
    /data/cryoem/Session_2026Q3 \
    --ltfs-mount /mnt/ltfs \
    --csv-catalog /data/cryoem_catalog/Tape_Archive_Report.csv \
    --safety-margin 15 \
    --new-tape-capacity 2250
```

`--project-dir` 也可以取代位置参数：

```bash
python main.py --project-dir /data/cryoem/Session_2026Q3
```

### 非交互模式（CI / 预演）

```bash
python main.py /data/cryoem/Session_2026Q3 --non-interactive --yes --dry-run
```

### 直接作为模块调用

```bash
python -m cryotape /data/cryoem/Session_2026Q3
```

## 命令行参数

### 必填参数

| 参数 | 说明 |
|---|---|
| `<project_dir>` 或 `--project-dir PATH` | 待归档的项目目录（单项目根或包含多个子项目的父目录）。位置参数与 `--project-dir` 不能同时使用。 |

### 可选参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--ltfs-mount PATH` | `/mnt/ltfs` | LTFS 挂载点 |
| `--csv-catalog PATH` | `~/cryotape_catalog.csv` | 本地总 CSV 台账路径 |
| `--safety-margin GB` | `15` | 磁带安全预留空间 |
| `--new-tape-capacity GB` | `2250` | 标准空磁带可用容量 |
| `--dry-run` | `False` | 预演模式，仅计算与打印摘要 |
| `--verbose / -v` | `False` | 实时显示 tar 输出（默认仅写到日志） |
| `--non-interactive` | `False` | 跳过所有交互确认（包括参数确认环节） |
| `--yes / -y` | `False` | 所有提示默认确认 |
| `--log-dir PATH` | `./logs` | per-tape tar 日志目录 |
| `--no-review` | `False` | 仅跳过执行前的参数确认环节（保留磁带前确认） |

## 代码结构

```
CryoTape/
├── main.py                  # 薄壳入口（~30 行）
├── cryotape/                # 业务包
│   ├── __init__.py          # 公开 API
│   ├── __main__.py          # 支持 python -m cryotape
│   ├── constants.py         # 常量、状态枚举、CSV 表头
│   ├── exceptions.py        # 异常体系
│   ├── types.py             # dataclass: FileEntry / ProjectInfo / TapePart / CapacityPlan
│   ├── utils.py             # format_size / 交互提示
│   ├── validator.py         # PathValidator
│   ├── detector.py          # ProjectDetector
│   ├── discovery.py         # FileDiscovery
│   ├── capacity.py          # TapeCapacityPlanner
│   ├── packer.py            # TapePacker
│   ├── streamer.py          # TarStreamer (含 Manifest 写入)
│   ├── catalog.py           # LocalCatalog / TapeCatalogWriter
│   ├── resume.py            # ResumeHandler
│   ├── interactive.py       # InteractiveConfigurator / RuntimeConfig
│   ├── cli.py               # argparse + main()
│   └── workflow.py          # Workflow (主流程 orchestration)
└── tests/
    ├── conftest.py
    ├── test_capacity.py
    ├── test_catalog.py
    ├── test_detector.py
    ├── test_discovery.py
    ├── test_end_to_end.py
    ├── test_interactive.py
    ├── test_packer.py
    └── test_streamer.py
```

## 工作流

1. **项目判定**：根下直接有 `.eer`/`.mrc`/`.tif` 等图像 → 单项目直接开始；否则列出子目录询问用户。
2. **扫描排序**：递归枚举所有文件，按相对路径字典序排序。
3. **预演摘要**（dry-run 模式）：打印每卷的起止文件与体积，不写磁带。
4. **写盘循环**（真实模式）：每盘依次
   - 打印摘要（文件数、起止、体积）
   - 用户确认后，写 `Manifest_Part{nn}.txt`
   - 流式 `tar -cf` 写 `.tar`（CPU 不压缩）
   - 写盘内 `Catalog_Part{nn}.csv`
   - 追加本地总 CSV，状态 `done`
   - 提示换带

## 中断恢复

任何时候 `Ctrl-C` 中断，下次运行：

1. 启动扫描本地 CSV，找到 `pending` 行
2. 插入对应磁带，工具自动识别盘内 `Catalog_Part{nn}.csv`
3. 提示选择：继续 (c) / 丢弃从头开始 (r) / 取消 (n)

## 测试

```bash
pytest tests/ -v
```

包括：
- 单元测试：discovery / packer / capacity / detector / streamer / catalog
- 集成测试：用临时目录模拟磁带，跑一次完整归档并用 `tar -tf` 反查

## 异常处理

所有自定义异常继承自 `CryoTapeError`：

- `InvalidPathError` / `TarNotFoundError` / `TapeNotMountedError`：启动期预检
- `InsufficientSpaceError`：磁带剩余空间不足
- `OversizedFileError`：单文件超过容量上限（违反不切分约束）
- `ProjectDetectionError`：无法判定单/多项目
- `TapeWriteError`：`tar` 子进程非 0 退出
- `UserAbortedError`：用户主动中止

`/tmp` 下的临时文件通过 `tempfile` + `try/finally` + `atexit` 三重清理保证不残留。