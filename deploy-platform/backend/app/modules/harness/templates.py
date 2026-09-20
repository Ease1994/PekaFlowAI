"""技能包 / 第三方工具包的开发模板。

模板必须细到：一个只会按文件填空的模型，改名字和正文后就能打出可上传的 zip。
字段约束与解析器保持一致，上传时会被同一套校验卡住。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi.responses import Response

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SDK = _BACKEND_ROOT / "plugins" / "sdk" / "python" / "release_atom_sdk"

SKILL_README = """# RELEASE Agent 技能包开发说明书（给开发者和 AI）

先读完本文件再改其它文件。技能包**不含可执行代码**，不能改平台数据。
它只是一份给模型看的说明书：什么时候用、怎么问、禁止做什么。
会改流水线 / 往节点传文件的能力是「内置工具」或「第三方工具包」，不要写进技能包。
技能包是 Markdown 说明，**不强制签名**。技能库上传会共享给全部人员，立刻可用；删除只有发布者或管理员。只给自己用的技能走助手确认卡。

## 1. 你要交付什么

一个 zip，**根目录**必须同时有这两个文件（不要外套一层文件夹）：

```
your-skill.zip
├── README.md          # 本说明，上传时会被忽略，可留着给人看
├── manifest.yaml      # 必填：包身份
├── SKILL.md           # 必填：给模型看的正文
└── examples.md        # 可选：对话样例
```

上传入口：技能库 → Agent 技能 → 技能包 → 上传技能包。管理员可传。上传后自动启用。
模板下载：同一位置点「下载技能包模板」。

## 2. 和另外两种包的区别（不要搞混）

| 包类型 | 必有文件 | 会不会执行代码 | 上传后出现在 |
|--------|----------|----------------|--------------|
| 技能包（本模板） | manifest.yaml + SKILL.md | 否 | Agent 技能 |
| 流水线插件包 | task.json + 入口脚本 | 是，在构建机/节点上跑 | 流水线插件 |
| 第三方 Agent 工具包 | manifest.yaml + manifest.sig + 入口脚本 | 是，在隔离容器里跑 | Agent 工具 |

如果用户要的是「流水线里多一个步骤」，去下载**插件包模板**，不要用本模板。
如果用户要的是「助手能调用一个新 API」，去下载**工具包模板**并做签名。

## 3. manifest.yaml 每个字段必须怎么填

```yaml
schema_version: "1"          # 必须是字符串 1，不能写 1（数字）
kind: agent-skill            # 必须原样，写错会当工具包或拒绝
name: release-checklist      # 全局唯一。只允许小写字母、数字、点、下划线、连字符
                             # 正则：^[a-z0-9]([a-z0-9._-]{0,126}[a-z0-9])?$
                             # 不能有空格、不能有中文、不能大写
version: 1.0.0               # 语义化版本 major.minor.patch
display_name: 发布前检查清单  # 页面上给人看的名字，可中文
description: 发布生产流水线前核对环境、权限和确认卡。在要发生产、上线到 prod 时使用。不是发起发布，改用 propose_release。
entrypoint: ""               # 技能包必须留空，不要填 python
capabilities: []             # 技能包不声明能力；有能力需求请做工具包
dependencies: []             # 一般空。若依赖其它组件，key 形如 pipeline-plugin:shell-exec
metadata:
  invocation_policy: model   # 只能是 model / user / always 三者之一
                             # model  = 出现在目录里，模型用 skill 工具按 name 加载 SKILL.md
                             # user   = 用户在会话里点选后才进入目录并内联正文
                             # always = 每一轮都把正文内联进系统提示，慎用，会占上下文
```

改 name 时必须同时改 zip 文件名习惯：`{name}-{version}.zip`。
升级时必须升高 version，否则会被当成同一版本。

## 4. SKILL.md 怎么写（这是技能的全部能力）

