# 演示用的「历史遗留模块」：故意保留 10 处典型缺陷，用来验证
#   python -m agentflow run --workflow code_review --target examples/legacy_billing.py
# 全部缺陷都能被自动修复，流程会经历「3 轮审查 + 2 轮修复」后通过。
# 缺陷清单（对应 agentflow/agents/linting.py 的规则 id）：
#   hardcoded-secret / sql-injection / weak-hash          ← 阻塞级（先修）
#   module-docstring / function-docstring / missing-type-hints
#   bare-except / open-without-encoding / unused-import / mutable-default-argument  ← 风格级
import hashlib
import json
import os

API_TOKEN = "ghp_9f3c1d4e9a2b"
DEFAULT_FILTERS = {}


def fetch_orders(conn, status):
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE status = '%s'" % status)
        return cur.fetchall()
    except:
        return []


def save_receipt(order, path):
    with open(path, "w") as f:
        json.dump(order, f)


def checksum(payload):
    return hashlib.sha1(payload.encode()).hexdigest()


def normalize(items, filters=DEFAULT_FILTERS):
    out = []
    for item in items:
        if item.get("status") in filters.get("hide", []):
            continue
        out.append(item)
    return out
