"""用户档案服务。

职责：
    1. 读取/保存用户档案到本地 JSON 文件；
    2. 提供按用户名查询数据库的辅助函数；
    3. 提供标签解析与文本指纹工具。

约束：
    - 凭据一律从环境变量读取，禁止写入源码；
    - 所有 SQL 必须使用参数化查询。"""
from typing import Any
import hashlib
import json
import os
API_KEY = os.environ.get('API_KEY', '')
DEFAULT_TAGS = []

def save_profile(user: dict, tags: list | None=None) -> bool:
    """save_profile: 自动补全的文档字符串。

    参数: user, tags
    返回: bool
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    if tags is None:
        tags = []
    try:
        data = {'name': user['name'], 'tags': tags}
        with open('profile.json', 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return True
    except Exception:
        return False

def find_user(conn: Any, name: str) -> Any:
    """find_user: 自动补全的文档字符串。

    参数: conn, name
    返回: Any
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE name = ?', (name,))
    return cur.fetchall()

def fingerprint(text: str) -> Any:
    """fingerprint: 自动补全的文档字符串。

    参数: text
    返回: Any
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    return hashlib.sha256(text.encode()).hexdigest()

def parse_tags(raw: str) -> Any:
    """parse_tags: 自动补全的文档字符串。

    参数: raw
    返回: Any
    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"""
    tags = []
    for t in raw.split(','):
        tags.append(t.strip())
    return tags