SKILL.md 不会整份塞进系统提示。系统提示只放 name + description；
模型用 `skill` 工具按精确 name 加载正文。写不好模型就会乱调工具或编造 ID。

SKILL.md 约定结构：

```markdown
---
name: query-pipeline-status
description: >-
  查询流水线最近一次发布状态。在问发得怎样、正在发布有哪些、失败了没时使用。
  不是发起发布。
---

# 查询流水线发布状态

## Workflow
1. 调用一次 get_release_status。点名流水线用 keyword。
2. 按当句传 status；没说筛哪种、也没说要历史时不传，只查最近一次。

## Do not use
- 要去发布 → propose_release
- 只要流水线目录 → list_pipelines

## Examples
**查一下 test-C 的状态** → get_release_status（keyword=test-C）
```

`description` 必须同时写**做什么**、**何时用**和**不是什么**，第三人称，带检索关键词。
正文写流程，不要把某一种问法写成技能的全部范围。
Examples 是检索用的同义样本，不是口吻白名单。

SKILL.md 不能为空，不能超过 20000 字。用 UTF-8。不要发明工具名。
技能**不能**自己发布流水线，只能教模型调用已经存在的内置工具。

## 5. 打包命令（必须在包含 manifest.yaml 的那一层执行）

Linux / macOS：

```bash
zip -r release-checklist-1.0.0.zip manifest.yaml SKILL.md examples.md README.md CHECKLIST.md
```

Windows PowerShell：

```powershell
Compress-Archive -Path manifest.yaml,SKILL.md,examples.md,README.md,CHECKLIST.md `
  -DestinationPath release-checklist-1.0.0.zip -Force
