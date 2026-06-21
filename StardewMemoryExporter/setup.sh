#!/bin/bash

# --- 配置区域 ---
# 项目子目录名称
PROJECT_DIR="StardewMemoryExporter"
# 星露谷游戏的 Mods 目标部署路径（根据你的 VS Code tasks.json 自动匹配）
DEPLOY_PATH="/Users/evils_you/Library/Application Support/Steam/steamapps/common/Stardew Valley/Contents/MacOS/Mods/StardewMemoryExporter"

# 获取当前脚本所在目录的绝对路径
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TARGET_CWD="$SCRIPT_DIR"

echo "========================================="
echo "🚀 开始自动化编译与部署星露谷 1.6 内存插件..."
echo "========================================="

# 1. 检查子目录是否存在
if [ ! -d "$TARGET_CWD" ]; then
    echo "❌ 错误: 找不到项目目录: $TARGET_CWD"
    exit 1
fi

# 2. 进入 C# 项目目录
cd "$TARGET_CWD" || exit 1

# 3. 执行 dotnet publish 编译并发布
echo "🛠️ 正在编译项目并生成依赖项..."
# 使用双引号包裹带有空格的路径 $DEPLOY_PATH
dotnet publish -c Debug -o "$DEPLOY_PATH"

# 4. 检查上一步 dotnet 命令的退出状态码
if [ $? -eq 0 ]; then
    echo "========================================="
    echo "🎉 部署成功！"
    echo "📦 已空投至: $DEPLOY_PATH"
    echo "========================================="
else
    echo "❌ 部署失败: dotnet 编译过程中遇到错误，请检查 C# 代码。"
    exit 1
fi

/tmp/open-smapi-terminal.command ; exit;