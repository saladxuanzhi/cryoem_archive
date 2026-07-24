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
- **磁带卷名（标签纸）**：每条台账记录 `磁带卷名` 字段（标签纸上标记的物理磁带标识符），用于跨会话识别同一盘磁带、便于数据管理；同盘磁带再次插入时自动复用已有卷名。
- **写入进度条**：tar 流式写入期间实时显示进度（百分比、字节数、吞吐、ETA）；非 TTY 场景自动静默。
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
| `--min-tail-gb GB` | `100` | 跨项目共享磁带空间时的最小剩余阈值 |
| `--prefer-project-integrity` | `False` | 项目完整度优先（一个项目独占一盘） |
| `--dry-run` | `False` | 预演模式，仅计算与打印摘要 |
| `--verbose / -v` | `False` | 实时显示 tar 输出（默认仅写到日志） |
| `--non-interactive` | `False` | 跳过所有交互确认（包括参数确认环节） |
| `--yes / -y` | `False` | 所有提示默认确认 |
| `--log-dir PATH` | `./logs` | per-tape tar 日志目录 |
| `--no-review` | `False` | 仅跳过执行前的参数确认环节（保留磁带前确认） |
| `--no-progress` | `False` | 禁用 tar 写入进度条（默认开启；非 TTY 时自动静默） |

## 算法规则（跨项目磁带共享）

`GlobalPacker` 把所有项目的文件按字典序统一处理，按需把多个项目装入同一盘磁带。提供**两种策略**：

### 策略 A：节省空间优先（默认）

`--prefer-project-integrity` **未**指定时的默认行为。

1. **单项目贪心切分**：单个项目 > 单盘容量时，按文件顺序切分为多 Part（`{project}_Part01.tar`、`Part02.tar`...），每个 Part 落到不同磁带。
2. **项目切换决策**：写完一个项目切换到下一个时：
   - 若当前磁带剩余 < `min_tail_gb`（默认 100 GB）→ **开新磁带**
   - 否则 → 继续在同一盘磁带上写下一个项目
3. **项目自动拆分**：下一个项目整体放不下当前磁带剩余空间时，**自动拆分为多 Part 跨磁带**（例：`C_Part01.tar` 在 Tape01 末尾，`C_Part02.tar` 在 Tape02 开头），最大化磁带利用率。

### 策略 B：项目完整度优先

加 `--prefer-project-integrity` 标志。

1. **单项目不跨盘**：一个 project 只能整体放入一盘磁带。
2. **项目切换决策**：
   - 若当前磁带剩余 < `min_tail_gb` → **开新磁带**
   - 若下一个 project 整体放不下当前磁带剩余空间 → **开新磁带**
   - 否则 → 继续在同一盘磁带上写下一个 project
3. **大项目仍可拆**：单 project > 单盘容量时仍会被迫拆分为多 Part（不可避免）。

### 示例对比（4 个项目，cap=3.68 TB，min_tail=100 GB）

```
项目   大小
A   1.60 TB
B   1.36 TB
C   1.65 TB
D   2.13 TB
```

**节省空间优先**（默认）：2 盘

```
Tape01: A_Part01 + B_Part01 + C_Part01 = 3.68 TB（满）
Tape02: C_Part02 + D_Part01           = 3.06 TB
```

**项目完整度优先**（`--prefer-project-integrity`）：3 盘

```
Tape01: A_Part01 + B_Part01            = 2.96 TB（A、B 各占 1 Part）
Tape02: C_Part01                       = 1.65 TB（C 独占 1 盘）
Tape03: D_Part01                       = 2.13 TB（D 独占 1 盘）
```

节省空间优先节省一整盘磁带（≈2.5 TB），但代价是 C 被拆到两盘（解压时需要合并）。
完整度优先每个项目独立完整、易于管理，但可能浪费磁带剩余空间。

## 代码结构