```

打完后用解压工具打开，**第一层**就要看到 `manifest.yaml` 和 `SKILL.md`。
如果看到的是 `release-checklist/manifest.yaml`，上传会失败。

## 6. 上传失败对照表

| 平台报错 | 你改哪里 |
|----------|----------|
| 技能包需要 manifest.yaml 和 SKILL.md | zip 根目录缺文件，或外套了一层目录 |
| name 只能包含小写字母… | 把 name 改成 hello-skill 这种 |
| 不是合法语义版本 | version 写成 1.0.0 |
| kind 必须是 agent-skill | 不要写成 skill / agent_skill |
| invocation_policy 只能是 … | metadata.invocation_policy 只能 model/user/always |
| SKILL.md 不能为空 | 正文至少写一段完整说明 |
| 不是合法的 zip 包 | 不要用 rar/7z 改后缀 |

## 7. 开发完成后

1. 按 CHECKLIST.md 逐项打勾。
2. 管理员在技能库上传。
3. 到 AI Agent 新开一轮对话，用 examples.md 里的原话试。
4. 若模型不读这份技能：把 invocation_policy 改成 always 再升一个版本上传。
"""

SKILL_CHECKLIST = """# 技能包提交检查清单

- [ ] zip 根目录有 manifest.yaml 和 SKILL.md
- [ ] schema_version 是字符串 "1"
- [ ] kind 是 agent-skill
- [ ] name 全小写、无空格、无中文
- [ ] version 是 1.0.0 这种三段数字
- [ ] invocation_policy 是 model 或 user 或 always
- [ ] SKILL.md 有 YAML description（做什么 + 何时用 + 不是什么）和 Workflow / Do not use / Examples
- [ ] SKILL.md 没有要求模型执行不存在的工具名
- [ ] SKILL.md 少于 20000 字且不是空文件
- [ ] 没有把 task.py、密钥、数据库地址放进包里
"""

SKILL_MANIFEST = """schema_version: "1"
kind: agent-skill
name: release-checklist
version: 1.0.0
display_name: 发布前检查清单
description: 发布生产流水线前核对环境、权限和确认卡。在要发生产、上线到 prod 时使用。不是发起发布，改用 propose_release。
entrypoint: ""
capabilities: []
dependencies: []
metadata:
  invocation_policy: model
"""

SKILL_BODY = """---
name: release-checklist
description: >-
  发布生产流水线前核对环境、权限和确认卡。
  在要发生产、上线到 prod 分组时使用。不是发起发布，改用 propose_release。
---

# 发布前检查清单

生产 / prod 分组的流水线发布前，先核环境、权限和确认卡。本技能只教模型先核对，真正发起发布改用 `propose_release`。

## Workflow

1. 能唯一对应到一条流水线：立刻 `propose_release`。确认卡就是确认，不要再问「是否现在发布」。
2. 没有数字 ID、也无法从名字唯一对应时，才 `list_pipelines`（带 keyword），列出候选项再选。
3. 禁止编造 `pipeline_id`。选定后再 `propose_release`。在此之前不要调用 `create_release`。
4. 没有执行权限时把错误原文转达，并引导 `apply_pipeline_execute`。
5. 生产分组需要审批。确认卡上会写「进入待审批」，不要承诺立刻上线。

## Do not use

- 只要流水线目录 → `list_pipelines`，不要发布
- 往服务器拷文件 → `list_push_nodes` + `propose_node_push`
- 申请执行权 → `apply_pipeline_execute`
- 停掉正在跑的发布 → `propose_cancel`；作废权限申请 → `cancel_access_application`

## Examples

**帮我把 order-service 发到生产** → `propose_release`（能唯一对应就直接出卡）

**把这些文件传到 192.0.2.10** → 本技能不适用，走节点下发
"""

SKILL_EXAMPLES = """# 对话样例

用户：帮我把 order-service 发到生产。
助手：先 list_pipelines（keyword=order-service），找到生产分组那条，复述给用户，再 propose_release。

用户：把这些文件传到 192.0.2.10 的 D:\\\\wwwroot\\\\site。
助手：这不是发布流水线。用 list_push_nodes（keyword=192.0.2.10）+ propose_node_push。本技能不适用。

用户：我没有权限发 pay-service。
助手：apply_pipeline_execute，不要强行 propose_release。
"""

TOOL_README = """# RELEASE 第三方 Agent 工具包开发说明书（给开发者和 AI）

本模板是**会执行代码**的助手工具。代码不在 API 进程里跑，只在 harness-runner 隔离容器里跑。
没签名、没登记公钥、隔离 Runner 不健康，平台都会拒绝安装或拒绝执行。

## 1. 交付物

本模板解压后（给开发者改）：

```
echo-tool-template/
├── README.md
├── CHECKLIST.md
├── sign.py            # 打包+签名脚本，不要改算法
├── manifest.yaml      # 必填
└── tool.py            # 入口，必须与 metadata.entrypoint_argv 一致
```

你最终上传的 zip（sign.py 生成）根目录必须有：

```
echo-tool-1.0.0.zip
├── manifest.yaml
├── manifest.sig       # sign.py 写入，Ed25519 JSON
├── tool.py
├── README.md
└── CHECKLIST.md
```

zip **根目录**就要看到这些文件。kind 必须是 agent-tool。不要把 developer-key.json 打进去。

## 2. 和技能包 / 流水线插件的区别

- 技能包：只有 SKILL.md，不跑代码。
- 流水线插件：在构建机执行，入口写在 task.json。
- 本工具包：助手对话里调用，在隔离容器执行，走 JSON-RPC stdin/stdout。

## 3. manifest.yaml 字段

```yaml
schema_version: "1"
kind: agent-tool
name: echo-tool
version: 1.0.0
display_name: 回声工具
description: 把入参 message 原样返回。用来验证打包/签名/隔离链路。
capabilities: []                 # 只能从平台白名单里挑，见下
config_schema:                   # 必须是 JSON Schema，type=object
  type: object
  properties:
    message:
      type: string
      description: 要回显的文本
  required: ["message"]
  additionalProperties: false
