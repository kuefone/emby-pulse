from fastapi import APIRouter, Request
from pydantic import BaseModel
from app.core.config import cfg
from app.core.database import query_db, DB_PATH
import requests
import sqlite3
import logging
import time
from datetime import datetime

logger = logging.getLogger("uvicorn")
router = APIRouter()

# --- 全局缓存 ---
GLOBAL_CACHE = { "quality_stats": None, "last_scan_time": 0 }
CACHE_EXPIRE_SECONDS = 86400 

def get_emby_auth(): return cfg.get("emby_host"), cfg.get("emby_api_key")

# --- 忽略名单管理模型 ---
class IgnoreModel(BaseModel):
    item_id: str
    item_name: str

class BatchUnignoreModel(BaseModel):
    item_ids: list[str]

# --- 忽略接口 ---
@router.post("/api/insight/ignore")
def ignore_item(data: IgnoreModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO insight_ignores (item_id, item_name) VALUES (?, ?)", (data.item_id, data.item_name))
        conn.commit()
        conn.close()
        # 清理缓存强制刷新
        GLOBAL_CACHE["quality_stats"] = None
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/insight/unignore_batch")
def unignore_items_batch(data: BatchUnignoreModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        placeholders = ','.join(['?'] * len(data.item_ids))
        c.execute(f"DELETE FROM insight_ignores WHERE item_id IN ({placeholders})", data.item_ids)
        conn.commit()
        conn.close()
        GLOBAL_CACHE["quality_stats"] = None
        return {"status": "success"}
    except Exception as e: return {"status": "error"}

@router.get("/api/insight/ignores")
def get_ignored_items(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    rows = query_db("SELECT * FROM insight_ignores ORDER BY ignored_at DESC")
    return {"status": "success", "data": [dict(r) for r in rows] if rows else []}


@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """ 质量盘点核心引擎（支持下钻全量数据） """
    user = request.session.get("user")
    if not user: return {"status": "error", "message": "Unauthorized: 请先登录"}
    
    force_refresh = request.query_params.get("force_refresh") == "true"
    current_time = time.time()
    
    if not force_refresh and GLOBAL_CACHE["quality_stats"] and (current_time - GLOBAL_CACHE["last_scan_time"] < CACHE_EXPIRE_SECONDS):
        return {"status": "success", "data": GLOBAL_CACHE["quality_stats"]}

    host, key = get_emby_auth()
    if not host or not key: return {"status": "error", "message": "Emby 未配置"}

    try:
        # 获取忽略名单
        ignore_rows = query_db("SELECT item_id FROM insight_ignores")
        ignore_set = {r['item_id'] for r in ignore_rows} if ignore_rows else set()

        headers = {"X-Emby-Token": key, "Accept": "application/json"}
        # 🔥 修改点：多抓取 Id 和 DateCreated 以便支持前端点击查看详情
        query_params = "Recursive=true&IncludeItemTypes=Movie&Fields=MediaSources,Path,MediaStreams,ProviderIds,DateCreated"
        url = f"{host}/emby/Items?{query_params}"
        
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code != 200: return {"status": "error"}
            
        items = response.json().get("Items", [])
        
        # 初始化分类容器，用于装全量影片数据
        stats = {
            "total_count": len(items),
            "scan_time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "movies": {
                "4k": [], "1080p": [], "720p": [], "sd": [],
                "hevc": [], "h264": [], "av1": [], "other_codec": [],
                "dolby_vision": [], "hdr10": [], "sdr": []
            }
        }

        for item in items:
            item_id = item.get("Id")
            
            # 如果被忽略，跳过
            if item_id in ignore_set: continue

            media_sources = item.get("MediaSources")
            if not media_sources or not isinstance(media_sources, list): continue
            
            video_stream = next((s for s in media_sources[0].get("MediaStreams", []) if s.get("Type") == "Video"), None)
            if not video_stream: continue

            # 构建通用电影对象 (供前端展示使用)
            movie_obj = {
                "Id": item_id,
                "Name": item.get("Name"),
                "Year": item.get("ProductionYear"),
                "Resolution": f"{video_stream.get('Width', 0)}x{video_stream.get('Height', 0)}",
                "Path": item.get("Path", "未知路径")
            }

            # --- A. 分辨率统计 ---
            width = video_stream.get("Width", 0)
            if width >= 3800: stats["movies"]["4k"].append(movie_obj)
            elif width >= 1900: stats["movies"]["1080p"].append(movie_obj)
            elif width >= 1200: stats["movies"]["720p"].append(movie_obj)
            else: stats["movies"]["sd"].append(movie_obj)

            # --- B. 编码统计 ---
            codec = video_stream.get("Codec", "").lower()
            if "hevc" in codec or "h265" in codec: stats["movies"]["hevc"].append(movie_obj)
            elif "h264" in codec or "avc" in codec: stats["movies"]["h264"].append(movie_obj)
            elif "av1" in codec: stats["movies"]["av1"].append(movie_obj)
            else: stats["movies"]["other_codec"].append(movie_obj)

            # --- C. 动态范围统计 ---
            video_range = video_stream.get("VideoRange", "").lower()
            display_title = video_stream.get("DisplayTitle", "").lower()
            
            if "dolby" in display_title or "dv" in display_title or "dolby" in video_range: stats["movies"]["dolby_vision"].append(movie_obj)
            elif "hdr" in video_range or "hdr" in display_title or "pq" in video_range: stats["movies"]["hdr10"].append(movie_obj)
            else: stats["movies"]["sdr"].append(movie_obj)

        GLOBAL_CACHE["quality_stats"] = stats
        GLOBAL_CACHE["last_scan_time"] = current_time
        
        return {"status": "success", "data": stats}

    except Exception as e:
        logger.error(f"质量盘点错误: {str(e)}")
        return {"status": "error"}