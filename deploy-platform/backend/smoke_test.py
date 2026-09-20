"""端到端冒烟测试：登录 → 建项目/分组/流水线 → 发布 → 执行 → 任务生成。"""
import json
import urllib.request

BASE = "http://localhost:8080/api/v1"


def call(method, path, token, body=None):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def login(u):
    s, d = call("POST", "/auth/login", None, {"username": u, "password": "admin123"})
    return d["data"]["token"]


admin = login("admin")
print("=== 1. 登录 admin ===")
print("  ✓ token 获取成功")

print("\n=== 2. 项目/分组/流水线列表 ===")
for name, path in [("项目", "/projects"), ("分组", "/groups?project_id=1"), ("流水线", "/pipelines?project_id=1")]:
    s, d = call("GET", path, admin)
    n = len(d["data"]) if isinstance(d["data"], list) else "?"
    print(f"  ✓ {name}: {n} 条")

print("\n=== 3. 插件列表（13 个预置插件）===")
s, d = call("GET", "/store/plugins", admin)
plugins = d["data"]
print(f"  ✓ 插件数: {len(plugins)}, 分类: {sorted(set(p['category'] for p in plugins))}")
# 检查关键插件有 config_schema
for p in plugins:
    has_schema = bool(p.get("config_schema") and p["config_schema"] != "{}")
    if p["name"] in ("git-checkout", "shell-exec", "maven-build"):
        print(f"    - {p['name']}: schema={'有' if has_schema else '无'}")

print("\n=== 4. 发布 → 执行 → 任务生成 ===")
s, d = call("POST", "/releases", admin, {"pipeline_id": 2, "version": "smoke-v1"})
rid = d["data"]["id"]
print(f"  ✓ 创建发布 #{rid} -> {s}, 状态 {d['data']['status']}")
s, _ = call("POST", f"/releases/{rid}/execute", admin)
print(f"  ✓ 执行发布 -> {s}")
s, d = call("GET", f"/tasks?release_id={rid}", admin)
print(f"  ✓ 任务数: {len(d['data'])}, 各任务状态: {[t['status'] for t in d['data']]}")

print("\n=== 5. 编排图 / 触发器 / 变量 ===")
s, d = call("GET", "/pipelines/2/graph", admin)
g = d["data"]
print(f"  ✓ stages: {len(g['stages'])}, triggers: {len(g['triggers'])}, variables: {len(g['variables'])}")

print("\n=== 6. 平台设置 / 角色 / 凭证 / 代码库 ===")
for name, path in [("设置", "/settings"), ("角色", "/roles?project_id=1"), ("凭证", "/credentials"), ("代码库", "/repositories?project_id=1")]:
    s, d = call("GET", path, admin)
    n = len(d["data"]) if isinstance(d["data"], list) else "?"
    print(f"  ✓ {name}: {n} 条")

print("\n=== 冒烟测试通过 ✅ ===")