metadata:
  entrypoint_argv: ["python", "tool.py"]   # 必须是数组，不能写成字符串
  method: echo-tool                        # JSON-RPC method，建议等于 name
  output_schema:                           # 必填
    type: object
    properties:
      echo: { type: string }
  category: general
  risk: read                               # read / write / destructive
  confirm: true                            # 写/破坏类建议 true
```

允许声明的 capabilities（多写一个未知值会安装失败）：

- pipeline:read
- release:read
- agent:read
- artifact:read

需要读平台数据时，容器里用环境变量 HARNESS_CAPABILITY_TOKEN 调
HARNESS_BROKER_URL（平台注入）。不要假设有数据库连接。

## 4. 入口协议（stdin / stdout）

平台会把一行 JSON-RPC 2.0 写到 stdin：

```json
{"jsonrpc":"2.0","id":"<call_id>","method":"echo-tool","params":{"message":"hi"}}
```

你的进程必须在 stdout 打回一行 JSON-RPC，id 必须原样返回：

成功：

```json
{"jsonrpc":"2.0","id":"<call_id>","result":{"echo":"hi"}}
```

失败：

```json
{"jsonrpc":"2.0","id":"<call_id>","error":{"code":-32000,"message":"说明原因"}}
```

退出码必须是 0。业务失败用 JSON-RPC error，不要靠非零退出码（非零会被当成进程崩溃）。
不要往 stdout 打日志。日志打 stderr。

## 5. 运行环境限制（写代码时就要当这些是真的）

- 非 root、只读根文件系统、无外网
- 工作目录是临时解压目录
- 没有平台数据库、没有用户 JWT
- 超时默认 60 秒
- stdout/stderr 有大小上限

所以：禁止 sleep 很久、禁止下载外网、禁止写 /etc、禁止读 /packages 以外的路径。

## 6. 签名（没有这一步一定装不上）

不要手写签名。模板里已经放了 `sign.py`，算法和平台校验完全一致。

```bash
pip install cryptography pyyaml
python sign.py
```

会生成：

- `developer-key.json`：私钥。**禁止上传、禁止提交 git、禁止放进 zip。**
- `echo-tool-1.0.0.zip`：可上传的包（根目录含 manifest.sig）。

把终端打印的公钥 JSON 交给管理员，粘到「平台设置」`harness_tool_signing_keys`，例如：

```json
{"dev-key":"<base64 编码的 32 字节 Ed25519 公钥>"}
```

管理员登记公钥之后，才能在技能库上传这个 zip。未登记的 key_id 一律安装失败。

## 7. 上传

技能库不会单独装未签名工具。走同一入口「上传技能包/工具包」，平台按文件识别：
有 SKILL.md → 技能包；有 manifest.sig → 工具包。

工具安装后默认停用，管理员确认隔离 Runner 正常后再启用。
启用失败常见原因：未配置 HARNESS_RUNNER_URL / TOKEN，或 Runner 自报隔离条件不满足。
"""

TOOL_CHECKLIST = """# 工具包提交检查清单

- [ ] kind 是 agent-tool
- [ ] name 小写无空格
- [ ] version 是语义化版本
- [ ] metadata.entrypoint_argv 是非空数组
- [ ] config_schema.type 是 object
- [ ] metadata.output_schema 是对象
- [ ] capabilities 只使用平台白名单
- [ ] 入口从 stdin 读 JSON-RPC，stdout 只打 JSON-RPC，id 原样返回
- [ ] 日志在 stderr
- [ ] zip 根目录有 manifest.yaml 和 manifest.sig
- [ ] 用本目录 sign.py 生成签名，没有手改算法
- [ ] 签名者 key_id 已在平台设置登记
- [ ] zip 里没有 developer-key.json
- [ ] 没把私钥打进包里
"""

TOOL_MANIFEST = """schema_version: "1"
kind: agent-tool
name: echo-tool
version: 1.0.0
display_name: 回声工具
description: 把入参 message 原样返回。用于验证打包、签名和隔离执行链路。
capabilities: []
config_schema:
  type: object
  properties:
    message:
      type: string
      description: 要回显的文本
  required:
    - message
  additionalProperties: false
metadata:
  entrypoint_argv:
    - python
    - tool.py
  method: echo-tool
  category: general
  risk: read
  confirm: false
  output_schema:
    type: object
    properties:
      echo:
        type: string
"""

TOOL_PY = '''#!/usr/bin/env python3
"""隔离容器里的 JSON-RPC 入口。不要 print 普通日志到 stdout。"""
from __future__ import annotations

import json
import sys


def main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"stdin 不是 JSON: {exc}\\n")
        print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}))
        return 0
    call_id = request.get("id")
    params = request.get("params") or {}
    message = str(params.get("message") or "")
    print(json.dumps({"jsonrpc": "2.0", "id": call_id, "result": {"echo": message}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

TOOL_SIGN_PY = r'''#!/usr/bin/env python3
"""把当前目录打成已签名的 agent-tool zip。算法必须与平台 signing.py 一致。

用法（在解压后的模板目录里）：
  pip install cryptography pyyaml
  python sign.py

输出：
  developer-key.json   私钥，绝对不要上传
  <name>-<version>.zip 可上传的包
"""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

SKIP_NAMES = {"developer-key.json", "manifest.sig", "sign.py"}
SKIP_SUFFIX = {".zip", ".pyc"}


def content_digest(archive: zipfile.ZipFile) -> str:
    digest = hashlib.sha256()
    entries = sorted(
        name.replace("\\", "/")
        for name in archive.namelist()
        if not name.endswith("/") and name.replace("\\", "/") != "manifest.sig"
    )
    for name in entries:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(archive.read(name)).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def load_or_create_key(path: Path) -> tuple[Ed25519PrivateKey, str]:
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(doc["private_key"]))
        return private_key, str(doc["key_id"])
    private_key = Ed25519PrivateKey.generate()
    key_id = "dev-key"
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    private_b64 = base64.b64encode(
        private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    ).decode()
    path.write_text(
        json.dumps(
            {"key_id": key_id, "private_key": private_b64, "public_key": public_b64},
            indent=2,
        ),
        encoding="utf-8",
    )
    return private_key, key_id


def main() -> int:
    root = Path(__file__).resolve().parent
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit("manifest.yaml 必须是映射")
    name = str(manifest.get("name") or "tool")
    version = str(manifest.get("version") or "1.0.0")
    private_key, key_id = load_or_create_key(root / "developer-key.json")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SKIP_NAMES or path.suffix in SKIP_SUFFIX or "__pycache__" in path.parts:
            continue
        files[rel] = path.read_bytes()
    out = root / f"{name}-{version}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, data in sorted(files.items()):
            archive.writestr(rel, data)
        digest = content_digest(archive)
        payload = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            + digest.lower()
        ).encode()
        signature = base64.b64encode(private_key.sign(payload)).decode()
        archive.writestr(
            "manifest.sig",
            json.dumps({"key_id": key_id, "signature": signature}, ensure_ascii=False),
        )
    public_b64 = json.loads((root / "developer-key.json").read_text(encoding="utf-8"))["public_key"]
    print(f"已生成 {out.name}")
    print("把下面这段交给管理员，粘到平台设置 harness_tool_signing_keys：")
    print(json.dumps({key_id: public_b64}, ensure_ascii=False))
    print("不要上传 developer-key.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

PLUGIN_SIGN_PY = r'''#!/usr/bin/env python3
"""把当前目录打成已签名的流水线插件 zip。算法必须与平台 signing.py 一致。

第三方上传必须带 manifest.sig；平台内置插件由仓库同步，不走这条。
公钥登记到平台设置 harness_tool_signing_keys（与 Agent 工具包同一套）。

用法：
  pip install cryptography
  python sign.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

SKIP_NAMES = {"developer-key.json", "manifest.sig", "sign.py"}
SKIP_SUFFIX = {".zip", ".pyc"}


def content_digest(archive: zipfile.ZipFile) -> str:
    digest = hashlib.sha256()
    entries = sorted(
        name.replace("\\", "/")
        for name in archive.namelist()
        if not name.endswith("/") and name.replace("\\", "/") != "manifest.sig"
    )
    for name in entries:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(archive.read(name)).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def load_or_create_key(path: Path) -> tuple[Ed25519PrivateKey, str]:
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(doc["private_key"]))
        return private_key, str(doc["key_id"])
    private_key = Ed25519PrivateKey.generate()
    key_id = "dev-key"
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    private_b64 = base64.b64encode(
        private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    ).decode()
    path.write_text(
        json.dumps(
            {"key_id": key_id, "private_key": private_b64, "public_key": public_b64},
            indent=2,
        ),
        encoding="utf-8",
    )
    return private_key, key_id


def main() -> int:
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "task.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit("task.json 必须是对象")
    name = str(manifest.get("name") or "plugin")
    version = str(manifest.get("version") or "1.0.0")
    private_key, key_id = load_or_create_key(root / "developer-key.json")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SKIP_NAMES or path.suffix in SKIP_SUFFIX or "__pycache__" in path.parts:
            continue
        files[rel] = path.read_bytes()
    out = root / f"{name}-{version}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, data in sorted(files.items()):
            archive.writestr(rel, data)
        digest = content_digest(archive)
        payload = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            + digest.lower()
        ).encode()
        signature = base64.b64encode(private_key.sign(payload)).decode()
        archive.writestr(
            "manifest.sig",
            json.dumps({"key_id": key_id, "signature": signature}, ensure_ascii=False),
        )
    public_b64 = json.loads((root / "developer-key.json").read_text(encoding="utf-8"))["public_key"]
    print(f"已生成 {out.name}")
    print("把下面这段交给管理员，粘到平台设置 harness_tool_signing_keys：")
    print(json.dumps({key_id: public_b64}, ensure_ascii=False))
    print("不要上传 developer-key.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

PLUGIN_README = """# RELEASE 流水线插件包开发说明书（给开发者和 AI）

本模板做的是**流水线步骤**，不是 AI 技能。
用户在编排器里拖一个步骤 → 构建机下载本 zip → 在插件目录执行 entrypoint。

## 1. 交付物（zip 根目录必须能直接看到 task.json）

```
hello-echo-1.0.0.zip
├── README.md
├── CHECKLIST.md
├── task.json          # 必填
├── task.py            # 与 entrypoint 一致
├── sign.py            # 第三方上传前必须用它签名
├── manifest.sig       # sign.py 写入，Ed25519 JSON
└── release_atom_sdk/     # Python 必带，平台模板已放好，不要删
```

不要外套 `hello-echo/` 这一层。解压后第一层就是 task.json。

上传入口：技能库 → 流水线插件 → 上传插件 zip。上传后还要点「安装」，安装后才会出现在编排器。
模板下载：同一位置点「下载插件开发模板」。解压后按本文件改 name / task.py 再打包。

## 2. task.json 每个字段

| 字段 | 必填 | 合法值 / 规则 |
|------|------|----------------|
| name | 是 | 流水线 YAML 的 plugin: name，全局唯一，建议小写+连字符 |
| display_name | 否 | 编排器显示名，可中文 |
| category | 否 | source / build / deploy / notify / trigger / exec / artifact |
| version | 否 | 建议 1.0.0。升级必须改 version，否则构建机继续用旧缓存 |
| language | 否 | python / java / nodejs |
| entrypoint | 是 | 在插件解压目录执行的命令。Python 用 python3 task.py |
| description | 否 | 商店描述：这个步骤干什么、何时用、和相近步骤差在哪 |
| config_schema.fields | 否 | 编排器表单。每个 field.key 会进入步骤 with，运行时从 get_input() 读取 |

config_schema.fields 每一项：

| 属性 | 规则 |
|------|------|
| key | 必填。字母或下划线开头的标识符，会成为 with.xxx |
| label | 表单标签，可中文 |
| type | 只能是：text / textarea / password / number / radio / select / checkbox / switch / code / group |
| required | true/false |
| default | 建议给；必填又没默认值，用户漏填步骤会失败 |
| placeholder | 输入提示 |
| options | type=select 或 radio 时必填，形如 [{"label":"生产","value":"prod"}] |
| children | type=group 时必填，里面再嵌套 fields |

task.py 里 `inp = sdk.get_input()` 得到的键，必须和 fields[].key 一字不差。

language=python 时 entrypoint 必须包含 python（平台体检会查）。
entrypoint 里写到的脚本文件必须真的在 zip 里。

Windows 构建机会把入口里的 python3 换成 python。

## 3. 运行时环境（Agent 注入，不要自己猜路径）

工作目录 = 插件解压目录，**不是**代码仓库目录。

| 环境变量 | 含义 |
|----------|------|
| RELEASE_WORKSPACE | 流水线工作区 |
| RELEASE_SRC | 代码目录，通常是工作区下的 src |
| RELEASE_ATOM_INPUT_JSON | 步骤 with 的 JSON 字符串 |
| RELEASE_PIPELINE_ID | 流水线 ID |
| RELEASE_BUILD_ID | 构建任务 ID |
| RELEASE_RELEASE_ID | 发布 ID |
| RELEASE_SERVER_URL | 平台地址 |
| RELEASE_TASK_TOKEN | **仅当前任务**有效的回调凭证，放请求头 X-Task-Token |

读入参请用 SDK：`inp = sdk.get_input()`，不要自己 parse 一半环境变量。
需要碰仓库文件时用 `sdk.get_workspace()` 或环境变量 RELEASE_SRC。

禁止使用构建机长期 Token。老接口 get_agent_token() 不要在新插件里用。

## 4. 成功 / 失败怎么告诉平台

- 进程退出码 0 = 步骤成功；非 0 = 步骤失败，Job 终止。
- 日志打 stdout/stderr，建议前缀 [INFO]: [ERROR]:
- 结构化输出（推荐）用 SDK set_output，或写 RELEASE_WORKSPACE/.release_atom_output.json
- 也可以打一行 `##[set-output]key=value`

