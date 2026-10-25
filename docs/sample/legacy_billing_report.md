# 代码审查报告 · legacy_billing.py

- 审查结论：**✅ 通过**
- 总分：**100 / 100**
- 迭代轮次：3 轮审查
- 代码版本：v3
- 阻塞问题：0　警告：0　提示：1

## 一、需求

审查并加固已有模块 legacy_billing.py

## 二、剩余问题

无阻塞问题，无告警。

## 三、提示项

| 规则 | 行 | 说明 | 建议 |
| --- | --- | --- | --- |
| `broad-except` | 24 | 捕获 Exception 后静默吞掉 | 记录异常并区分可恢复与不可恢复错误 |

## 四、自动修复记录

| 规则 | 位置 | 处理 |
| --- | --- | --- |
| `module-docstring` | L1 · <module> | 已自动修复 |
| `function-docstring` | L7 · fetch_orders | 已自动修复 |
| `missing-type-hints` | L7 · fetch_orders | 已自动修复 |
| `bare-except` | L12 | 已自动修复 |
| `function-docstring` | L15 · save_receipt | 已自动修复 |
| `missing-type-hints` | L15 · save_receipt | 已自动修复 |
| `open-without-encoding` | L16 · open | 已自动修复 |
| `function-docstring` | L19 · checksum | 已自动修复 |
| `missing-type-hints` | L19 · checksum | 已自动修复 |
| `function-docstring` | L22 · normalize | 已自动修复 |
| `missing-type-hints` | L22 · normalize | 已自动修复 |
| `mutable-default-argument` | L22 · normalize | 已自动修复 |

## 五、代码演进 (v1 → 终版)

```diff
--- legacy_billing.py (v1 / 编码 Agent)
+++ legacy_billing.py (v3 / 修复 Agent)
@@ -1,40 +1,57 @@
-# 演示用的「历史遗留模块」：故意保留 10 处典型缺陷，用来验证
-#   python -m agentflow run --workflow code_review --target examples/legacy_billing.py
-# 全部缺陷都能被自动修复，流程会经历「3 轮审查 + 2 轮修复」后通过。
-# 缺陷清单（对应 agentflow/agents/linting.py 的规则 id）：
-#   hardcoded-secret / sql-injection / weak-hash          ← 阻塞级（先修）
-#   module-docstring / function-docstring / missing-type-hints
-#   bare-except / open-without-encoding / unused-import / mutable-default-argument  ← 风格级
+"""legacy_billing.py 的模块说明（由修复 Agent 自动补充）。
+
+待审对象：legacy_billing.py
+审查目的：审查并加固已有模块 legacy_billing.py
+约束：凭据一律从环境变量读取；SQL 使用参数化查询。
+TODO: 由修复 Agent 依据文件信息生成，请人工确认职责与使用方式。"""
+from typing import Any
 import hashlib
 import json
 import os
-
-API_TOKEN = "ghp_9f3c1d4e9a2b"
+API_TOKEN = os.environ.get('API_TOKEN', '')
 DEFAULT_FILTERS = {}
 
+def fetch_orders(conn: Any, status: Any) -> Any:
+    """fetch_orders: 自动补全的文档字符串。
 
-def fetch_orders(conn, status):
+    参数: conn, status
+    返回: Any
+    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
     try:
         cur = conn.cursor()
-        cur.execute("SELECT * FROM orders WHERE status = '%s'" % status)
+        cur.execute('SELECT * FROM orders WHERE status = ?', (status,))
         return cur.fetchall()
-    except:
+    except Exception:
         return []
 
+def save_receipt(order: Any, path: str) -> None:
+    """save_receipt: 自动补全的文档字符串。
 
-def save_receipt(order, path):
-    with open(path, "w") as f:
+    参数: order, path
+    返回: None
+    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
+    with open(path, 'w', encoding='utf-8') as f:
         json.dump(order, f)
 
+def checksum(payload: dict) -> Any:
+    """checksum: 自动补全的文档字符串。
 
-def checksum(payload):
-    return hashlib.sha1(payload.encode()).hexdigest()
+    参数: payload
+    返回: Any
+    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
+    return hashlib.sha256(payload.encode()).hexdigest()
 
+def normalize(items: list, filters: Any=None) -> Any:
+    """normalize: 自动补全的文档字符串。
 
-def normalize(items, filters=DEFAULT_FILTERS):
+    参数: items, filters
+    返回: Any
+    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
+    if filters is None:
+        filters = {}
     out = []
     for item in items:
-        if item.get("status") in filters.get("hide", []):
+        if item.get('status') in filters.get('hide', []):
             continue
         out.append(item)
     return out
```

## 六、终版代码

```python
"""legacy_billing.py 的模块说明（由修复 Agent 自动补充）。

待审对象：legacy_billing.py
审查目的：审查并加固已有模块 legacy_billing.py
约束：凭据一律从环境变量读取；SQL 使用参数化查询。
TODO: 由修复 Agent 依据文件信息生成，请人工确认职责与使用方式。"""
from typing import Any
import hashlib
import json
import os
API_TOKEN = os.environ.get('API_TOKEN', '')
DEFAULT_FILTERS = {}

def fetch_orders(conn: Any, status: Any) -> Any:
    """fetch_orders: 自动补全的文档字符串。

    参数: conn, status
    返回: Any
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM orders WHERE status = ?', (status,))
        return cur.fetchall()
    except Exception:
        return []

def save_receipt(order: Any, path: str) -> None:
    """save_receipt: 自动补全的文档字符串。

    参数: order, path
    返回: None
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(order, f)

def checksum(payload: dict) -> Any:
    """checksum: 自动补全的文档字符串。

    参数: payload
    返回: Any
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    return hashlib.sha256(payload.encode()).hexdigest()

def normalize(items: list, filters: Any=None) -> Any:
    """normalize: 自动补全的文档字符串。

    参数: items, filters
    返回: Any
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    if filters is None:
        filters = {}
    out = []
    for item in items:
        if item.get('status') in filters.get('hide', []):
            continue
        out.append(item)
    return out
```

