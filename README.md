# CryoEM 磁带归档工具

实验室内部使用的 CryoEM（冷冻电镜）原始数据 LTO-6 磁带归档工具。

长期稳定、简单可靠、十年后仍然容易维护。

## 三个核心概念

整个系统围绕 **三个清晰分离的层次** 展开：

| 概念 | 含义 | 物理性质 |
|------|------|----------|
| **Dataset** | 用户看到的数据逻辑集合 | 逻辑单位 |
| **Archive**  | 写入磁带的物理单位，一个 `.tar.zst` | 物理单位，**不跨磁带** |
| **Tape**     | LTO-6 磁带（LTFS 挂载，约 2.2 TB 可用） | 物理介质 |

```
Dataset ──> 动态切分的 Archive（50GB 向下取整 + 40GB 安全区间）──> Tape
```

**关键不变量**：

* Archive 大小**动态决定**：按磁带实时剩余空间计算（50 GB 向下取整，
  并保留 40 GB 安全区间），没有固定大小
* **任何 Archive 都不能跨磁带**--剩余空间切不出下一个安全包就换下一盘
* 一个 Dataset 可以跨多个 Archive（如果很大）
* 多个小 Dataset 可以合并到一个 Archive（目录库 `file` 表主键含
  `dataset_id`，不同 Dataset 的同名相对路径不会冲突）

## 动态切分规则

每个 archive 写入前重新读取磁带剩余空间，然后：

1. 剩余空间向下取整到 50 GB 的整数倍
2. 若取整后的「空隙」小于 40 GB 安全区间，再退一档（-50 GB）
3. 剩余空间不足 50 GB 时触发换磁带
4. 若剩余空间能一次放下所有剩余文件（含安全区间），整个项目作为单一
   archive 写入，不切分

## 项目结构

```
main.py        主菜单和固定配置
archive.py     动态装箱、tar.zst 打包、磁带写入、流式恢复/校验
tape.py        LTFS 挂载点访问、磁带提示
catalog.py     SQLite（5 张表）+ 所有 CRUD + 备份/迁移
utils.py       SHA256、format、prompt、log、进度条
tests_smoke.py 冒烟测试（python tests_smoke.py）
```

没有 Repository / DAO / DTO / Service / Controller / Factory / Manager。
没有 config.py / settings.py / yaml / toml / .env。
所有固定参数直接写在 `main.py` 顶部。

## SQLite Schema（5 张表）

```sql
tape            -- 每盘磁带一行（label, capacity, archive_count, status）
dataset         -- 每个 Dataset 一行（name, project, operator, comment）
archive         -- 每个 Archive 一行（name, size, sha256, tape_label, file_number）
dataset_archive -- 关联表：哪些 archive 包含哪些 dataset，以及在 dataset 内的顺序
file            -- 每个文件一行（主键 archive_name + dataset_id + rel_path）
```

外键全部开启 (`PRAGMA foreign_keys = ON`)，所有写入走 `BEGIN IMMEDIATE` 事务。
旧版目录库（`file` 主键缺 `dataset_id`）在打开时自动无损迁移。

## Archive 命名

`YYYYMMDD_NNNN.tar.zst`，例如 `20260728_0001.tar.zst`。

编号按天懒分配（写入前才取名，避免预分配浪费）。

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
  1 创建 Archives（多 Dataset -> 动态切分）
  2 恢复 Dataset（自动按序提取所有 archive）
  3 查询（datasets / archives / 文件）
  4 校验磁带
  5 查看台账
  6 导出台账 (CSV)
  7 重建目录库（从磁带 manifest）
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
| `--ltfs-mount PATH`  | LTFS 挂载点（默认 `/mnt/ltfs`）|
| `--db PATH`          | 目录库 SQLite 文件 |
| `--log PATH`         | 日志文件 |