## 5. 改这个模板时你最小要动哪些地方

1. task.json 的 name / display_name / description / version / config_schema.fields
2. task.py 里读取的 inp["..."] 键，必须和 fields.key 一致
3. 打包前按 CHECKLIST.md 勾完

不要改 release_atom_sdk 目录里的文件。

## 6. 打包与签名

平台内置插件由仓库同步，不验签。第三方上传的 zip **必须**用本目录 `sign.py` 生成 `manifest.sig`，公钥登记到平台设置 `harness_tool_signing_keys`（与 Agent 工具包同一套密钥）。

```bash
pip install cryptography
python sign.py
```

不要手写签名。算法与平台校验一致。不要把 developer-key.json 打进 zip。

## 7. 上传失败对照表

| 报错 | 处理 |
|------|------|
| 缺少 task.json | zip 根目录没有它，或外套了文件夹 |
| 缺少 name | task.json 要有 name |
| 第三方插件必须包含 manifest.sig | 用 sign.py 签名，并把公钥交给管理员 |
| 插件签名校验失败 | 公钥未登记、或打包后又改了文件 |
| entrypoint 引用了 xx 但包里没有 | 文件名和 entrypoint 不一致 |
| language=python 的 entrypoint 应形如 python3 task.py | 入口字符串里要有 python |
"""

PLUGIN_CHECKLIST = """# 流水线插件提交检查清单

