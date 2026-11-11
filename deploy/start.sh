#!/bin/bash
# ============================================
# DBCheck 启动脚本 (含 RBAC 用户管理模块)
# 说明：本脚本位于 deploy/，自动切换到仓库根目录执行，
#       以便 web_ui.py / user_management / data/ 等相对路径解析正确。
# ============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

echo "=========================================="
echo "  DBCheck - 数据库健康巡检平台"
echo "  (RBAC 用户管理模块已集成)"
echo "=========================================="

# 检查 Python 环境
PYTHON="python3"
if ! command -v $PYTHON &> /dev/null; then
    echo "❌ 未找到 Python 3，请先安装 Python 3.9+"
    exit 1
fi

echo "✅ Python: $($PYTHON --version)"

# 安装依赖（如需要）
if [ ! -f ".deps_installed" ]; then
    echo ""
    echo "📦 安装项目依赖..."
    pip3 install -r deploy/requirements.txt --break-system-packages 2>/dev/null || \
    pip3 install -r deploy/requirements.txt
    touch .deps_installed
    echo "✅ 依赖安装完成"
fi

# 初始化 RBAC 种子数据（如需要）
if [ ! -f "data/pro_data/.rbac_seeded" ]; then
    echo ""
    echo "🔐 初始化 RBAC 用户管理系统..."
    $PYTHON -m user_management.seed
fi

# 启动应用
echo ""
echo "🚀 启动 DBCheck Web 服务..."
echo "   访问地址: http://localhost:5003"
echo "   管理后台: http://localhost:5003/um/admin"
echo "   登录页面: http://localhost:5003/um/login"
echo ""
echo "   默认管理员: admin / admin123"
echo ""

# 控制台日志由 shell 重定向持有 fd，运行中无法安全轮转；
# 每次启动前先把超限的旧文件移入归档目录（data/logs/_archive/console），
# 长期运行请使用 deploy/service/ 下的服务方式（systemd/journald 自带容量上限）。
CONSOLE_LOG="data/logs/web_service_restart.log"
if [ -f "$CONSOLE_LOG" ] && [ "$(wc -c < "$CONSOLE_LOG" 2>/dev/null || echo 0)" -gt 20971520 ]; then
    mkdir -p data/logs/_archive/console
    mv "$CONSOLE_LOG" "data/logs/_archive/console/web_service_restart.$(date +%Y%m%d-%H%M%S).log"
fi

$PYTHON web_ui.py >> "$CONSOLE_LOG" 2>&1