如果只指定了数据目录而其他参数未给，程序会在运行时逐一询问。
如果只指定 `source` 而不指定 `--dataset`，则用目录名作为默认 Dataset 名称。

## 磁带操作（LTFS）

新磁带首次使用前需手动格式化并挂载；常用命令：

```bash
# 查看 SCSI 设备列表，找到磁带驱动器对应的 sg 设备（如 /dev/sg1）
lsscsi -g

# 格式化磁带并写入卷名（--force 会清空磁带上全部数据，仅在全新/
# 确认可废弃的磁带上使用）
sudo mkltfs -d /dev/sg1 -n EM_data_3 --force

# 挂载磁带
sudo ltfs -o devname=/dev/sg1 /mnt/ltfs

# 卸载磁带（换带前执行）
umount /mnt/ltfs
```

注意：卷名（`-n`）必须与程序中的磁带标签一致（如 `EM_data_3`），
恢复/校验时程序按标签提示插带。

### 1 创建 Archives

1. 依次输入每个 Dataset：数据目录 + 名称 + Project + Operator + Comment
2. 系统扫描所有文件（目录符号链接、无法读取的文件、空目录都会明确提示，
   不再静默跳过）
3. 续传：已在磁带上的文件自动跳过；归档后被修改过的文件（size/mtime 与
   目录库不符）会重新归档
4. 确认后进入动态切分主循环：算目标大小 -> 切一包 -> 写磁带 -> 复测
   剩余空间 -> 重复；空间不足自动提示换带
5. 每个 archive 落盘后同目录写一份 `<name>.manifest.json` sidecar
6. 每成功入库一个 archive，自动备份一次目录库到 `data/backups/`

### 2 恢复 Dataset

1. 输入 Dataset 名称
2. 系统查出该 Dataset 涉及的所有 archive（按顺序）
3. 提示插入第一盘磁带
4. **流式**恢复：边读磁带边算 SHA256 -> `zstd -d` -> `tar -x`，
   不把 archive 拷到本地磁盘（单包可达 ~950 GB）
5. 只解出该 Dataset 自己的文件（同一 archive 里其他 Dataset 的文件不动）
6. SHA256 不符时删除本次解出的内容再报错；需要换带时自动提示

中途 Ctrl+C 安全：catalog 永远不会有半条记录。

### 4 校验磁带

逐个 archive 流式读回并比对 SHA256（不落盘）。从 manifest 重建的
archive 记录没有 SHA256，首次校验通过后自动回填。

### 7 重建目录库

`catalog.sqlite3` 丢失/损坏时的兜底：从当前挂载磁带上的
`*.manifest.json` 重建 tape/dataset/archive/file 记录。幂等，可逐盘磁带
重复执行；重建后跑一次「校验磁带」回填 SHA256。

## 固定配置

所有本地数据（数据库、日志、备份）默认放在程序目录下的 `data/` 子目录中：

```
<程序目录>/
├── data/
│   ├── catalog.sqlite3       # SQLite 目录库
│   ├── cryoem_archive.log    # 运行日志
│   └── backups/              # 目录库自动备份（保留最近 10 份）
├── main.py
├── archive.py
├── catalog.py
├── tape.py
└── utils.py
```

如需更改安装位置，只需移动整个项目目录；无需担心绝对路径散落在系统中。

需要改其他参数时，修改 `main.py` 顶部的常量：

```python
LTFS_MOUNT_DEFAULT = "/mnt/ltfs"    # LTFS 挂载点
LTO6_CAPACITY      = 2_500_000_000_000    # 2.5 TB 裸容量（仅台账记录用）
```

## 依赖

* Python ≥ 3.9
* GNU `tar`
* `zstd`

## 进度条

耗时操作会显示字节级进度条（速度 + ETA）到 stderr：

* 写磁带：`[====>  ] 50.0% 50.00 GB/100.00 GB 160.2 MB/s ETA 5m12s`
* 读磁带 / 校验：同上

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
