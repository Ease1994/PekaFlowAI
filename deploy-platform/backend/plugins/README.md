# release 插件开发规范与对接说明

> Agent（`deploy-agent.jar`）**只执行命令**：下载已安装的插件 zip → 注入环境变量 → 跑 `task.json` 里的 `entrypoint` → 采集 stdout/stderr。  
> **业务逻辑全部写在插件包内**，不要改 Agent。

对齐蓝鲸 Atom 模型：
- [发起新插件](https://bk.tencent.com/docs/markdown/ZH/Devops/3.0/UserGuide/Services/Store/start-new-task.md)
- [上传插件](https://bk.tencent.com/docs/markdown/ZH/Devops/3.0/UserGuide/Services/Store/upload-new-task.md)
- SDK 参考：[python](https://github.com/ci-plugins/python-plugin-sdk) / [java](https://github.com/ci-plugins/java-plugin-sdk) / [nodejs](https://github.com/ci-plugins/nodejs-plugin-sdk)

---

## 1. 整体链路

```
开发插件 zip  →  研发商店「上传」  →  「安装」
                                      ↓
流水线步骤 plugin: <name>  →  后端下发 package{entrypoint, download_path}
                                      ↓
Agent 下载 zip 到 ~/.release-agent/plugins/<name>/<version>/
      设置环境变量，在插件目录执行 entrypoint
      回传日志；解析 .release_atom_output.json / ##[set-output]
```

构建机需按语言安装运行时：

| language | 构建机需要 | 典型 entrypoint |
|----------|------------|-----------------|
| python   | Python 3   | `python3 task.py` |
| java     | JDK 8+     | `java -cp . com.example.HelloAtom` |
| nodejs   | Node.js 16+| `node task.js` |

Windows 上 Agent 会把入口里的 `python3` 自动换成 `python`。

---

## 2. 插件包格式（zip）

压缩包**根目录**必须能看到 `task.json`（不要只套一层无意义的空文件夹；若误套一层，平台仍会尝试定位 `task.json`）。

```
my-plugin.zip
├── task.json          # 必填：清单
├── task.py / task.js / *.class  # 入口（与 entrypoint 一致）
├── release_atom_sdk/     # Python：把 SDK 拷进包
├── com/pekaflow/atom/     # Java：把 SDK class/源码打进包
├── release_atom_sdk.js   # Node.js：拷贝 SDK
└── 其它业务文件
```

升级插件时请改 `version`，否则 Agent 会继续用本机缓存 `~/.release-agent/plugins/<name>/<version>/`。

---

## 3. task.json 规范

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 插件标识，流水线 YAML 里 `plugin: name`，全局唯一，建议小写+连字符 |
| `display_name` | 否 | 商店/编排器显示名 |
| `category` | 否 | `source` / `build` / `deploy` / `notify` / `trigger` / `exec` / `artifact` |
| `version` | 否 | 语义化版本，默认 `1.0.0` |
| `language` | 否 | `python` / `java` / `nodejs` |
| `entrypoint` | 是 | **在插件解压目录下**执行的 shell 命令 |
| `description` | 否 | 商店描述 |
| `config_schema` | 否 | 编排器表单 DSL，见下节 |

最小示例：

```json
{
  "name": "hello-python",
  "display_name": "Hello Python",
  "category": "exec",
  "version": "1.0.0",
  "language": "python",
  "entrypoint": "python3 task.py",
  "description": "示例插件",
  "config_schema": {
    "fields": [
      { "key": "message", "label": "消息", "type": "text", "default": "hello" }
    ]
  }
}
```

### config_schema.fields

| 字段 | 说明 |
|------|------|
| `key` | 写入步骤 `with` 的键名，运行时从 `get_input()` 读取 |
| `label` | 表单标签 |
| `type` | `text` / `select` / `radio` / `checkbox` / `code` |
| `required` | 是否必填 |
| `default` | 默认值 |
| `placeholder` | 占位提示 |
| `options` | `select`：`[{"label":"...","value":"..."}]` |

平台会把用户填写的 `with` 整份 JSON 注入环境变量 `RELEASE_ATOM_INPUT_JSON`（**值以字符串为主**）。

---

## 4. Agent 注入的环境变量（对接核心）

无论 Python / Java / Node.js，都读同一套变量：

| 变量 | 含义 |
|------|------|
| `RELEASE_WORKSPACE` | 流水线工作区根目录（其下有 `src/`） |
| `RELEASE_SRC` | 代码目录 `.../src` |
| `RELEASE_ATOM_INPUT_JSON` | 步骤 `with` 的 JSON |
| `RELEASE_PIPELINE_ID` | 流水线数字 ID |
| `RELEASE_BUILD_ID` | 本次任务 ID |
| `RELEASE_JOB_NAME` | Job 名称 |
| `RELEASE_RELEASE_ID` | 本次发布 ID |
| `RELEASE_SERVER_URL` | 平台地址（需要回调平台时用） |
| `RELEASE_TASK_TOKEN` | **本次任务的凭证**，回调平台时放在 `X-Task-Token` 头里 |
| `RELEASE_SENSITIVE_<KEY>` | 插件私有配置（若平台注入） |
| `BK_CI_WORKSPACE` 等 | 与蓝鲸命名兼容的别名 |

工作目录（cwd）= **插件解压目录**，不是 `src/`。需要改仓库时请拼 `RELEASE_SRC` 或 `RELEASE_WORKSPACE/src`。

### 关于 `RELEASE_TASK_TOKEN`

插件拿到的凭证**只代表当前这一个任务**，任务结束即失效，能调的接口只有 `/api/v1/plugin-api/*`
（目前是启动子流水线和查子流水线状态）。构建机自己的长期 token 不再注入插件进程 ——
插件代码来源不完全可控，拿到构建机身份就等于能领走别的任务、伪造任务状态。
老的 `RELEASE_AGENT_TOKEN` 和 `sdk.get_agent_token()` 仅为兼容未升级的 Agent 保留，新插件不要用。

---

## 5. 日志与输出（Agent 如何回收结果）

### 日志

打印到 **stdout/stderr** 即可，执行明细会采集。建议前缀：

```
[INFO]: ...
[WARNING]: ...
[ERROR]: ...
```

### 退出码

- `0`：步骤成功  
- 非 0：步骤失败，Job 终止  

### 结构化输出（推荐）

写文件（工作区或插件 cwd）：

`RELEASE_WORKSPACE/.release_atom_output.json`

```json
{
  "status": "success",
  "message": "ok",
  "type": "default",
  "data": {
    "source_ref": { "type": "string", "value": "abc123def" }
  }
}
```

同时打一行（Agent 也会解析）：

```
##[set-output]source_ref=abc123def
```

`git-checkout` 用 `source_ref` 回写发布记录，供 Rebuild / 代码变更使用。其它字段可自定义，后续步骤暂不自动注入（可后续扩展）。

---

## 6. Python 插件

SDK：`plugins/sdk/python/release_atom_sdk/`  
完整示例：`plugins/git-checkout/`

1. 把 `release_atom_sdk` 目录拷进插件包根目录。  
2. `task.py`：

```python
# -*- coding: utf-8 -*-
import sys
import release_atom_sdk as sdk

def main():
    inp = sdk.get_input()
    ws = sdk.get_workspace()
    sdk.log.info("workspace=" + ws)
    sdk.log.info("message=" + str(inp.get("message", "")))
    sdk.set_output({
        "status": sdk.status.SUCCESS,
        "message": "ok",
        "type": sdk.output_template_type.DEFAULT,
        "data": {
            "echo": {
                "type": sdk.output_field_type.STRING,
                "value": str(inp.get("message", "")),
            }
        },
    })
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

3. `task.json` 的 `entrypoint` 设为 `python3 task.py`。  
4. 打包（在插件目录内）：

```bash
zip -r hello-python-1.0.0.zip task.json task.py release_atom_sdk
```

---

## 7. Java 插件（JDK 8）

SDK：`plugins/sdk/java/src/com/pekaflow/atom/ReleaseAtomSdk.java`（无第三方依赖）  
骨架：`plugins/examples/hello-java/`

1. 将 `com/pekaflow/atom/ReleaseAtomSdk.java` 与业务入口一起编译进包。  
2. 入口类读取 `System.getenv("RELEASE_ATOM_INPUT_JSON")`，或调用 SDK。  
3. 本地编译示例：

```bash
cd plugins/examples/hello-java
javac -encoding UTF-8 com/pekaflow/atom/ReleaseAtomSdk.java com/example/HelloAtom.java
```

4. zip 内需包含 `.class`（Agent 不会帮你 javac）：

```
task.json
com/example/HelloAtom.class
com/pekaflow/atom/ReleaseAtomSdk.class
```

5. `entrypoint` 示例：`java -cp . com.example.HelloAtom`  
   若打成 fat jar：`java -jar plugin.jar`（把 jar 放进 zip 根目录）。

构建机已有运行 Agent 的 JRE/JDK 即可跑 Java 插件。

---

## 8. Node.js 插件

SDK：`plugins/sdk/nodejs/release_atom_sdk.js`  
骨架：`plugins/examples/hello-nodejs/`

1. 把 `release_atom_sdk.js` 拷到插件根目录。  
2. `task.js`：

```javascript
'use strict'
const sdk = require('./release_atom_sdk')
const input = sdk.getInput()
sdk.log.info('message=' + (input.message || ''))
sdk.setOutput({
  status: sdk.status.SUCCESS,
  message: 'ok',
  type: 'default',
  data: {
    echo: { type: 'string', value: String(input.message || '') },
  },
})
```

3. `entrypoint`：`node task.js`  
4. **不要依赖 node_modules**（Agent 不会 npm install）。必要依赖请打包进 zip，或只使用 Node 标准库。

构建机需安装 Node.js，并保证 `node` 在 PATH 中。

---

## 9. 上传、安装、流水线选用

1. 登录平台 → **研发商店** → **上传插件 zip**。  
2. 列表中点 **安装**（未安装不会出现在编排器）。  
3. 流水线步骤：

```yaml
steps:
  - plugin: git-checkout
    with:
      repoName: group/project
      ref: master
```

4. 卸载：商店点卸载后，新执行不再下发该包；已缓存的 Agent 目录可手动删。

HTTP API（需登录 JWT）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/store/plugins/upload` | multipart 字段名 `file` |
| POST | `/api/v1/store/plugins/{id}/install` | 安装 |
| POST | `/api/v1/store/plugins/{id}/uninstall` | 卸载 |
| GET | `/api/v1/store/plugins?installed=true` | 编排器可选列表 |
| GET | `/api/v1/store/plugins/{name}/package` | 下载 zip（Agent 用 `X-Agent-Token`） |

---

## 10. 本地调试（不经过 Agent）

在插件目录模拟 Agent 注入：

```bash
export RELEASE_WORKSPACE=/tmp/ws
export RELEASE_SRC=/tmp/ws/src
mkdir -p "$RELEASE_SRC"
export RELEASE_ATOM_INPUT_JSON='{"message":"hello","ref":"master"}'
export RELEASE_PIPELINE_ID=1
export RELEASE_BUILD_ID=100

# Python
python3 task.py

# Node
node task.js

# Java
java -cp . com.example.HelloAtom
```

看 stdout 与 `.release_atom_output.json`。

---

## 11. 目录说明

```
backend/plugins/
├── README.md                 # 本规范
├── sdk/
│   ├── python/release_atom_sdk/
│   ├── java/src/com/pekaflow/atom/ReleaseAtomSdk.java
│   └── nodejs/release_atom_sdk.js
├── git-checkout/             # 生产示例（Python，平台启动时自动打包安装）
├── run-pipeline/             # 子流水线调用（平台编排执行，启动时自动安装）
└── examples/
    ├── hello-java/
    └── hello-nodejs/
```

`sdk/`、`examples/` **不会**在后端启动时自动安装；`git-checkout/`、`run-pipeline/` 会。

---

## 12. 常见问题

**Q: 改了插件代码流水线还是旧逻辑？**  
升 `version` 后重新上传并安装；或删除构建机 `~/.release-agent/plugins/<name>/`。

**Q: zip 上传失败「缺少 task.json」？**  
保证解压后第一层就能看到 `task.json`。

**Q: Java/Node 步骤直接失败？**  
构建机 PATH 里没有 `java` / `node`，或 entrypoint 与包内文件名不一致。

**Q: 密钥出现在日志里？**  
不要 `print` 完整 token；平台注入的 `repoToken` 等只给命令使用。

**Q: 能否继续用 shell-exec？**  
可以。那是 Agent 内置**命令执行**，不是商店插件包。新业务能力请做成 zip 插件。
