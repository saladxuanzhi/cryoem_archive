# CryoEM 磁带归档工具

实验室内部使用的 CryoEM（冷冻电镜）原始数据 LTO-6 磁带归档工具。

长期稳定、简单可靠、十年后仍然容易维护。

## 三个核心概念

整个系统围绕 **三个清晰分离的层次** 展开：

| 概念 | 含义 | 物理性质 |
|------|------|----------|
| **Dataset** | 用户看到的数据逻辑集合 | 逻辑单位 |
| **Archive**  | 写入磁带的物理单位，约 100 GB | 物理单位，**不跨磁带** |
| **Tape**     | LTO-6 磁带，约 2400 GB 可用 | 物理介质 |

```
Dataset ──→ 100 GB Archive 块 ──→ Tape（24 个 Archive 满盘）
```

**关键不变量**：

* 每个 Archive 严格控制在 **100 GB** 左右（实际略小，接近压缩后大小）
* 每盘磁带写入 **24 个 Archive**（100 × 24 = 2400 GB）
* **任何 Archive 都不能跨磁带**——一盘满就换下一盘
* 一个 Dataset 可以跨多个 Archive（如果 > 100 GB）
* 多个小 Dataset 可以合并到一个 Archive

## 为什么是 100 GB / 24 个

* LTO-6 原始容量 2.5 TB，预留 100 GB 安全余量（磁带物理损耗 + FileMark）
* 目标可用容量 2400 GB
* 2400 ÷ 100 = 24，整除，完美填满
* 这也是「**唯一允许且最完美**」的数据拆分方式

## 项目结构

整个项目只有 **5 个 Python 文件**：

```
main.py        主菜单和固定配置
archive.py     100GB 装箱、tar.zst 打包、磁带写入、恢复
tape.py        mt 操作、磁带提示
catalog.py     SQLite（5 张表）+ 所有 CRUD
utils.py       SHA256、format、prompt、log、进度条
```

没有 Repository / DAO / DTO / Service / Controller / Factory / Manager。
没有 config.py / settings.py / yaml / toml / .env。
所有固定参数直接写在 `main.py` 顶部。

## SQLite Schema（5 张表）

```sql
tape            -- 每盘磁带一行（label, capacity, archive_count, status）
dataset         -- 每个 Dataset 一行（name, project, operator, comment）
archive         -- 每个 100GB Archive 一行（name, size, sha256, tape_label, file_number）
dataset_archive -- 关联表：哪些 archive 包含哪些 dataset，以及在 dataset 内的顺序
file            -- 每个文件一行（archive_name, dataset_id, rel_path, size, mtime）
```

外键全部开启 (`PRAGMA foreign_keys = ON`)，所有写入走 `BEGIN IMMEDIATE` 事务。

## Archive 命名

`YYYYMMDD_NNNN.tar.zst`，例如 `20260728_0001.tar.zst`。

编号全局递增（不是每盘重置），并发安全（`IMMEDIATE` 事务内分配）。

## 运行

### 方式一：交互式菜单

```bash
python main.py
```

进入主菜单：

```
========================================
  CryoEM 磁带归档工具
========================================
  1 创建 Archives（多 Dataset → 100GB 分块）
  2 恢复 Dataset（自动按序提取所有 archive）
  3 查询（datasets / archives / 文件）
  4 校验磁带
  5 查看台账
  0 退出
========================================
```

### 方式二：命令行参数 + 交互补齐

可以预填一些参数直接进入「创建 Archives」，其余仍会交互询问：

```bash
# 最小调用：只指定数据目录
python main.py /mnt/tmp/

# 指定 LTFS 挂载点（磁带以文件系统形式挂载时使用）
python main.py /mnt/tmp/ --ltfs-mount /mnt/ltfs

# 预填所有非关键字段
python main.py /mnt/tmp/20260515_wl \
    --ltfs-mount /mnt/ltfs \
    --dataset 20260515_CL5+CL6 \
    --project "CL" \
    --operator xuanzhi \
    --comment "" \
    --tape-label EM_data_1
```