```
CryoTape/
├── main.py                  # 薄壳入口（~30 行）
├── cryotape/                # 业务包
│   ├── __init__.py          # 公开 API
│   ├── __main__.py          # 支持 python -m cryotape
│   ├── constants.py         # 常量、状态枚举、CSV 表头（含磁带卷名字段）
│   ├── exceptions.py        # 异常体系
│   ├── types.py             # dataclass: FileEntry / ProjectInfo / TapePart / CapacityPlan
│   ├── utils.py             # format_size / 交互提示
│   ├── validator.py         # PathValidator
│   ├── detector.py          # ProjectDetector
│   ├── discovery.py         # FileDiscovery
│   ├── capacity.py          # TapeCapacityPlanner
│   ├── packer.py            # TapePacker
│   ├── streamer.py          # TarStreamer (含 Manifest 写入 + 进度条)
│   ├── progress.py          # ProgressBar (纯标准库 ASCII 进度条)
│   ├── catalog.py           # LocalCatalog / TapeCatalogWriter (含磁带卷名)
│   ├── resume.py            # ResumeHandler
│   ├── interactive.py       # InteractiveConfigurator / RuntimeConfig
│   ├── cli.py               # argparse + main()
│   └── workflow.py          # Workflow (主流程 orchestration，含卷名提示)
└── tests/
    ├── conftest.py
    ├── test_capacity.py
    ├── test_catalog.py
    ├── test_detector.py
    ├── test_discovery.py
    ├── test_end_to_end.py
    ├── test_global_packer.py
    ├── test_interactive.py
    ├── test_packer.py
    ├── test_progress.py
    ├── test_streamer.py
    └── test_workflow.py
```

## 工作流

1. **项目判定**：根下直接有 `.eer`/`.mrc`/`.tif` 等图像 → 单项目直接开始；否则列出子目录询问用户。
2. **扫描排序**：递归枚举所有文件，按相对路径字典序排序。
3. **预演摘要**（dry-run 模式）：打印每卷的起止文件与体积，不写磁带。
4. **写盘循环**（真实模式）：每盘依次
   - 打印摘要（文件数、起止、体积）
   - 询问磁带卷名（自动沿用磁带上既有 Catalog_Part*.csv 中的卷名）
   - 用户确认后，写 `Manifest_Part{nn}.txt`
   - 流式 `tar -cf` 写 `.tar`（CPU 不压缩）+ 实时显示进度条
   - 写盘内 `Catalog_Part{nn}.csv`（含磁带卷名字段）
   - 追加本地总 CSV，状态 `done`
   - 提示换带

## 磁带卷名（标签纸标识）

冷冻电镜归档通常会把同一项目跨多盘磁带（部分项目 > 2.25 TB）。为避免「下次插入这盘磁带时不确定它原来是谁」，CryoTape 在本地 CSV 与盘内 CSV 都记录一列 `磁带卷名`：

- **录入时机**：每盘写入前提示输入；若磁带上已有 `Catalog_PartNN.csv`，自动沿用其卷名（同盘磁带复用）。
- **存储位置**：本地总 CSV 与盘内 `Catalog_PartNN.csv` 各保留一份，离线也能识别。
- **断点续写**：把已有磁带插回，工具自动读取盘内 CSV 的卷名并填入新行，无需重复录入。
- **典型命名**：`TAPE-2026-07-24-A`、`LTO6-2026Q3-001` 等贴在磁带外壳 / 标签纸上的标识符。

CSV 表头（节选）：

```
写入日期, 项目名称, 分卷编号, 归档文件名, 文件数量, 总体积,
起始文件路径, 终止文件路径, 磁带挂载点, 状态, 磁带卷名
```

## 写入进度条

`tar` 流式写入期间，stderr 上单行刷新：

```
[Tape01/Project_A_Part01.tar] [████████████░░░░░░░░░░░░░░░░]  42.5%  1.06 GB / 2.50 GB  15.2 MB/s  ETA 1m32s
```

- 进度条按 `.tar` 文件大小与本 part 总体积的比例计算，**与 `tar` 实际产出字节数同步**。
- 非 TTY（管道 / CI / 重定向）时自动禁用，不会污染日志。
- 加 `--no-progress` 强制关闭。
- 进度条渲染在 stderr，stdout 仍可用于其它管道。

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