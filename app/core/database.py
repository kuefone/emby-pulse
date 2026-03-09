import sqlite3
import os
import requests
import json
import logging
from app.core.config import cfg, DB_PATH

logger = logging.getLogger("uvicorn")

def init_db():
    # 确保数据库目录存在
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except: pass

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # 0. 播放记录表
        c.execute('''
            CREATE TABLE IF NOT EXISTS PlaybackActivity (
                Id INTEGER PRIMARY KEY AUTOINCREMENT,
                UserId TEXT,
                UserName TEXT,
                ItemId TEXT,
                ItemName TEXT,
                PlayDuration INTEGER,
                DateCreated DATETIME DEFAULT CURRENT_TIMESTAMP,
                Client TEXT,
                DeviceName TEXT
            )
        ''')
        
        # 1. 只初始化机器人专属配置表 (不碰插件的表)
        c.execute('''CREATE TABLE IF NOT EXISTS users_meta (
                        user_id TEXT PRIMARY KEY,
                        expire_date TEXT,
                        note TEXT,
                        created_at TEXT
                    )''')
        
        # 2. 邀请码表 (合并了双版本的字段)
        c.execute('''CREATE TABLE IF NOT EXISTS invitations (
                        code TEXT PRIMARY KEY,
                        days INTEGER,        -- 有效期天数 (-1为永久)
                        used_count INTEGER DEFAULT 0,
                        max_uses INTEGER DEFAULT 1,
                        created_at TEXT,
                        used_at DATETIME,
                        used_by TEXT,
                        status INTEGER DEFAULT 0,
                        template_user_id TEXT -- 绑定的权限模板用户
                    )''')
        
        # 兼容老版本数据库：尝试追加列 (如果列已存在会抛异常，忽略即可)
        try:
            c.execute("ALTER TABLE invitations ADD COLUMN template_user_id TEXT")
        except:
            pass

        # 3. 追剧日历本地缓存表
        c.execute('''CREATE TABLE IF NOT EXISTS tv_calendar_cache (
                        id TEXT PRIMARY KEY,       -- 组合主键: seriesId_season_episode
                        series_id TEXT,            -- Emby 剧集 ID，用于 Webhook 联动
                        season INTEGER,
                        episode INTEGER,
                        air_date TEXT,             -- 播出日期 (YYYY-MM-DD)
                        status TEXT,               -- 红绿灯状态: ready/missing/upcoming/today
                        data_json TEXT             -- 完整数据的 JSON 文本
                    )''')

        # 4. 求片资源主表 (同步最新多季架构，引入 season 和 复合主键)
        c.execute('''
            CREATE TABLE IF NOT EXISTS media_requests (
                tmdb_id INTEGER,
                media_type TEXT,
                title TEXT,
                year TEXT,
                poster_path TEXT,
                status INTEGER DEFAULT 0,
                season INTEGER DEFAULT 0,
                reject_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tmdb_id, season)
            )
        ''')

        # 5. 求片用户关联表 (+1 机制，同步引入 season 复合唯一约束)
        c.execute('''
            CREATE TABLE IF NOT EXISTS request_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER,
                user_id TEXT,
                username TEXT,
                season INTEGER DEFAULT 0,
                requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tmdb_id, user_id, season)
            )
        ''')
        
        # 6. 质量盘点忽略名单 (insight_ignores)
        c.execute('''
            CREATE TABLE IF NOT EXISTS insight_ignores (
                item_id TEXT PRIMARY KEY,
                item_name TEXT,
                ignored_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 7. 缺集管理记录表 (gap_records)
        c.execute('''
            CREATE TABLE IF NOT EXISTS gap_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id TEXT,
                series_name TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                status INTEGER DEFAULT 0, -- 1: 永久忽略(屏蔽), 2: MP处理中(蓝灯)
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, season_number, episode_number)
            )
        ''')

        conn.commit()
        conn.close()
        print("✅ Database initialized.")
    except Exception as e: 
        print(f"❌ DB Init Error: {e}")


# --- 魔法工具：将带 ? 的 SQL 和参数转换为纯字符串 ---
def _interpolate_sql(query: str, args) -> str:
    if not args: return query
    parts = query.split('?')
    if len(parts) - 1 != len(args): return query # 防止异常
    res = parts[0]
    for i, arg in enumerate(args):
        if isinstance(arg, bool): val = "1" if arg else "0"
        elif isinstance(arg, (int, float)): val = str(arg)
        elif arg is None: val = "NULL"
        else: val = f"'{str(arg).replace(chr(39), chr(39)+chr(39))}'" # 防注入单引号转义
        res += val + parts[i+1]
    return res


def query_db(query, args=(), one=False):
    # ==========================================
    # 🔥 双擎路由拦截器 (API 穿透模式)
    # ==========================================
    mode = cfg.get("playback_data_mode", "sqlite")
    is_playback_query = "PlaybackActivity" in query or "PlaybackReporting" in query
    
    if mode == "api" and is_playback_query:
        host = cfg.get("emby_host")
        token = cfg.get("emby_api_key")
        if host and token:
            full_sql = _interpolate_sql(query, args)
            url = f"{host.rstrip('/')}/emby/user_usage_stats/submit_custom_query"
            headers = {"X-Emby-Token": token, "Content-Type": "application/json"}
            payload = {"CustomQueryString": full_sql}
            
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=20)
                if res.status_code == 200:
                    raw_data = None
                    try:
                        res_json = res.json()
                        # 🔥 核心防御：应对 Emby 插件把 JSON 作为纯字符串返回的"二次套娃" Bug
                        if isinstance(res_json, str):
                            try: raw_data = json.loads(res_json)
                            except: raw_data = res_json
                        else:
                            raw_data = res_json
                    except:
                        try: raw_data = json.loads(res.text)
                        except: raw_data = []
                    
                    # 兼容套壳
                    if isinstance(raw_data, dict):
                        raw_data = raw_data.get("results", raw_data.get("Items", [raw_data]))
                        
                    if raw_data is None:
                        raw_data = []
                        
                    # 确保最终产物是一个标准的 List，完美顶替 sqlite3.fetchall()
                    data = raw_data if isinstance(raw_data, list) else []

                    if query.strip().upper().startswith("SELECT"):
                        return (data[0] if data else None) if one else data
                    return True
                else:
                    logger.error(f"[API 路由] 穿透查询失败: HTTP {res.status_code}, 响应: {res.text}")
            except Exception as e:
                logger.error(f"[API 路由] 网络或解析异常: {e}")
        # 如果 API 失败或未配置，代码会继续往下走，平滑降级回 sqlite 模式
    
    # ==========================================
    # 🚂 原版 SQLite 执行器 (处理非播放表及降级情况)
    # ==========================================
    if not os.path.exists(DB_PATH): return None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=20.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        if query.strip().upper().startswith("SELECT"):
            rv = cur.fetchall()
            conn.close()
            return (rv[0] if rv else None) if one else rv
        else:
            conn.commit()
            conn.close()
            return True
    except Exception as e: 
        logger.error(f"[SQLite] 失败: {e} | Query: {query}")
        return None

def get_base_filter(user_id_filter):
    where = "WHERE 1=1"
    params = []
    
    if user_id_filter and user_id_filter != 'all':
        where += " AND UserId = ?"
        params.append(user_id_filter)
    
    hidden = cfg.get("hidden_users")
    if (not user_id_filter or user_id_filter == 'all') and hidden and len(hidden) > 0:
        placeholders = ','.join(['?'] * len(hidden))
        where += f" AND UserId NOT IN ({placeholders})"
        params.extend(hidden)
        
    return where, params