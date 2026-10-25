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