- [ ] description 写清干什么、何时用、别和相近插件搞混
- [ ] zip 根目录有 task.json
- [ ] name 全局唯一、建议小写+连字符
- [ ] entrypoint 与包内脚本文件名一致
- [ ] language 与入口命令匹配
- [ ] config_schema.fields.key 和 task.py 读取的 inp 键一致
- [ ] 用 SDK 读输入、写输出，没有改 release_atom_sdk
- [ ] 退出码 0 表示成功
- [ ] 没有把密钥写进源码
- [ ] 第三方包根目录有 manifest.sig（用本目录 sign.py 生成）
- [ ] 公钥已交给管理员写入 harness_tool_signing_keys
- [ ] 没有把 developer-key.json 打进 zip
"""

PLUGIN_TASK_JSON = {
    "name": "hello-echo",
    "display_name": "回声示例插件",
    "category": "exec",
    "version": "1.0.0",
    "language": "python",
    "entrypoint": "python3 task.py",
    "description": "读取编排器表单里的 message，打日志并写结构化输出。在验证插件打包和签名链路时用。不是业务构建或部署步骤。",
    "config_schema": {
        "fields": [
            {
                "key": "message",
                "label": "要打印的消息",
                "type": "text",
                "required": True,
                "default": "hello",
                "placeholder": "会写入步骤 with.message，运行时从 get_input() 读取",
            }
        ]
    },
}

PLUGIN_TASK_PY = '''# -*- coding: utf-8 -*-
"""流水线步骤入口。cwd 是插件解压目录，仓库代码在 RELEASE_SRC。"""
from __future__ import annotations

import sys

import release_atom_sdk as sdk


def main() -> int:
    inp = sdk.get_input()
    message = str(inp.get("message") or "")
    sdk.log.info("workspace=" + sdk.get_workspace())
    sdk.log.info("message=" + message)
    if not message.strip():
        sdk.log.error("message 为空。请在编排器步骤参数里填写。")
        return 1
    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "ok",
            "type": sdk.output_template_type.DEFAULT,
            "data": {
                "echo": {
                    "type": sdk.output_field_type.STRING,
                    "value": message,
                }
            },
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            archive.writestr(name, data)
    return buf.getvalue()


def _add_plugin_sdk(files: dict[str, str | bytes]) -> None:
    if not _PLUGIN_SDK.is_dir():
        return
    for path in _PLUGIN_SDK.rglob("*"):
        if path.is_file() and path.suffix == ".py":
            relative = Path("release_atom_sdk") / path.relative_to(_PLUGIN_SDK)
            files[relative.as_posix()] = path.read_bytes()


def skill_package() -> bytes:
    return _zip_bytes(
        {
            "README.md": SKILL_README,
            "CHECKLIST.md": SKILL_CHECKLIST,
            "manifest.yaml": SKILL_MANIFEST,
            "SKILL.md": SKILL_BODY,
            "examples.md": SKILL_EXAMPLES,
        }
    )


def tool_package() -> bytes:
    return _zip_bytes(
        {
            "README.md": TOOL_README,
            "CHECKLIST.md": TOOL_CHECKLIST,
            "manifest.yaml": TOOL_MANIFEST,
            "tool.py": TOOL_PY,
            "sign.py": TOOL_SIGN_PY,
        }
    )


def plugin_package() -> bytes:
    files: dict[str, str | bytes] = {
        "README.md": PLUGIN_README,
        "CHECKLIST.md": PLUGIN_CHECKLIST,
        "task.json": json.dumps(PLUGIN_TASK_JSON, ensure_ascii=False, indent=2) + "\n",
        "task.py": PLUGIN_TASK_PY,
        "sign.py": PLUGIN_SIGN_PY,
    }
    _add_plugin_sdk(files)
    return _zip_bytes(files)


def download(filename: str, data: bytes) -> Response:
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
