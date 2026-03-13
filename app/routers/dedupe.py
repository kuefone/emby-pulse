import sqlite3
import logging
import threading
import time
import requests
from collections import defaultdict
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List

from app.core.config import cfg
from app.core.database import query_db, DB_PATH

logger = logging.getLogger("uvicorn")
router = APIRouter(prefix="/api/dedupe", tags=["去重管理"])

# 内存全局状态，用于前端轮询进度
scan_state = {
    "is_scanning": False,
    "progress": 0,
    "total_items": 0,
    "duplicate_groups": 0,
    "message": "空闲中"
}

# ==========================================
# 🛠️ 1. 数据库自动初始化
# ==========================================
def init_dedupe_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # 去重结果缓存表
        c.execute('''CREATE TABLE IF NOT EXISTS dedupe_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT,
            tmdb_id TEXT,
            media_type TEXT,
            title TEXT,
            season_num INTEGER,
            episode_num INTEGER,
            item_id TEXT,
            file_name TEXT,
            resolution TEXT,
            bitrate INTEGER,
            size_bytes REAL,
            video_codec TEXT,
            audio_codec TEXT,
            has_hdr INTEGER,
            has_dovi INTEGER,
            has_chi_sub INTEGER,
            has_ass_sub INTEGER,
            score INTEGER,
            is_recommended_del INTEGER DEFAULT 0,
            is_exempt INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        # 用户永久忽略白名单
        c.execute('''CREATE TABLE IF NOT EXISTS dedupe_whitelist (
            group_key TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[去重引擎] 自动建表失败: {e}")

init_dedupe_db()

# ==========================================
# 🧠 2. 核心洗版打分引擎 2.0 (策略模式)
# ==========================================
def calculate_score(src: dict, strategy: str = "quality"):
    score = 0
    video = next((s for s in src.get("MediaStreams", []) if s.get("Type") == "Video"), {})
    audio = next((s for s in src.get("MediaStreams", []) if s.get("Type") == "Audio"), {})
    subs = [s for s in src.get("MediaStreams", []) if s.get("Type") == "Subtitle"]
    
    # 1. 画质得分
    width = video.get("Width", 0)
    if width >= 3800: score += 40
    elif width >= 1900: score += 20
    elif width >= 1200: score += 10
    
    # 2. 码率得分 (最高 20分)
    bitrate = src.get("Bitrate", 0)
    if bitrate > 0:
        score += min(20, int((bitrate / 1000000) / 2))
        
    # 3. 编码得分 (空间刺客流的最爱)
    codec = video.get("Codec", "").lower()
    if "hevc" in codec or "x265" in codec or "av1" in codec:
        score += 30 if strategy == "size" else 5
        
    # 4. HDR / DoVi 特效
    v_range = video.get("VideoRange", "")
    v_title = video.get("DisplayTitle", "").upper()
    if "DOVI" in v_title or "DOLBY VISION" in v_title: score += 15
    elif "HDR" in v_range or "HDR" in v_title: score += 10
    
    # 5. 音轨得分
    a_codec = audio.get("Codec", "").lower()
    channels = audio.get("Channels", 2)
    if "atmos" in audio.get("DisplayTitle", "").lower() or "truehd" in a_codec: score += 15
    elif channels >= 6: score += 5
    
    # 6. 字幕得分 (熟肉判定)
    has_chi = has_ass = False
    for sub in subs:
        lang = sub.get("Language", "").lower()
        if lang in ["chi", "zho", "chs", "cht", "zh"]:
            has_chi = True
            sub_codec = sub.get("Codec", "").lower()
            if "ass" in sub_codec or "ssa" in sub_codec: has_ass = True
            
    if has_chi: score += 40 if strategy == "subs" else 10
    if has_ass: score += 30 if strategy == "subs" else 15
        
    # 7. 体积惩罚 (仅在 size 策略下，体积越大扣分越多，1GB扣2分)
    size = src.get("Size", 0)
    if strategy == "size" and size > 0:
        gb = size / (1024**3)
        score -= int(gb * 2)
        
    return score, {
        "res": f"{width}P" if width else "未知",
        "has_hdr": 1 if ("HDR" in v_range or "HDR" in v_title) else 0,
        "has_dovi": 1 if ("DOVI" in v_title or "DOLBY VISION" in v_title) else 0,
        "has_chi": 1 if has_chi else 0,
        "has_ass": 1 if has_ass else 0,
        "v_codec": codec.upper(),
        "a_codec": a_codec.upper()
    }

# ==========================================
# 🚀 3. 漏斗式异步扫描任务
# ==========================================
def run_dedupe_scan(strategy: str = "quality"):
    global scan_state
    scan_state["is_scanning"] = True
    scan_state["progress"] = 0
    scan_state["message"] = "第一阶段：极速抽取全库索引..."
    
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    try:
        # 获取 Admin ID
        admin_res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5).json()
        admin_id = next((u['Id'] for u in admin_res if u.get("Policy", {}).get("IsAdministrator")), admin_res[0]['Id'])
        
        # 【阶段一：极速获取轻量级索引】
        items = []
        start = 0
        limit = 10000
        while True:
            url = f"{host}/emby/Users/{admin_id}/Items"
            params = {
                "IncludeItemTypes": "Movie,Episode",
                "Recursive": "true",
                "Fields": "ProviderIds,ParentIndexNumber,IndexNumber,IndexNumberEnd",
                "StartIndex": start,
                "Limit": limit,
                "api_key": key
            }
            res = requests.get(url, params=params, timeout=30).json()
            chunk = res.get("Items", [])
            items.extend(chunk)
            if len(chunk) < limit: break
            start += limit
            scan_state["message"] = f"第一阶段：已抽取 {len(items)} 条索引..."
            
        scan_state["total_items"] = len(items)
        scan_state["message"] = "第二阶段：内存哈希碰撞匹配中..."
        
        # 获取白名单
        whitelist = [r['group_key'] for r in query_db("SELECT group_key FROM dedupe_whitelist")]
        
        # 【阶段二：哈希碰撞找重复】
        groups = defaultdict(list)
        for i in items:
            tmdb = i.get("ProviderIds", {}).get("Tmdb")
            if not tmdb: continue
            
            mtype = i.get("Type")
            if mtype == "Movie":
                g_key = f"movie_{tmdb}"
            else:
                s_idx = i.get("ParentIndexNumber", 0)
                e_idx = i.get("IndexNumber", 0)
                g_key = f"tv_{tmdb}_s{s_idx}e{e_idx}"
                
            if g_key not in whitelist:
                groups[g_key].append(i)
                
        # 过滤出重复组
        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        scan_state["duplicate_groups"] = len(dup_groups)
        
        # 清空旧缓存
        conn = sqlite3.connect(DB_PATH)
        conn.cursor().execute("DELETE FROM dedupe_results")
        conn.commit()

        # 【阶段三：精准深层探针与打分】
        total_dups = len(dup_groups)
        current = 0
        
        for g_key, item_list in dup_groups.items():
            current += 1
            scan_state["progress"] = int((current / total_dups) * 100)
            scan_state["message"] = f"第三阶段：深层分析视频流 ({current}/{total_dups})"
            
            # 批量请求这组的具体信息
            ids = ",".join([i["Id"] for i in item_list])
            detail_url = f"{host}/emby/Users/{admin_id}/Items?Ids={ids}&Fields=MediaSources,Path&api_key={key}"
            details = requests.get(detail_url, timeout=10).json().get("Items", [])
            
            parsed_items = []
            for d in details:
                # 检查是否是多集合并版 (豁免)
                is_exempt = 1 if d.get("IndexNumberEnd") and d.get("IndexNumberEnd") > d.get("IndexNumber", 0) else 0
                
                src = d.get("MediaSources", [{}])[0]
                score, tags = calculate_score(src, strategy)
                
                parsed_items.append({
                    "g_key": g_key, "tmdb": d.get("ProviderIds", {}).get("Tmdb"),
                    "mtype": d.get("Type"), "title": d.get("SeriesName") or d.get("Name", ""),
                    "season": d.get("ParentIndexNumber", 0), "episode": d.get("IndexNumber", 0),
                    "item_id": d["Id"], "file_name": src.get("Path", "").split("/")[-1].split("\\")[-1],
                    "res": tags["res"], "bitrate": src.get("Bitrate", 0),
                    "size": src.get("Size", 0), "v_codec": tags["v_codec"], "a_codec": tags["a_codec"],
                    "hdr": tags["has_hdr"], "dovi": tags["has_dovi"], 
                    "chi": tags["has_chi"], "ass": tags["has_ass"],
                    "score": score, "exempt": is_exempt
                })
            
            # 智能判定“建议清理项” (给最低分的打上标记，前提是分数差距明显，且非豁免项)
            if parsed_items:
                parsed_items.sort(key=lambda x: x["score"], reverse=True)
                top_score = parsed_items[0]["score"]
                for idx, pi in enumerate(parsed_items):
                    # 如果不是最高分，且分数差距 > 10，且不是豁免项，则建议删除
                    if idx > 0 and (top_score - pi["score"] >= 10) and pi["exempt"] == 0:
                        pi["del_mark"] = 1
                    else:
                        pi["del_mark"] = 0
                        
                # 写入数据库
                for pi in parsed_items:
                    conn.cursor().execute('''INSERT INTO dedupe_results 
                        (group_key, tmdb_id, media_type, title, season_num, episode_num, item_id, file_name, 
                         resolution, bitrate, size_bytes, video_codec, audio_codec, has_hdr, has_dovi, 
                         has_chi_sub, has_ass_sub, score, is_recommended_del, is_exempt) 
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (pi['g_key'], pi['tmdb'], pi['mtype'], pi['title'], pi['season'], pi['episode'], 
                         pi['item_id'], pi['file_name'], pi['res'], pi['bitrate'], pi['size'], pi['v_codec'], 
                         pi['a_codec'], pi['hdr'], pi['dovi'], pi['chi'], pi['ass'], pi['score'], 
                         pi['del_mark'], pi['exempt'])
                    )
            conn.commit()
            time.sleep(0.1) # 防止 Emby API QPS 过高
            
        conn.close()
        scan_state["message"] = f"✅ 扫描完成！发现 {scan_state['duplicate_groups']} 组重复项。"
        
    except Exception as e:
        logger.error(f"[去重引擎] 扫描异常: {e}")
        scan_state["message"] = f"❌ 扫描失败: {str(e)}"
    finally:
        time.sleep(2) # 留给前端展示 100% 的时间
        scan_state["is_scanning"] = False

# ==========================================
# 🌐 4. API 接口暴露
# ==========================================
class ScanReq(BaseModel):
    strategy: str = "quality" # quality / size / subs

class DeleteReq(BaseModel):
    item_ids: List[str]

class IgnoreReq(BaseModel):
    group_keys: List[str]

@router.post("/scan")
async def trigger_scan(req: ScanReq, bg_tasks: BackgroundTasks):
    if scan_state["is_scanning"]:
        return {"success": False, "msg": "系统正在扫描中，请勿重复提交"}
    bg_tasks.add_task(run_dedupe_scan, req.strategy)
    return {"success": True, "msg": "🚀 扫描任务已在后台启动！"}

@router.get("/status")
async def get_scan_status():
    return {"success": True, "data": scan_state}

@router.get("/results")
async def get_results():
    rows = query_db("SELECT * FROM dedupe_results ORDER BY group_key, score DESC")
    if not rows: return {"success": True, "data": {}}
    
    # 将平铺的数据组装成按组(Group)分类的手风琴结构
    result_tree = defaultdict(list)
    for r in rows:
        result_tree[r["group_key"]].append(dict(r))
        
    return {"success": True, "data": result_tree}

@router.post("/ignore")
async def ignore_groups(req: IgnoreReq):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for gk in req.group_keys:
            c.execute("INSERT OR IGNORE INTO dedupe_whitelist (group_key) VALUES (?)", (gk,))
            # 同时从当前结果里删掉
            c.execute("DELETE FROM dedupe_results WHERE group_key = ?", (gk,))
        conn.commit()
        conn.close()
        return {"success": True, "msg": "已加入永久白名单"}
    except Exception as e:
        return {"success": False, "msg": str(e)}

@router.post("/delete")
async def delete_items(req: DeleteReq):
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    success_count = 0
    fail_count = 0
    
    for item_id in req.item_ids:
        try:
            url = f"{host}/emby/Items/{item_id}?api_key={key}"
            # 🔥 调用 Emby 物理删除接口
            res = requests.delete(url, timeout=10)
            if res.status_code in [200, 204]:
                success_count += 1
                # 删除成功后，清除数据库缓存
                query_db("DELETE FROM dedupe_results WHERE item_id = ?", (item_id,))
            else:
                fail_count += 1
        except:
            fail_count += 1
            
    return {"success": True, "msg": f"操作完成。成功删除 {success_count} 项，失败 {fail_count} 项 (请检查Emby是否开启删除权限)"}