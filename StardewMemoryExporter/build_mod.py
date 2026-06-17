import os
import shutil
import subprocess

# ==================== 🛠️ 路径物理标定 ====================
# 获取当前脚本所在目录（即 C# 工程根目录）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 目标 Mac Steam Mods 路径
TARGET_MODS_DIR = "/Users/evils_you/Library/Application Support/Steam/steamapps/common/Stardew Valley/Contents/MacOS/Mods/StardewMemoryExporter"

# 编译输出路径 (注意：根据你前边报错提示，你的 dotnet 环境使用的是 net10.0，若不符脚本会自动向下兼容 net8.0)
BUILD_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "bin", "Debug", "net10.0")
if not os.path.exists(BUILD_OUTPUT_DIR):
    BUILD_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "bin", "Debug", "net8.0")


# ==================== 🚀 自动化管线核心 ====================
def deploy():
    print("🔄 [1/3] 开始调用 .NET 编译器...")

    # 在工程根目录下执行编译
    result = subprocess.run(["dotnet", "build"], cwd=PROJECT_ROOT, capture_output=True, text=True)

    if result.returncode != 0:
        print("❌ 编译失败！.NET 编译器抛出以下错误：")
        print(result.stderr or result.stdout)
        return
    print("✅ 编译成功！")

    # 2. 清理并准备目标 Mods 目录（保持纯净）
    print("🔄 [2/3] 正在准备 Mac 游戏 Mods 干净的落脚点...")
    if os.path.exists(TARGET_MODS_DIR):
        shutil.rmtree(TARGET_MODS_DIR)  # 物理删除旧版残留
    os.makedirs(TARGET_MODS_DIR, exist_ok=True)

    # 3. 精准同步核心产物 (DLL, PDB, manifest)
    print("🔄 [3/3] 开始搬运核心数据流到游戏内存加载区...")

    # 需要同步的文件清单
    files_to_copy = [
        (os.path.join(BUILD_OUTPUT_DIR, "StardewMemoryExporter.dll"), "StardewMemoryExporter.dll"),
        (os.path.join(BUILD_OUTPUT_DIR, "StardewMemoryExporter.pdb"), "StardewMemoryExporter.pdb"),
        (os.path.join(PROJECT_ROOT, "manifest.json"), "manifest.json"),
    ]

    for src, filename in files_to_copy:
        if os.path.exists(src):
            dest = os.path.join(TARGET_MODS_DIR, filename)
            shutil.copy2(src, dest)
            print(f" 📦 成功搬运 ➔ {filename}")
        else:
            print(f" ⚠️ 警告：未找到源文件 {src}")

    print(f"\n🎉 部署完美完成！Mod 已就绪，目标路径：\n👉 {TARGET_MODS_DIR}\n")


if __name__ == "__main__":
    deploy()
