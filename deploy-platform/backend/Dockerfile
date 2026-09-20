# 发布部署平台 · 后端 Dockerfile
# 构建：docker build -t deploy-platform-backend -f Dockerfile .
# 运行：docker run -d -p 8080:8080 -e DATABASE_URL=mysql+pymysql://root:root123@host:3306/deploy_platform?charset=utf8mb4 deploy-platform-backend

FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/

# 拷贝源码 + Agent 下发包（/agents/download 依赖此文件）
# scripts 是一键安装脚本，目标机器直接从平台下载后执行
# jar 是提交进仓库的编译产物，连 java 源码和指纹一起带上：启动时比对，
# 有人改了 java 却忘了重新 build 时能当场发现，而不是等到功能「不生效」才排查
COPY app ./app
COPY agent-java/deploy-agent.jar ./agent-java/deploy-agent.jar
COPY agent-java/deploy-agent.jar.srcsha ./agent-java/deploy-agent.jar.srcsha
COPY agent-java/src ./agent-java/src
COPY agent-java/src_fingerprint.py ./agent-java/src_fingerprint.py
COPY agent-java/scripts ./agent-java/scripts
# 节点没 JDK 时由安装脚本拉取。包应放进数据卷 /app/data/shared-packs/jdk-linux（页面上传一次即可）。
# 这里仍 COPY 目录：首次启动会把镜像里的包拷进数据卷；目录始终存在，没有包也能构建。
COPY agent-java/jdk-linux ./agent-java/jdk-linux
COPY plugins ./plugins

EXPOSE 8080

# 生产启动：单 worker（定时触发调度线程为进程内线程，多 worker 会重复触发）
# limit-concurrency 抬高以容纳百并发心跳/领取 + SSE
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--limit-concurrency", "500"]
