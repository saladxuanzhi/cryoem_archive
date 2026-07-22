import os
import csv
import math
import subprocess

# ================= 配置区 =================
PROJECT_DIR = "/data/cryoem/Project_A"  # 你的冷冻电镜项目所在文件夹绝对路径
LTFS_MOUNT = "/mnt/ltfs"                # 你的 LTFS 磁带挂载点
LOCAL_CSV_REPORT = "/data/cryoem_catalog/Tape_Archive_Report.csv" # 本地台账保存位置

# LTO-6 容量配置 (标称2.5TB，为留足索引与安全空间，设定每个大包最大为 2.25 TB)
# 1 TB = 1000^4 Bytes (存储厂商标准)
MAX_TAPE_SIZE_BYTES = 2.25 * (1000 ** 4) 
# ==========================================

def get_human_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def main():
    print(f"[*] 开始扫描项目文件夹: {PROJECT_DIR}")
    
    # 获取父目录和项目名，方便 tar 截取相对路径 (防止解压后出现长串绝对路径)
    parent_dir = os.path.dirname(PROJECT_DIR.rstrip('/'))
    project_name = os.path.basename(PROJECT_DIR.rstrip('/'))
    
    all_files = []
    
    # 1. 扫描并收集所有文件及其大小
    for root, dirs, files in os.walk(PROJECT_DIR):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, parent_dir) # 相对路径，如 Project_A/Micrographs/001.eer
            size = os.path.getsize(full_path)
            all_files.append((rel_path, size, full_path, f))
            
    # 按相对路径(文件名)进行严格排序，确保照片编号是连续的
    all_files.sort(key=lambda x: x[0])
    
    print(f"[*] 扫描完毕，共发现 {len(all_files)} 个文件。")

    # 2. 根据容量划分子任务 (虚拟装箱)
    tapes_data = []
    current_tape_files = []
    current_tape_size = 0
    
    for file_info in all_files:
        rel_path, size, full_path, filename = file_info
        if current_tape_size + size > MAX_TAPE_SIZE_BYTES:
            tapes_data.append(current_tape_files)
            current_tape_files = []
            current_tape_size = 0
            
        current_tape_files.append(file_info)
        current_tape_size += size
        
    if current_tape_files:
        tapes_data.append(current_tape_files)
        
    total_tapes = len(tapes_data)
    print(f"[*] 根据 2.25 TB/盘 的限制，该项目将被划分为 {total_tapes} 盘磁带写入。")

    # 准备写入 CSV 台账
    csv_exists = os.path.isfile(LOCAL_CSV_REPORT)
    csv_file = open(LOCAL_CSV_REPORT, 'a', newline='', encoding='utf-8')
    csv_writer = csv.writer(csv_file)
    if not csv_exists:
        # 写入表头
        csv_writer.writerow(["写入日期", "项目名称", "分卷编号", "存档文件名", "包含文件数", "总体积", "首个文件 (Start)", "末个文件 (End)"])

    # 3. 循环提示并流式写入每一盘磁带
    import datetime
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    
    for idx, tape_files in enumerate(tapes_data):
        part_num = idx + 1
        archive_name = f"{project_name}_Part{part_num:02d}.tar"
        total_size = sum([f[1] for f in tape_files])
        first_file = tape_files[0][0]
        last_file = tape_files[-1][0]
        file_count = len(tape_files)
        
        print("\n" + "="*50)
        print(f"准备写入第 {part_num} 部分 (共 {total_tapes} 部分)")
        print(f"  - 目标文件: {archive_name}")
        print(f"  - 包含文件数: {file_count}")
        print(f"  - 数据体积: {get_human_size(total_size)}")
        print(f"  - 照片范围: {first_file}  ===>  {last_file}")
        print("="*50)
        
        # 交互等待确认
        input(f"\n>>> 请确保已将正确的磁带插入机器并成功挂载至 {LTFS_MOUNT}！\n>>> 准备好后，按 [Enter] 键开始写入...")
        
        # 将当前磁带需要打包的文件列表写入一个临时文件 (供 tar -T 读取)
        list_file_path = f"/tmp/{project_name}_part{part_num}_list.txt"
        with open(list_file_path, 'w', encoding='utf-8') as f_list:
            for f in tape_files:
                f_list.write(f"{f[0]}\n")  # 写入相对路径
                
        # 构造 tar 命令
        # -c 创建, -v 显示过程, -f 指定目标位置(磁带), -C 切换到父目录, -T 读取文件列表
        tar_target = os.path.join(LTFS_MOUNT, archive_name)
        tar_cmd = ["tar", "-cvf", tar_target, "-C", parent_dir, "-T", list_file_path]
        
        print(f"[*] 正在流式写入磁带，请耐心等待 (磁带机切勿断电)...")
        try:
            # 运行 tar 命令
            subprocess.run(tar_cmd, check=True)
            print(f"[√] 第 {part_num} 部分写入成功！")
            
            # 记录到 CSV
            csv_writer.writerow([
                today_str, project_name, f"Part{part_num}", archive_name, 
                file_count, get_human_size(total_size), first_file, last_file
            ])
            csv_file.flush() # 实时保存台账
            
        except subprocess.CalledProcessError as e:
            print(f"[X] 写入失败，Tar 命令报错: {e}")
            print(f"[!] 退出程序，请检查磁带状态。")
            break
            
        # 清理临时列表文件
        if os.path.exists(list_file_path):
            os.remove(list_file_path)
            
        if part_num < total_tapes:
            print(f"\n[*] 请弹出当前磁带：执行 `umount {LTFS_MOUNT}`")
            print("[*] 换上新的空磁带，并重新挂载 LTFS")
            
    csv_file.close()
    print("\n[*] 全部任务执行完毕！台账已更新至: " + LOCAL_CSV_REPORT)

if __name__ == "__main__":
    main()