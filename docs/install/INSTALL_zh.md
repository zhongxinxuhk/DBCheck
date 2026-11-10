## 一、Docker 快速上手（推荐）

Docker 可以使用以下两种方式：

### 1、docker images
一条命令启动，无需安装任何依赖：

```bash
# 两个 Docker 镜像源，选择其一
# 1、Docker Hub
docker pull jackge12345/dbcheck:latest
docker run -d -p 5003:5003 \
  -v dbcheck_data:/app/data \
  -v dbcheck_reports:/app/data/reports \
  --name dbcheck \
  jackge12345/dbcheck:latest

# 2、GitHub Container Registry
docker pull ghcr.io/fiyo/dbcheck:latest
docker run -d -p 5003:5003 \
  -v dbcheck_data:/app/data \
  -v dbcheck_reports:/app/data/reports \
  --name dbcheck \
  ghcr.io/fiyo/dbcheck:latest
```

### 2、docker-compose

```bash
curl -o deploy/docker-compose.yml https://raw.githubusercontent.com/fiyo/DBCheck/main/deploy/docker-compose.yml
docker compose -f deploy/docker-compose.yml up -d
```

---

## 二、源码安装快速上手

### 1、环境要求

- Python 3.10+
- 各数据库对应的 Python 驱动

### 2、拉取本地模型

本地安装 Ollama，并使用以下命令拉取模型：

```bash
ollama pull qwen3:30b          # 拉取诊断模型（此处以qwen3:30b为例）
ollama pull nomic-embed-text    # 拉取 RAG 嵌入模型（知识库功能需要）
```
### 3、拉取源码并安装依赖
```bash
# 克隆项目
git clone https://github.com/fiyo/DBCheck.git
cd DBCheck

# 安装依赖
pip install -r deploy/requirements.txt

```
### 4、启动 Web UI
```bash
python web_ui.py
```

## 三、打包分发

使用根据不同平台分别以下命令打包为单个可执行文件：

```bash
# 1、Windows
build/build_windows.bat

cd dist
dbcheck.exe

# 2、Linux
build/build_linux.sh

cd dist
./dbcheck

# 3、MacOS
build/build_macos.sh

cd dist
./dbcheck
```
---
## 四、访问界面

访问 **http://localhost:5003**，首次访问会跳转到登录页。

初始账号与角色（管理员 `admin` / 只读 `viewer` / 运维 `operator`）的完整说明、各平台启动步骤
以及管理员安装期口令的获取方式，见 **[初始化文档（INITIALIZATION_zh.md）](./INITIALIZATION_zh.md)**。

> 首次登录后请立即在账户中心修改密码。