参数说明：

| 参数 | 说明 |
|------|------|
| `source`（位置参数）| 第一个 Dataset 的数据目录 |
| `--dataset NAME`     | Dataset 名称（不指定则交互询问）|
| `--project P`        | Project 名称 |
| `--operator O`       | 操作员 |
| `--comment C`        | 备注 |
| `--tape-label L`     | 目标磁带标签 |
| `--tape-device D`    | 原始磁带设备（默认 `/dev/nst0`）|
| `--ltfs-mount PATH`  | LTFS 挂载点（与 `--tape-device` 互斥）|
| `--db PATH`          | 目录库 SQLite 文件 |
| `--log PATH`         | 日志文件 |

如果只指定了数据目录而其他参数未给，程序会在运行时逐一询问。
如果只指定 `source` 而不指定 `--dataset`，则用目录名作为默认 Dataset 名称。

### 原始磁带 vs LTFS

* **`/dev/nst0`（默认）**：使用 `mt` + `dd` 直接控制 LTO 驱动器，
  通过 `mt weof` 写 FileMark。需要在终端前手动换带。
* **`--ltfs-mount PATH`**：磁带以 LTFS 文件系统形式挂载，每个 archive
  就是一个普通文件（`PATH/<tape_label>/<archive_name>.tar.zst`）。
  无需 `mt`，无需 FileMark；适合在 GUI 环境或远程服务器上操作。

### 1 创建 Archives

1. 依次输入每个 Dataset：数据目录 + 名称 + Project + Operator + Comment
2. 系统扫描所有文件
3. 顺序装箱：不断读取下一个文件；当前 Archive 满 100 GB 就关闭
4. 预分配 archive 名字（YYYYMMDD_NNNN）
5. 显示计划：需要几个 archive、几盘磁带
6. 确认后逐个构建 + 写磁带
7. 一盘满 24 个 archive 时自动提示换带

### 2 恢复 Dataset

1. 输入 Dataset 名称
2. 系统查出该 Dataset 涉及的所有 archive（按顺序）
3. 提示插入第一盘磁带
4. 逐个读取、校验 SHA256、解压到目标目录
5. 需要换带时自动提示

中途 Ctrl+C 安全：catalog 永远不会有半条记录。

## 固定配置

所有本地数据（数据库、日志、暂存目录）默认放在程序目录下的 `data/` 子目录中：

```
<程序目录>/
├── data/
│   ├── catalog.sqlite3       # SQLite 目录库
│   ├── cryoem_archive.log    # 运行日志
│   └── staging/              # 打包过程中的 .tar.zst 暂存
├── main.py
├── archive.py
├── catalog.py
├── tape.py
└── utils.py
```

如需更改安装位置，只需移动整个项目目录；无需担心绝对路径散落在系统中。

需要改其他参数时，修改 `main.py` 顶部的常量：

```python
TAPE_DEVICE   = "/dev/nst0"          # 磁带设备
LTO6_CAPACITY = 2_500_000_000_000    # 2.5 TB（来自 archive.LTO6_RAW_BYTES）
```

## 依赖

* Python ≥ 3.9
* GNU `tar`
* `zstd`
* `mt`
* `dd`

## 进度条

耗时操作会显示进度条到 stderr：

* 文件哈希：`[=====>     ] 50.0% (1234/2468)`
* tar+zstd 压缩：`[=====>     ] 50.0% (93.1 GiB/100 GiB)`
* 写磁带：显示 `archive 5/24` 这种行号进度

## 安装（可选）

```bash
# 直接运行
python main.py

# 或安装为命令
pip install -e .
cryoem-archive
```

## 设计原则

* KISS / YAGNI
* 函数优先，class 只在需要状态时用
* 不分层，分层只为了清晰而不为了"专业"
* 不为未来可能的需求增加抽象
* 像 `git`、`mt`、`fdisk` 那样写
