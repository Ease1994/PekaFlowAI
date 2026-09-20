#!/usr/bin/env bash
# 编译并打包 Java Agent（Linux / macOS）
cd "$(dirname "$0")"
rm -rf out
mkdir -p out
javac -encoding UTF-8 -d out src/com/pekaflow/agent/*.java
if [ $? -ne 0 ]; then
  echo "编译失败"
  exit 1
fi
jar cfe deploy-agent.jar com.pekaflow.agent.AgentMain -C out . || exit 1
# 记下这份 jar 是哪版源码编译的：jar 是提交进仓库的产物，改了 java 忘了重新编译时，
# 平台会一直分发旧 jar，而现象是「新功能没生效」，极难往编译上想
python3 "$(dirname "$0")/src_fingerprint.py" --write || exit 1
echo "打包完成: deploy-agent.jar"
echo "运行示例:"
echo "  java -jar deploy-agent.jar --server http://localhost:8080 --name my-agent --tags linux,maven --poll 2"
