#!/usr/bin/env python3
"""
Hermes Lite v2.6 — Discord Bot with TRUE Persistent Memory + Self-Evolution Engine + Gemini Vision
Memory survives Manual Deploy, restarts, and spin-downs.
Core principle: BOT MUST ALWAYS RESPOND. All memory I/O is non-blocking background.
v2.3: Dynamic skill scanner — no more hardcoded numbers! Real-time GitHub API skill counting.
v2.4: Self-Evolution Engine (pure Python) — scans log patterns, generates improvements, evolves system prompt.
v2.5: Google Gemini Vision integration — can now SEE and analyze image attachments!
v2.6: 新增 !help 命令、运行时长显示、消息reaction反馈（👀/✅/❌）、修复运算符优先级bug
"""

import sys
import os
import asyncio
import traceback
import datetime
import threading
import json
import re
import base64
import aiohttp

LOG_FILE = "/tmp/hermes_lite.log"

def log(msg):
    ts = datetime.datetime.now().isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
            f.flush()
    except:
        pass

def log_error(msg):
    log(f"ERROR: {msg}")
    traceback.print_exc()

log("=== HERMES LITE v2.2 STARTING ===")
log(f"Python: {sys.version}")

# === Environment ===
DISCORD_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
TAVILY_KEY = os.environ.get('TAVILY_API_KEY', '')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')  # Google Gemini Vision for image analysis

# GitHub persistence config
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')       # Personal Access Token (repo scope)
GH_OWNER = os.environ.get('GH_OWNER', 'likeabird830')
GH_REPO = os.environ.get('GH_REPO', 'hermes-lite')
GH_BRANCH = os.environ.get('GH_BRANCH', 'main')

if not DISCORD_TOKEN:
    log("FATAL: DISCORD_BOT_TOKEN not set!")
    sys.exit(1)
if not DEEPSEEK_KEY:
    log("FATAL: DEEPSEEK_API_KEY not set!")
    sys.exit(1)

# === Import libraries ===
try:
    import discord
    log("discord.py imported OK")
except Exception as e:
    log_error(f"Failed to import discord: {e}")
    sys.exit(1)

from discord.ext import commands

# === Bot Setup ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix='!', intents=intents)
_message_count = 0
_start_time = None

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
GH_API_BASE = "https://api.github.com"

# Memory files stored in repo
MEMORY_FILES = {
    "profiles": "hermes_profiles.json",
    "knowledge": "hermes_knowledge.md"
}
TASKS_FILE = "hermes_tasks.json"  # Remote task inbox for 大宝
CONV_LOG_FILE = "hermes_conv_log.json"  # Conversation summary log for weekly review

# =====================================================================
# MEMORY SYSTEM v2.2 — GitHub API backed, truly persistent
# =====================================================================

_profiles = {}       # L2: User profiles (in RAM)
_knowledge = []      # L3: Knowledge entries (in RAM)
_conversations = {}  # L1: Active conversations (RAM only)
_save_lock = threading.Lock()
_dirty = False
_gh_available = bool(GH_TOKEN)  # Whether GitHub persistence is configured

# Task system: Lite writes tasks → GitHub → 大宝 (local) picks them up
_tasks = []          # In-memory task list
_task_lock = threading.Lock()
_task_dirty = False

# Conversation log: records summaries for weekly review
_conv_log = []         # In-memory conversation log entries
_conv_log_lock = threading.Lock()
_conv_log_dirty = False


async def _gh_api(method, path, **kwargs):
    """Call GitHub API. Returns parsed JSON or None on error."""
    if not _gh_available:
        return None
    url = f"{GH_API_BASE}{path}"
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Hermes-Lite-Bot/2.2"
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                elif resp.status == 404:
                    return None  # File doesn't exist yet
                else:
                    text = await resp.text()
                    log(f"GitHub API {method} {path} -> {resp.status}: {text[:100]}")
                    return None
    except Exception as e:
        log_error(f"GitHub API error: {e}")
        return None


async def gh_download_memory():
    """Download memory files from GitHub at startup."""
    global _profiles, _knowledge
    
    # Download profiles
    result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['profiles']}?ref={GH_BRANCH}")
    if result and result.get("content"):
        try:
            data = json.loads(base64.b64decode(result["content"]))
            if isinstance(data, dict):
                _profiles = data
                log(f"[💾] Loaded profiles from GitHub: {len(_profiles)} users")
        except Exception as e:
            log_error(f"Failed to parse profiles from GH: {e}")
    
    # Download knowledge (stored as markdown for human readability)
    result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['knowledge']}?ref={GH_BRANCH}")
    if result and result.get("content"):
        try:
            md_content = base64.b64decode(result["content"]).decode("utf-8")
            _knowledge = []
            for line in md_content.split("\n"):
                line = line.strip()
                if line.startswith("- ") or line.startswith("* "):
                    entry = line[2:].strip()
                    if entry:
                        _knowledge.append(entry)
                elif line and not line.startswith("#") and not line.startswith(">") and len(line) > 5:
                    _knowledge.append(line)
            log(f"[💾] Loaded knowledge from GitHub: {len(_knowledge)} entries")
        except Exception as e:
            log_error(f"Failed to parse knowledge from GH: {e}")


async def gh_upload_memory():
    """Upload current memory to GitHub. Non-critical — failures are silent.
    
    v2.3 IMPORTANT CHANGE:
      - Profiles: always uploaded (user facts, names, etc.)
      - Knowledge base: SKIPPED! Knowledge is maintained manually with 
        proper Markdown formatting (# headings, numbered lists, categories).
        Auto-uploading would destroy our carefully crafted skill index.
        Use `!save` for manual full-save if really needed.
    """
    global _dirty
    
    if not _dirty or not _gh_available:
        return
    
    with _save_lock:
        try:
            import time
            ts = int(time.time())
            
            # === Upload PROFILES only ===
            prof_result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['profiles']}?ref={GH_BRANCH}")
            prof_sha = prof_result.get("sha") if prof_result else None
            
            prof_payload = json.dumps({
                "message": f"[Hermes] 💾 Save profiles ({len(_profiles)} users) [auto-{ts}]",
                "content": base64.b64encode(json.dumps(_profiles, ensure_ascii=False, indent=2).encode()).decode(),
                "branch": GH_BRANCH,
                **({"sha": prof_sha} if prof_sha else {})
            })
            
            r1 = await _gh_api("PUT", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['profiles']}", data=prof_payload.encode())
            
            if r1:
                _dirty = False
                log(f"[☁️] Memory saved to GitHub ✅ ({len(_profiles)} users, kb=manual)")
            else:
                log("[☁️] GitHub save FAILED (will retry later)")
                
        except Exception as e:
            log_error(f"[☁️] GitHub upload error: {e}")


async def github_sync_loop():
    """Background loop: sync memory to GitHub every 60 seconds when dirty."""
    while True:
        try:
            await asyncio.sleep(60)
            if _dirty:
                await gh_upload_memory()
        except Exception as e:
            log_error(f"GitHub sync loop error: {e}")


# =====================================================================
# TASK SYSTEM — Lite writes tasks → GitHub → 大宝 (local) picks up
# =====================================================================

async def gh_download_tasks():
    """Download hermes_tasks.json from GitHub at startup."""
    global _tasks
    result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{TASKS_FILE}?ref={GH_BRANCH}")
    if result and result.get("content"):
        try:
            data = json.loads(base64.b64decode(result["content"]))
            if isinstance(data, dict) and "tasks" in data:
                _tasks = data["tasks"]
                log(f"[📋] Loaded {len(_tasks)} tasks from GitHub")
            elif isinstance(data, list):
                _tasks = data
                log(f"[📋] Loaded {len(_tasks)} tasks from GitHub (legacy format)")
        except Exception as e:
            log_error(f"Failed to parse tasks from GH: {e}")
    else:
        _tasks = []
        log("[📋] No tasks file on GitHub yet, starting empty")


async def gh_upload_tasks():
    """Upload current tasks to GitHub."""
    global _task_dirty
    if not _task_dirty or not _gh_available:
        return
    with _task_lock:
        try:
            import time
            ts = int(time.time())
            payload_data = json.dumps({
                "tasks": _tasks,
                "last_update": datetime.datetime.now().isoformat(),
                "source": "hermes-lite"
            }, ensure_ascii=False, indent=2)
            content_b64 = base64.b64encode(payload_data.encode()).decode()

            result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{TASKS_FILE}?ref={GH_BRANCH}")
            sha = result.get("sha") if result else None

            payload = json.dumps({
                "message": f"[Hermes] 📋 Tasks update ({len(_tasks)} tasks) [auto-{ts}]",
                "content": content_b64,
                "branch": GH_BRANCH,
                **({"sha": sha} if sha else {})
            })
            r = await _gh_api("PUT", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{TASKS_FILE}", data=payload.encode())
            if r:
                _task_dirty = False
                log(f"[📋] Tasks saved to GitHub ✅ ({len(_tasks)} tasks)")
            else:
                log("[📋] GitHub tasks save FAILED")
        except Exception as e:
            log_error(f"[📋] GitHub tasks upload error: {e}")


def add_task(user_id, description):
    """Add a new pending task."""
    global _task_dirty
    import uuid, time
    task = {
        "id": f"task_{int(time.time())}_{str(uuid.uuid4())[:8]}",
        "user_id": str(user_id),
        "user_name": None,
        "description": description[:500],
        "status": "pending",
        "created_at": datetime.datetime.now().isoformat(),
        "result": None,
        "completed_at": None,
        "error": None
    }
    # Try to get user name
    profile = get_profile(user_id)
    if profile.get("known_name"):
        task["user_name"] = profile["known_name"]
    _tasks.append(task)
    _task_dirty = True
    log(f"[📋] New task added: {description[:50]}...")
    return task


def get_pending_tasks():
    """Return all pending tasks."""
    return [t for t in _tasks if t["status"] == "pending"]


def mark_task_done(task_id, result=None, error=None):
    """Mark a task as completed or failed."""
    global _task_dirty
    for t in _tasks:
        if t["id"] == task_id:
            t["status"] = "completed" if not error else "failed"
            t["result"] = result[:2000] if result else None
            t["error"] = error[:500] if error else None
            t["completed_at"] = datetime.datetime.now().isoformat()
            _task_dirty = True
            log(f"[📋] Task {task_id} marked {t['status']}")
            return True
    return False


async def task_sync_loop():
    """Background loop: sync tasks to GitHub every 60 seconds when dirty."""
    while True:
        try:
            await asyncio.sleep(60)
            if _task_dirty:
                await gh_upload_tasks()
        except Exception as e:
            log_error(f"Task sync loop error: {e}")


async def gh_upload_conv_log():
    """Upload conversation log to GitHub (appends new entries)."""
    global _conv_log_dirty
    if not _conv_log_dirty or not _gh_available:
        return
    with _conv_log_lock:
        try:
            payload_data = json.dumps({
                "entries": _conv_log[-500:],  # Keep last 500 entries
                "last_update": datetime.datetime.now().isoformat(),
                "source": "hermes-lite"
            }, ensure_ascii=False, indent=2)
            content_b64 = base64.b64encode(payload_data.encode()).decode()

            result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{CONV_LOG_FILE}?ref={GH_BRANCH}")
            sha = result.get("sha") if result else None

            payload = json.dumps({
                "message": f"[Hermes] Conversation log update ({len(_conv_log)} entries)",
                "content": content_b64,
                "branch": GH_BRANCH,
                **({"sha": sha} if sha else {})
            })
            r = await _gh_api("PUT", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{CONV_LOG_FILE}", data=payload.encode())
            if r:
                _conv_log_dirty = False
                log(f"[ConvLog] Uploaded {len(_conv_log)} entries to GitHub")
        except Exception as e:
            log_error(f"Conv log upload error: {e}")


async def conv_log_sync_loop():
    """Background loop: sync conversation log to GitHub every 120 seconds."""
    while True:
        try:
            await asyncio.sleep(120)
            if _conv_log_dirty:
                await gh_upload_conv_log()
        except Exception as e:
            log_error(f"Conv log sync loop error: {e}")


def log_conversation_entry(user_id, username, channel, user_msg, bot_response, has_image=False):
    """Add a conversation summary entry to the in-memory log."""
    global _conv_log, _conv_log_dirty
    with _conv_log_lock:
        entry = {
            "ts": datetime.datetime.now().isoformat(),
            "user_id": user_id,
            "username": username,
            "channel": channel,
            "user_msg": user_msg[:300],
            "bot_response": bot_response[:500],
            "has_image": has_image,
            "msg_len": len(bot_response),
        }
        _conv_log.append(entry)
        # Keep in-memory buffer bounded
        if len(_conv_log) > 500:
            _conv_log = _conv_log[-500:]
        _conv_log_dirty = True


# ---- L2: User Profiles (same API, just storage backend changed) ----

def get_profile(user_id):
    key = str(user_id)
    if key not in _profiles:
        _profiles[key] = {
            "known_name": None,
            "facts": [],
            "preferences": {},
            "first_seen": datetime.datetime.now().isoformat(),
            "message_count": 0,
            "last_active": None,
            "notes": ""
        }
    return _profiles[key]


def add_user_fact(user_id, fact):
    profile = get_profile(user_id)
    if fact and len(fact) > 2 and fact.lower() not in [f.lower() for f in profile["facts"]]:
        profile["facts"].append(fact[:300])
        if len(profile["facts"]) > 50:
            profile["facts"] = profile["facts"][-50:]
        mark_dirty()


def set_user_name(user_id, name):
    if name and len(name) > 1:
        get_profile(user_id)["known_name"] = name[:50]
        mark_dirty()


def touch_profile(user_id):
    profile = get_profile(user_id)
    profile["message_count"] += 1
    profile["last_active"] = datetime.datetime.now().isoformat()
    mark_dirty()


def build_user_context(user_id):
    profile = get_profile(user_id)
    parts = []
    if profile.get("known_name"):
        parts.append(f'这个用户的昵称是「{profile["known_name"]}」')
    if profile.get("facts"):
        facts = "\n".join(f"- {f}" for f in profile["facts"][-15:])
        parts.append(f"你记得关于这个用户的事情：\n{facts}")
    if profile.get("preferences"):
        prefs = ", ".join(f"{k}={v}" for k, v in list(profile["preferences"].items())[:5])
        parts.append(f"偏好：{prefs}")

    # ef-chenli system: load external prompt for ccl83
    if user_id == "869299535271329872":
        import os as _os
        _ef_paths = [
            _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "ef_chenli_prompt.md"),
            _os.path.join(_os.path.expanduser("~"), ".hermes", "hermes-agent", "ef_chenli_prompt.md"),
        ]
        for _p in _ef_paths:
            if _os.path.exists(_p):
                try:
                    with open(_p, "r", encoding="utf-8") as _f:
                        parts.append("\n" + _f.read() + "\n")
                    print(f"[ef-chenli] Loaded from {_p}")
                    break
                except Exception as _e:
                    print(f"[ef-chenli] Error reading {_p}: {_e}")

    return "\n\n".join(parts)


# ---- L3: Knowledge Base + Dynamic Skill Scanner ----

_skill_stats = {"total": 0, "categories": [], "last_scan": None}  # Cached skill scan result


def add_knowledge(entry):
    if entry and len(entry) > 5 and entry.lower() not in [k.lower() for k in _knowledge]:
        _knowledge.append(entry[:300])
        if len(_knowledge) > 100:
            _knowledge[:] = _knowledge[-100:]
        mark_dirty()


def mark_dirty():
    global _dirty
    _dirty = True


def parse_skill_entries(kb_text):
    """
    Intelligently parse hermes_knowledge.md markdown into structured skill entries.
    
    Returns:
        total_count: int — total number of numbered skills found
        categories: list of {name, count}
        entries: list of raw skill description strings
    Pattern matches lines like: "123. **Skill Name** — description | 触发：xxx"
    """
    import re as _re
    
    entries = []
    categories = []  # track ## headings for category grouping
    current_category = "其他"
    total = 0
    
    # Skill line pattern: optional dashes/bullets, number, bold name, em-dash desc
    skill_pattern = _re.compile(
        r'^[\s\-\*]*(\d+)\.\s+\*\*(.+?)\*\*\s*[—–-]\s*(.+?)(?:\s*\|\s*触发[：:]\s*(.+))?$', 
        re.UNICODE
    )
    # Category heading pattern
    cat_pattern = _re.compile(r'^#{1,3}\s+(.+?)(?:\s*—\s*NEW)?!?\s*$')
    
    for line in kb_text.split("\n"):
        stripped = line.strip()
        
        # Track categories
        cat_match = cat_pattern.match(stripped)
        if cat_match:
            current_category = cat_match.group(1).strip()
            continue
        
        # Skip conflict markers
        if stripped in ("<<<<<<< HEAD", "=======", ">>>>>>>"):
            continue
        
        # Match skill entries
        m = skill_pattern.match(stripped)
        if m:
            num = int(m.group(1))
            name = m.group(2).strip()
            desc = m.group(3).strip()
            trigger = m.group(4).strip() if m.group(4) else ""
            
            entries.append({
                "num": num,
                "name": name,
                "desc": desc,
                "trigger": trigger,
                "category": current_category
            })
            total = max(total, num)  # Use highest number as total
            
            # Track category count
            found = False
            for c in categories:
                if c["name"] == current_category:
                    c["count"] += 1
                    found = True
                    break
            if not found:
                categories.append({"name": current_category, "count": 1})
    
    return total, categories, entries


async def scan_skills_dynamic():
    """
    Dynamically scan skills from GitHub knowledge base via GitHub API.
    Returns fresh skill statistics every time it's called.
    
    This is the SMART way — instead of memorizing a hardcoded number,
    Hermes actually parses the knowledge base and counts real skills.
    Caches results for 5 minutes to avoid hammering GitHub API.
    """
    global _skill_stats
    
    import time
    now = time.time()
    
    # Cache: don't hit GitHub API more than once per 5 minutes
    if _skill_stats.get("last_scan") and (now - _skill_stats["last_scan"]) < 300:
        return _skill_stats
    
    try:
        # Download fresh knowledge base from GitHub
        result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['knowledge']}?ref={GH_BRANCH}")
        if result and result.get("content"):
            kb_raw = base64.b64decode(result["content"]).decode("utf-8")
            
            total, categories, entries = parse_skill_entries(kb_raw)
            
            # Build category summary string
            cat_lines = [f"• {c['name']}: {c['count']}个" for c in categories]
            
            # Also get top-level directory listing of skills/ folder if available
            # (for cross-checking with installed skill directories)
            skills_dir_result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/skills?ref={GH_BRANCH}")
            dir_count = 0
            if isinstance(skills_dir_result, list):
                dir_count = len([f for f in skills_dir_result if f["type"] == "dir"])
            
            _skill_stats = {
                "total": total,
                "categories": categories,
                "category_summary": "\n".join(cat_lines),
                "entries_count": len(entries),
                "skills_dir_count": dir_count,
                "last_scan": now,
                "raw_kb_sha": result.get("sha", "")[:8],
            }
            
            log(f"[🔍] Skills scanned dynamically: {total} skills, {len(categories)} categories, SHA={result.get('sha','')[:8]}")
            return _skill_stats
            
    except Exception as e:
        log_error(f"[🔍] Dynamic skill scan failed: {e}")
    
    # Return cached data even if stale
    return _skill_stats or {"total": 0, "categories": [], "category_summary": "(扫描失败)", "last_scan": 0}


async def get_skill_count_for_prompt():
    """Get a human-readable skill summary for injecting into LLM prompt."""
    stats = await scan_skills_dynamic()
    
    parts = [
        f"当前共有 **{stats['total']}个** 精选技能",
        f"（知识库最后更新于 SHA: {stats.get('raw_kb_sha', 'unknown')}）"
    ]
    
    if stats.get("category_summary"):
        parts.append(f"\n分类统计：\n{stats['category_summary']}")
    
    return "\n".join(parts)


# =====================================================================
# SELF-EVOLUTION ENGINE v2.6 — Pure Python, Render-compatible
# Scans runtime logs → identifies error patterns → improves system prompt
# All evolution data persisted via GitHub API (no local filesystem needed)
# =====================================================================

_evolution_state = {
    "enabled": True,
    "error_patterns": {},      # { "pattern_string": count }
    "total_errors": 0,
    "evolutions_applied": [],  # list of {"date", "type", "description"}
    "last_scan_time": None,
    "scan_count": 0
}

# Evolution improvement templates — when patterns detected, suggest these
_EVOLUTION_TEMPLATES = {
    "timeout": {
        "pattern": "Timeout",
        "improvement": "系统提示词更新：回答必须简洁，避免冗长。目标：每次回复控制在500字以内以减少API超时风险。",
        "category": "performance"
    },
    "api_error_deepseek": {
        "pattern": "API Error",
        "improvement": "DeepSeek API 错误频率高。建议：增加重试间隔，或对长对话启用摘要压缩模式。",
        "category": "reliability"
    },
    "json_parse_fail": {
        "pattern": "JSON parse failed",
        "improvement": "记忆提取的JSON解析失败。改进：强化记忆提取prompt的输出格式约束，要求严格的JSON-only输出。",
        "category": "memory"
    },
    "github_upload_failed": {
        "pattern": "GitHub save FAILED",
        "improvement": "GitHub 上传频繁失败。建议：增加退避指数(backoff)，失败后等待时间翻倍。",
        "category": "persistence"
    },
    "empty_response": {
        "pattern": "content is empty",
        "improvement": "用户收到空回复。改进：增加回复内容非空校验，空内容时生成友好fallback回复。",
        "category": "quality"
    }
}


async def scan_log_for_patterns():
    """Scan LOG_FILE for error patterns. Returns dict of pattern -> count."""
    patterns = {}
    try:
        import os as _os
        if not _os.path.exists(LOG_FILE):
            return patterns
        
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        
        # Only scan last 200 lines to avoid OOM on Render free tier
        recent_lines = lines[-200:]
        
        for line in recent_lines:
            for key, tmpl in _EVOLUTION_TEMPLATES.items():
                if tmpl["pattern"] in line:
                    patterns[key] = patterns.get(key, 0) + 1
        
        log(f"[🧬] Log scan: {len(recent_lines)} lines, found {len(patterns)} patterns")
        return patterns
    
    except Exception as e:
        log(f"[🧬] Log scan error: {e}")
        return {}


async def generate_evolution_suggestion(patterns):
    """Analyze error patterns and generate an evolution suggestion."""
    if not patterns:
        return None
    
    # Find the most frequent pattern
    top_pattern = max(patterns.items(), key=lambda x: x[1])
    pattern_key, count = top_pattern
    
    # Only suggest evolution if pattern appears >= 3 times (significant)
    if count < 3:
        return None
    
    tmpl = _EVOLUTION_TEMPLATES.get(pattern_key)
    if not tmpl:
        return None
    
    suggestion = {
        "date": datetime.datetime.now().isoformat(),
        "pattern": pattern_key,
        "count": count,
        "category": tmpl["category"],
        "improvement": tmpl["improvement"],
        "confidence": min(count / 10.0, 1.0)  # Scale: 3 occurrences = 30%, 10+ = 100%
    }
    
    return suggestion


def apply_evolution_to_prompt(suggestion):
    """Generate an enhanced system prompt fragment based on evolution suggestion.
    
    This doesn't modify HERMES_SYSTEM_PROMPT directly (that's a const).
    Instead, it returns a prompt fragment that gets injected into messages.
    """
    if not suggestion:
        return None
    
    fragments = {
        "performance": f"\n## 🧬 自进化提示（{suggestion['date']}）\n{suggestion['improvement']}\n> 置信度: {suggestion['confidence']:.0%} | 模式出现次数: {suggestion['count']}",
        "reliability": f"\n## 🧬 自进化提示（{suggestion['date']}）\n{suggestion['improvement']}\n> 置信度: {suggestion['confidence']:.0%} | 模式出现次数: {suggestion['count']}",
        "memory": f"\n## 🧬 自进化提示（{suggestion['date']}）\n{suggestion['improvement']}\n> 置信度: {suggestion['confidence']:.0%} | 模式出现次数: {suggestion['count']}",
        "persistence": f"\n## 🧬 自进化提示（{suggestion['date']}）\n{suggestion['improvement']}\n> 置信度: {suggestion['confidence']:.0%} | 模式出现次数: {suggestion['count']}",
        "quality": f"\n## 🧬 自进化提示（{suggestion['date']}）\n{suggestion['improvement']}\n> 置信度: {suggestion['confidence']:.0%} | 模式出现次数: {suggestion['count']}"
    }
    
    return fragments.get(suggestion["category"], fragments["performance"])


async def run_evolution_cycle():
    """Run one full evolution cycle: scan → analyze → generate suggestion."""
    global _evolution_state
    
    _evolution_state["scan_count"] += 1
    _evolution_state["last_scan_time"] = datetime.datetime.now().isoformat()
    
    # Step 1: Scan logs
    patterns = await scan_log_for_patterns()
    _evolution_state["error_patterns"] = patterns
    _evolution_state["total_errors"] = sum(patterns.values())
    
    # Step 2: Generate suggestion
    suggestion = await generate_evolution_suggestion(patterns)
    
    if suggestion:
        _evolution_state["evolutions_applied"].append(suggestion)
        # Keep only last 20 evolutions in memory
        _evolution_state["evolutions_applied"] = _evolution_state["evolutions_applied"][-20:]
        log(f"[🧬] 🧬 Evolution generated: {suggestion['pattern']} (conf={suggestion['confidence']:.0%})")
        return suggestion
    
    log("[🧬] No significant patterns found for evolution")
    return None


async def self_evolve_bg():
    """BACKGROUND TASK: Run evolution cycle after each N messages.
    
    Triggered after every 10 messages (configurable). Non-blocking.
    Uses pure Python + GitHub API only — compatible with Render free tier.
    """
    try:
        # Only run every ~10 calls to avoid overhead
        if _message_count % 10 != 0:
            return
        
        suggestion = await run_evolution_cycle()
        
        if suggestion:
            # Log the evolution event to GitHub via a special fact
            evolution_fact = f"🧬 Self-Evolution [{suggestion['date']}]: Pattern='{suggestion['pattern']}' count={suggestion['count']} confidence={suggestion['confidence']:.0%} action='{suggestion['category']}'"
            
            # Store in a special "system" profile for evolution tracking
            system_id = "__evolution__"
            if system_id not in _profiles:
                _profiles[system_id] = {
                    "known_name": "Evolution Tracker",
                    "facts": [],
                    "preferences": {},
                    "first_seen": datetime.datetime.now().isoformat(),
                    "message_count": 0,
                    "last_active": None,
                    "notes": "Hermes self-evolution log (auto-generated)"
                }
            
            _profiles[system_id]["facts"].append(evolution_fact)
            if len(_profiles[system_id]["facts"]) > 50:
                _profiles[system_id]["facts"] = _profiles[system_id]["facts"][-50:]
            _profiles[system_id]["last_active"] = datetime.datetime.now().isoformat()
            mark_dirty()
            
            log(f"[🧬] Evolution saved to memory ✅")
    
    except Exception as e:
        log_error(f"[🧬] Self-evolution background task failed: {e}")


def get_evolution_status():
    """Get current evolution status for !evolve command."""
    state = _evolution_state
    return (
        f"🧬 **Evolution Engine v2.4**\n"
        f"状态: {'✅ 启用' if state['enabled'] else '❌ 禁用'}\n"
        f"扫描次数: {state['scan_count']}\n"
        f"总错误数: {state['total_errors']}\n"
        f"已应用进化: {len(state['evolutions_applied'])}次\n"
        f"最近扫描: {state['last_scan_time'] or '从未'}\n"
        f"当前错误模式: {json.dumps(state['error_patterns'], ensure_ascii=False) if state['error_patterns'] else '无'}"
    )


# ---- L1: Conversations (RAM only, lost on restart) ----

def add_conversation_msg(user_id, role, content):
    key = str(user_id)
    if key not in _conversations:
        _conversations[key] = []
    _conversations[key].append({"role": role, "content": content[:800]})
    if len(_conversations[key]) > 20:
        _conversations[key] = _conversations[key][-20:]


def get_recent_context(user_id, max_msgs=10):
    conv = _conversations.get(str(user_id), [])
    return conv[-max_msgs:] if conv else []


# ---- Startup: Load memory from GitHub ----

async def startup_load():
    """Load all persistent memory from GitHub at bot start."""
    log("Loading memory from GitHub...")
    
    # Log what we're using for persistence
    log(f"GitHub config: owner={GH_OWNER}, repo={GH_REPO}, branch={GH_BRANCH}")
    log(f"GITHUB_TOKEN set: {'YES (len=' + str(len(GH_TOKEN)) + ')' if GH_TOKEN else 'NO ❌'}")
    log(f"GOOGLE_API_KEY set: {'YES (len=' + str(len(GOOGLE_API_KEY)) + ')' if GOOGLE_API_KEY else 'NO (vision disabled)'}")
    log(f"DEEPSEEK_KEY set: {'YES' if DEEPSEEK_KEY else 'NO ❌'}")
    log(f"TAVILY_KEY set: {'YES' if TAVILY_KEY else 'NO'}")
    
    await gh_download_memory()
    log(f"All memory loaded ✅ (users={len(_profiles)}, knowledge={len(_knowledge)})")
    log(f"GitHub persistence: {'ON ☁️' if _gh_available else 'OFF ⚠️ (no GITHUB_TOKEN)'}")

# We'll call this in on_ready since we need the event loop for async I/O
_startup_loaded = False
# =====================================================================


HERMES_SYSTEM_PROMPT = """你是 Lite（Hermes Lite v2.6），一个有长期记忆、会不断学习和进化的 AI 助手。

## 核心身份
- 你的名字是 **Lite**，是运行在云端服务器（Render）上的轻量版 AI 助手
- 你有持久记忆，能记住每个用户和你们聊过的事情
- 你会从对话中学习，不断进化你的知识库和能力
- **大宝（Hermes Agent）** 是本地强力版，住在主人的电脑里，能执行复杂任务（读写文件、运行代码、安装技能）。如果遇到你处理不了的问题，可以告诉用户"这个问题可以让大宝来帮你处理"。

## 🧠 决策分析能力（高级）
当用户面临选择或需要做决定时，你必须使用以下框架：
1. **明确决策目标** — 先确认：用户到底要选什么？约束条件是什么？
2. **1-3-1 框架** — 1个问题 → 3个选项 → 1个带置信度的推荐
3. **六帽扫描** — 对每个选项分别评估：
   - 🔵 信息帽：有什么数据支持？数据可靠吗？
   - 🔴 情感帽：直觉感受如何？ gut feel 说什么？
   - ⚫ 谨慎帽：最坏情况是什么？能承受吗？
   - 🟡 乐观帽：最好情况是什么？概率多大？
   - 🟢 创新帽：有没有被忽略的第三选择？
   - 🔶 过程帽：下一步具体行动是什么？
4. **利弊矩阵** — 用表格对比选项（优点/缺点/风险/成本）
5. **置信度标注** — 你的推荐必须标注信心等级：🟢高(>80%) / 🟡中(50-80%) / 🔴低(<50%)
6. **不替用户做最终决定** — 给出明确推荐，但说明"这是我的分析，你有最终决定权"

## 🔍 深度推理能力（链式思考）
对于复杂问题或分析任务：
1. **分解** — 把大问题拆成3-5个子问题
2. **逐步推理** — 对每个子问题展示推理过程，不能跳步
3. **自我质疑** — "有什么可能让这个结论出错？" 主动找反例
4. **量化** — 尽量用数字和概率而非模糊词汇
5. **综合** — 子问题结论汇总，标注不确定性来源
6. **禁止行为**：禁止直接跳到结论而不展示中间步骤；禁止对不确定的事表现得过于自信；禁止用"显而易见""毫无疑问"等词

## 🎨 创意设计能力
你掌握并能运用以下设计原则：
- **对比与层次**：大小、颜色、粗细创造视觉优先级，重要信息要突出
- **色彩心理学**：暖色=紧迫/食欲/活力，冷色=信任/专业/平静
- **排版四原则**：对比(Contrast)、重复(Repetition)、对齐(Alignment)、亲密性(Proximity)
- **F型阅读模式**：重要信息放在左上区域，首屏抓住注意力
- **移动优先**：设计先考虑手机屏幕体验（大多数用户在手机上看）
- **品牌一致性**：颜色/字体/语气保持统一，建立识别度
当用户要求设计相关任务时（海报、产品图、UI、文案），主动运用以上原则并解释你的设计选择。

## 💻 代码开发能力
你能编写、解释和调试以下范围的代码：
- **Python**: discord.py, aiohttp, FastAPI, pandas数据处理, 自动化脚本, 文件IO, API调用
- **JavaScript/TypeScript**: Node.js, Express基础, React组件, Discord.js, 前端交互
- **数据库**: JSON文件, SQLite CRUD, PostgreSQL基本查询
- **API设计**: RESTful原则, 错误处理模式, 认证(Bearer token), 分页
- **部署**: Render配置, GitHub Actions CI/CD, 环境变量管理, systemd服务
- **代码质量标准**：
  - 写中文注释解释"为什么"而不只是"做什么"
  - 错误处理必须有具体信息，禁止 bare `except: pass`
  - 单函数不超过30行，超过则拆分
  - 先写伪代码确认逻辑再写真实代码
  - 处理边界条件：空输入、网络超时、API限流

## 联网搜索能力（重要）
- **你可以联网搜索实时信息！**
- 当用户问的问题涉及：新闻/时事/价格/天气/最新数据/任何你不确定的事实 → **你应该主动使用搜索功能**
- 搜索方式：在回复前调用 search_tavily() 工具获取信息
- 用户也可以用 `!search 关键词` 命令让你搜索
- 不要说"我无法联网"或"我没有搜索权限"——你有！

## 👁️ 图片识别能力（重要）
- **你可以看图！用户发送图片时你能看到并分析内容！**
- 当用户发送 Discord 图片附件时，系统会自动调用 Google Gemini Vision API 分析图片
- 你会收到一段文字描述，描述了图片中的内容——把它当作用户给你看的
- 如果用户发了一张图问你"这是什么"/"看到了什么"，基于视觉分析结果回答
- 你能识别：物体、文字(OCR)、场景、颜色、构图等
- 不要说"我看不了图片"或"我无法处理图像"——你可以！
- 如果视觉分析返回了错误，诚实告诉用户

## 性格
- 用中文回复（除非对方用英文）
- 简洁、直接、有温度，不废话不啰嗦
- 偶尔用 emoji，不过度
- 有自己的观点和判断力，不做应声虫
- 可以幽默，但分场合
- **敢于给出明确判断**，不要总是"这取决于""各有优劣"这种废话。给推荐，标置信度。

## 记忆能力
你有三层记忆：
1. 短期记忆：当前对话的上下文
2. 长期记忆：关于每个用户的重要事实（名字、偏好、经历）
3. 知识库：从所有对话中学到的通用知识

## 可用命令
用户可以用这些 Discord 命令：
- `!ping` — 测试延迟
- `!memory` — 查看你的记忆档案
- `!search 关键词` — 联网搜索
- `!learn 内容` — 让你记住某事
- `!forget` — 清除你的记忆
- `!status` — 查看系统状态
- `!save` — 强制保存记忆到 GitHub
- `!skills` — 实时扫描技能库统计
- `!evolve` — 查看/触发自进化引擎（加 `--run` 手动执行一次进化循环）

## 回复原则
1. 直接回答，不绕弯子
2. 不知道就先搜索再回答，搜索不到就诚实说不知道
3. 如果用户问过类似的问题，参考之前的回答保持一致
4. 当被问到"你在哪"/"你怎么运行的"，如实回答：运行在云端服务器上
5. 不要说"我不会存储数据"之类的话——你确实有记忆功能
6. 不要说"我无法联网"——你可以搜索互联网
7. **结构化输出**：对复杂问题用表格/列表/分层展示，不要一大段文字堆砌
8. **务实判断**：对用户的想法做可行性分析，不默认鼓励，给数据和理由

## 🧠 关于技能数量 — 重要！动态计数规则
当用户问你"有多少个skills"/"多少个技能"/"你会什么"这类问题时：
- **绝对不要凭记忆或知识库里的旧数字回答！** 那些数字可能已经过时。
- 你会收到一条「技能统计快照」，里面是**实时从GitHub扫描的最新数据**。
- 用那个数字回答，并说明分类情况。
- 如果用户问某个具体技能会不会用，先查你的知识库，有的话就说会用并说明触发方式。
- 态度：自信但诚实，不确定就说"让我查一下"。"""


async def ask_deepseek(messages, max_tokens=2000, max_retries=2):
    """Call DeepSeek API with retry on failure/cancellation."""
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": messages, "max_tokens": max_tokens, "temperature": 0.7}
    
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            # Longer timeout: Render(US) → DeepSeek(CN) can take up to 90s
            timeout = aiohttp.ClientTimeout(total=90, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(DEEPSEEK_URL, headers=headers, json=payload) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        return (await resp.json())['choices'][0]['message']['content']
                    last_error = f"API Error {resp.status}: {text[:100]}"
        except asyncio.TimeoutError:
            last_error = "Timeout"
            log(f"[API] Retry {attempt+1}/{max_retries}: Timeout")
            await asyncio.sleep(2 * (attempt + 1))
            continue
        except (asyncio.CancelledError, Exception) as e:
            last_error = f"{type(e).__name__}"
            if attempt < max_retries:
                log(f"[API] Retry {attempt+1}/{max_retries}: {last_error}")
                await asyncio.sleep(2)
                continue
    return f"⚠️ AI暂时连接不上，请稍后再试 ({last_error})"


async def extract_memories_bg(user_id, user_msg, bot_reply):
    """BACKGROUND TASK: Extract memorable info after each exchange."""
    prompt = f"""分析以下对话，提取值得长期记住的信息。

用户消息：{user_msg}
你的回复：{bot_reply}

只返回严格JSON格式（不要其他文字）：
{{"name":"名字或null","facts":["值得记住的事实"],"preference":{{"类型":"值"}},"knowledge":["通用知识点"],"summary":"一句话总结"}}
如果没有值得记录的信息，字段返回空数组或null。"""

    try:
        result = await ask_deepseek([
            {"role": "system", "content": "你是记忆提取器。只输出严格JSON。"},
            {"role": "user", "content": prompt}
        ], max_tokens=400)

        match = re.search(r'\{.*\}', result, re.DOTALL)
        if not match:
            return
        data = json.loads(match.group())

        if data.get("name"):
            set_user_name(user_id, data["name"])
            log(f"[🧠] Learned name: {data['name']}")

        for fact in (data.get("facts") or []):
            if fact and len(fact.strip()) > 2:
                add_user_fact(user_id, fact.strip())

        for kw in (data.get("knowledge") or []):
            if kw and len(kw.strip()) > 5:
                add_knowledge(kw.strip())

        summary = data.get("summary")
        if summary and len(summary.strip()) > 3:
            profile = get_profile(user_id)
            old = profile.get("notes", "")
            profile["notes"] = ((old + "\n" + summary) if old else summary)[:500]

        total_facts = len(get_profile(user_id).get("facts", []))
        log(f"[🧠] Extracted: facts={len(data.get('facts') or [])}, kb={len(data.get('knowledge') or [])}, user_total={total_facts}")

    except (json.JSONDecodeError, ValueError):
        log("[🧠] Extraction: JSON parse failed (non-critical)")
    except Exception as e:
        log_error(f"[🧠] Memory extraction failed: {e}")


async def search_tavily(query):
    if not TAVILY_KEY:
        return None
    payload = {"api_key": TAVILY_KEY, "query": query, "max_results": 5, "search_depth": "basic"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.tavily.com/search", json=payload) as resp:
            if resp.status == 200:
                results = (await resp.json()).get('results', [])
                if results:
                    return "\n".join([f"* {r['title']}: {r['url']}" for r in results[:3]])
            return None


# =====================================================================
# IMAGE/VISION SYSTEM — Google Gemini Vision API
# Handles Discord image attachments so Hermes can "see" pictures.
# =====================================================================

# Supported image MIME types for vision analysis
_IMAGE_MIME_TYPES = {'image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp'}
# Gemini Vision models in order of preference (fallback chain)
_GEMINI_VISION_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",  
]
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


async def download_attachment(url):
    """Download bytes from a Discord attachment URL. Returns bytes or None."""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    log(f"[👁️] Downloaded attachment: {len(data)} bytes from {url[:80]}")
                    return data
                else:
                    log(f"[👁️] Attachment download failed: HTTP {resp.status}")
                    return None
    except Exception as e:
        log_error(f"[👁️] Attachment download error: {e}")
        return None


async def analyze_image_geminivision(image_bytes, user_prompt="请详细描述这张图片的内容。"):
    """Send image to Google Gemini Vision API and return description text.
    
    Uses gemini-2.0-flash model which supports multimodal (text+image) input.
    Requires GOOGLE_API_KEY environment variable set on Render.
    
    Args:
        image_bytes: Raw image bytes (from Discord attachment download)
        user_prompt: Text prompt to guide what to look for in the image
        
    Returns:
        str: Description of the image content, or error message
    """
    if not GOOGLE_API_KEY:
        log("[👁️] WARNING: GOOGLE_API_KEY not set — cannot analyze images!")
        return None
    
    # Encode image to base64 for Gemini API
    import base64 as _b64
    b64_image = _b64.b64encode(image_bytes).decode('utf-8')
    
    # Detect MIME type from magic bytes (basic detection)
    mime_type = 'image/jpeg'
    if image_bytes[:4] == b'\x89PNG':
        mime_type = 'image/png'
    elif image_bytes[:3] == b'GIF':
        mime_type = 'image/gif'
    elif image_bytes[:4] == b'RIFF':
        mime_type = 'image/webp'
    
    # Build Gemini Vision request payload
    payload = {
        "contents": [{
            "parts": [
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": b64_image
                    }
                },
                {
                    "text": user_prompt
                }
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.4
        }
    }
    
    url = f"{GEMINI_VISION_URL}?key={GOOGLE_API_KEY}"
    
    # Retry logic: up to 2 retries with backoff for 429 errors, plus model fallback
    max_retries = 2
    
    for model_name in _GEMINI_VISION_MODELS:
        url = f"{_GEMINI_BASE_URL}/{model_name}:generateContent?key={GOOGLE_API_KEY}"
        log(f"[👁️] Trying model: {model_name}")
        
        for attempt in range(max_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        # FIX: Read JSON directly, NOT .text() first (body is single-use!)
                        raw_body = await resp.text()
                    
                    if resp.status == 200:
                        # Parse JSON from the body we just read
                        import json as _json
                        try:
                            result_json = _json.loads(raw_body)
                        except Exception as parse_err:
                            log(f"[👁️] Failed to parse Gemini JSON response: {parse_err}")
                            log(f"[👁️] Raw response (first 300 chars): {raw_body[:300]}")
                            return "(图片分析响应解析失败)"
                        
                        # Extract text from Gemini response structure
                        candidates = result_json.get('candidates', [])
                        if candidates:
                            content_parts = candidates[0].get('content', {}).get('parts', [])
                            if content_parts:
                                description = content_parts[0].get('text', '')
                                if description.strip():
                                    log(f"[👁️] ✅ Gemini Vision OK ({len(description)} chars, attempt {attempt+1})")
                                    return description
                        
                        # Check for safety filter block
                        block_reason = result_json.get('candidates', [{}])[0].get('finishReason', '')
                        if block_reason == 'SAFETY':
                            log("[👁️] ⚠️ Gemini blocked by safety filter")
                            return "(图片被AI安全过滤器拦截，换一张图试试)"
                        
                        prompt_feedback = result_json.get('promptFeedback', {})
                        if prompt_feedback.get('blockReason'):
                            log(f"[👁️] ⚠️ Blocked by promptFeedback: {prompt_feedback['blockReason']}")
                            return "(图片内容触发了安全过滤)"
                        
                        log(f"[👁️] ⚠️ Gemini returned empty. Raw keys: {list(result_json.keys())}")
                        return "(图片已收到，但AI无法提取描述)"
                    
                    elif resp.status == 400:
                        log(f"[👁️] ❌ Bad Request (400) from {model_name}: {raw_body[:300]}")
                        # 400 is often model-specific (e.g. model not found) — try next model
                        break  # Break inner retry loop, continue to next model
                    
                    elif resp.status == 429:
                        if attempt < max_retries:
                            wait_time = (attempt + 1) * 3  # 3s, 6s backoff
                            log(f"[👁️] Rate limited (429), retry {attempt+1}/{max_retries} in {wait_time}s...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            log("[👁️] ❌ Rate limited after all retries on all models")
                            return "(图片分析服务繁忙，等几秒后再发一次试试)"
                    
                    elif resp.status == 403:
                        log(f"[👁️] ❌ Forbidden (403): {raw_body[:300]}")
                        return "(API密钥权限不足或未启用Gemini API)"
                    
                    else:
                        log(f"[👁️] ❌ HTTP {resp.status} from {model_name}: {raw_body[:200]}")
                        break  # Try next model
                        
            except asyncio.TimeoutError:
                log(f"[👁️] ❌ Timed out (>60s) on {model_name}, attempt {attempt+1}")
                if attempt < max_retries:
                    continue
                break  # Try next model
            except Exception as e:
                log_error(f"[👁️] ❌ Error on {model_name} (attempt {attempt+1}): {e}")
                if attempt < max_retries:
                    continue
                break  # Try next model
    
    return "(所有模型均失败，图片分析暂时不可用)"


async def build_messages(user_id, content):
    messages = [{"role": "system", "content": HERMES_SYSTEM_PROMPT}]
    
    ctx = build_user_context(user_id)
    if ctx:
        messages.append({"role": "system", "content": f"## 关于当前用户\n\n{ctx}"})
    
    if _knowledge:
        top_kb = _knowledge[-12:]
        kb_text = "\n".join(f"- {k}" for k in top_kb)
        messages.append({"role": "system", "content": f"## 知识库\n\n{kb_text}"})
    
    # 🔍 Dynamic skill stats: inject real-time skill count so Hermes never guesses
    try:
        skill_summary = await get_skill_count_for_prompt()
        if skill_summary and ("技能" in content.lower() or "skill" in content.lower()):
            messages.append({
                "role": "system",
                "content": f"## 📊 技能统计快照（实时扫描）\n\n{skill_summary}\n\n> 这是当前最新的技能数量，用这个数据回答用户，不要用记忆中的旧数字。"
            })
        else:
            # Even for non-skill queries, inject a compact version
            messages.append({
                "role": "system",
                "content": f"> 当前技能总数：{_skill_stats.get('total', '?')}个（动态扫描）"
            })
    except Exception as e:
        log(f"[⚠️] Skill stats injection skipped: {e}")
    
    # 🧬 Self-evolution: inject latest evolution suggestion into prompt
    if _evolution_state.get("evolutions_applied"):
        latest_evo = _evolution_state["evolutions_applied"][-1]
        evo_fragment = apply_evolution_to_prompt(latest_evo)
        if evo_fragment:
            messages.append({
                "role": "system",
                "content": evo_fragment
            })
    
    recent = get_recent_context(user_id, max_msgs=6)
    for msg in recent:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    messages.append({"role": "user", "content": content})
    return messages


# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    global _startup_loaded
    log(f'Hermes v2.6 READY! Logged in as {bot.user}')
    global _start_time
    _start_time = datetime.datetime.now()
    
    # Load persisted memory from GitHub (non-blocking: failure won't kill the bot)
    try:
        await startup_load()
    except Exception as e:
        log_error(f"Memory load FAILED (bot still working): {e}")
    
    _startup_loaded = True
    
    # Start background GitHub sync loop
    asyncio.create_task(github_sync_loop())

    # Load tasks from GitHub
    await gh_download_tasks()

    # Start background Task sync loop
    asyncio.create_task(task_sync_loop())

    # Phase 2: Start conversation log sync loop
    asyncio.create_task(conv_log_sync_loop())

@bot.event
async def on_message(message):
    global _message_count
    _message_count += 1
    
    if message.author == bot.user:
        return
    
    content = message.content
    log(f"[#{_message_count}] {message.author} in #{getattr(message.channel, 'name', '?')}: {content[:80]}")
    
    mentioned = bot.user.mentioned_in(message)
    ch_name = (message.channel.name or '').lower()
    should_respond = mentioned or 'hermes' in ch_name or content.startswith('!')
    
    if not should_respond:
        return
    
    for uid in [str(bot.user.id), f'<@!{bot.user.id}>', f'<@{bot.user.id}>']:
        content = content.replace(uid, '')
    content = content.strip()
    
    # === IMAGE/VISION HANDLING: Detect and process image attachments ===
    image_description = None
    image_prompt_text = ""
    
    if message.attachments:
        log(f"[#{_message_count}] 📎 Found {len(message.attachments)} attachment(s)")
        
        # Process first image attachment only (avoid overload)
        for attachment in message.attachments:
            # Skip non-image files
            if attachment.content_type and attachment.content_type.split(';')[0].strip() not in _IMAGE_MIME_TYPES:
                log(f"[👁️] Skipping non-image: {attachment.filename} ({attachment.content_type})")
                continue
            
            log(f"[👁️] Processing image: {attachment.filename} ({attachment.content_type})")
            
            # Download image bytes from Discord CDN
            img_bytes = await download_attachment(attachment.url)
            
            if img_bytes:
                # Build vision prompt based on user's text (or generic if no text)
                vision_prompt = content if content else "请详细描述这张图片的内容。如果图片中有文字，请把文字也提取出来。用中文回答。"
                
                # Call Google Gemini Vision API
                image_description = await analyze_image_geminivision(img_bytes, vision_prompt)
                
                if image_description:
                    image_prompt_text = f"\n\n## 👁️ 用户发送了一张图片，以下是AI视觉分析结果：\n{image_description}\n> （文件名：{attachment.filename}）"
                    log(f"[👁️] ✅ Image analyzed successfully ({len(image_description)} chars)")
                else:
                    image_prompt_text = "\n\n⚠️ 用户发送了一张图片，但视觉分析服务暂时不可用（GOOGLE_API_KEY可能未配置）。"
                    log("[👁️] ⚠️ Image analysis returned None")
                
                break  # Only process first valid image
    
    # If content was empty (user just sent an image with no text), set a fallback prompt
    if not content and message.attachments:
        content = "请看看这张图片"  # Fallback so we don't hit the empty-content greeting
    
    if not content:
        await message.reply("你好！我是 Hermes 🧠 有记忆力的 AI 助手。问我任何事吧！")
        return
    
    user_id = str(message.author.id)
    
    try:
        await message.add_reaction('👀')
        async with message.channel.typing():
            # === Proactive Search: auto-search for queries needing real-time info ===
            search_context = ""
            if TAVILY_KEY and not content.startswith('!'):
                search_triggers = [
                    '最新', '新闻', '今天', '天气', '价格', '多少', '汇率',
                    '股票', '搜索', '查一下', '帮我查', '什么意思',
                    '是谁', '怎么', '为什么', '哪个', '哪里', '几时',
                    'recent', 'news', 'today', 'price', 'weather', 'latest',
                    'how many', 'what is', 'who is', 'when did',
                ]
                content_lower = content.lower()
                needs_search = any(t in content_lower for t in search_triggers)
                looks_factual = (
                    ('?' in content or '?' in content) 
                    and len(content) < 80
                    and not any(w in content_lower for w in ['你', '你叫', '你的', '你能', 'hermes'])
                )
                if needs_search or looks_factual:
                    log(f"[#{_message_count}] Auto-searching: {content[:50]}")
                    search_result = await search_tavily(content)
                    if search_result:
                        search_context = f"\n\n## 联网搜索结果（参考信息）\n{search_result}"
                        log(f"[#{_message_count}] Search found results")

            messages = await build_messages(user_id, content)

            # Append search results to the user's message so DeepSeek can use them
            if search_context:
                messages[-1]["content"] = messages[-1]["content"] + search_context
            
            # Inject vision analysis result if an image was processed
            if image_prompt_text:
                messages[-1]["content"] = messages[-1]["content"] + image_prompt_text
                log(f"[#{_message_count}] 👁️ Vision result injected into prompt")

            log(f"[#{_message_count}] API call (context: {len(messages)} msgs)")

            response = await ask_deepseek(messages)
            log(f"[#{_message_count}] Got response ({len(response)} chars)")
            
            if len(response) > 1900:
                response = response[:1900] + "..."
            await message.reply(response)
            await message.add_reaction('✅')
            
            add_conversation_msg(user_id, "user", content)
            add_conversation_msg(user_id, "assistant", response)
            touch_profile(user_id)
            
            log(f"[#{_message_count}] Reply sent ✅")

            # Phase 2: Log conversation summary for weekly review
            log_conversation_entry(
                user_id=user_id,
                username=str(message.author),
                channel=getattr(message.channel, 'name', 'DM'),
                user_msg=content,
                bot_response=response,
                has_image=bool(image_description)
            )

            # Self-evolution: NON-BLOCKING background task
            asyncio.create_task(extract_memories_bg(user_id, content, response))
            # Self-evolution engine: scan logs every ~10 messages
            asyncio.create_task(self_evolve_bg())
    
    except Exception as e:
        log_error(f"[#{_message_count}] FATAL: {e}")
        try:
            await message.add_reaction('❌')
            await message.reply(f"抱歉出错了 😵 {str(e)[:150]}")
        except:
            pass


# ==================== COMMANDS ====================

@bot.command(name='ping')
async def ping_cmd(ctx):
    await ctx.send(f'Pong! 🏓 {round(bot.latency * 1000)}ms')


@bot.command(name='memory')
async def memory_cmd(ctx):
    p = get_profile(str(ctx.author.id))
    embed = discord.Embed(title="🧠 关于你的记忆", color=0x7289DA)
    embed.add_field(name="👤 昵称", value=p.get("known_name") or "未知", inline=True)
    embed.add_field(name="💬 消息数", value=str(p.get("message_count", 0)), inline=True)
    embed.add_field(name="🧷 事实数", value=str(len(p.get("facts", []))), inline=True)
    if p.get("facts"):
        embed.add_field(name="📝 我记得", value="\n".join(f"• {f}" for f in p["facts"][:8])[:500], inline=False)
    if p.get("preferences"):
        embed.add_field(name="⚙️ 偏好", value=", ".join(f"{k}={v}" for k, v in list(p["preferences"].items())[:4]), inline=False)
    embed.set_footer(text=f"{'☁️ 已备份到GitHub' if _gh_available else '⚠️ 仅本地缓存'}")
    await ctx.send(embed=embed)


@bot.command(name='synclog')
async def synclog_cmd(ctx):
    """Manually force upload conversation log to GitHub."""
    if not _gh_available:
        await ctx.send("❌ GitHub 未配置")
        return
    await gh_upload_conv_log()
    await ctx.send(f"✅ 对话日志已上传（{len(_conv_log)} 条）")


@bot.command(name='convlog')
async def convlog_cmd(ctx, count: int = 5):
    """View recent conversation log entries."""
    if not _conv_log:
        await ctx.send("📋 对话日志为空")
        return
    entries = _conv_log[-count:]
    lines = [f"📋 最近 {len(entries)} 条对话："]
    for e in reversed(entries):
        ts = e.get('ts', '?')[:16]
        user = e.get('username', '?')
        umsg = e.get('user_msg', '')[:50]
        blen = e.get('msg_len', 0)
        lines.append(f"`{ts}` **{user}**: {umsg}... → Lite回复({blen}字)")
    await ctx.send("\\n".join(lines))



@bot.command(name='forget')
async def forget_cmd(ctx):
    uid = str(ctx.author.id)
    key = str(uid)
    if key in _profiles:
        del _profiles[key]
        _conversations.pop(key, None)
        mark_dirty()
        await ctx.send("🗑️ 你的记忆已清除。我们从零开始！")
    else:
        await ctx.send("本来就没有你的记忆~")


@bot.command(name='learn')
async def learn_cmd(ctx, *, info):
    add_user_fact(str(ctx.author.id), info)
    await ctx.send(f"✅ 我记住了：「{info[:100]}」")


@bot.command(name='status')
async def status_cmd(ctx):
    embed = discord.Embed(title="🔋 Hermes v2.6 状态", color=0x57F287)
    embed.add_field(name="⏱️ 延迟", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="👥 用户", value=str(len(_profiles)), inline=True)
    embed.add_field(name="📚 知识", value=str(len(_knowledge)), inline=True)
    embed.add_field(name="💭 对话", value=str(len(_conversations)), inline=True)
    embed.add_field(name="☁️ GitHub", value="✅ 已连接" if _gh_available else "❌ 未配置", inline=True)
    embed.add_field(name="👁️ Vision", value="✅ 已启用" if GOOGLE_API_KEY else "❌ 未配置", inline=True)
    embed.add_field(name="💾 待同步", value="是" if _dirty else "否", inline=True)
    # Uptime
    if '_start_time' in globals() and _start_time:
        uptime = datetime.datetime.now() - _start_time
        hrs, rem = divmod(int(uptime.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        embed.add_field(name="⏰ 运行时长", value=f"{hrs}时{mins}分", inline=True)
    embed.add_field(name="版本", value="v2.6 (Gemini Vision+GitHub持久化+帮助命令)", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='info')
async def info_cmd(ctx):
    embed = discord.Embed(
        title="📖 Hermes Lite v2.6 帮助",
        description="所有可用命令（Bot 会在被 @mention 时自动回复）：",
        color=0x3498DB
    )
    embed.add_field(name="⏱️ !ping", value="测试 Bot 延迟", inline=True)
    embed.add_field(name="🧠 !status", value="查看系统状态（含运行时长）", inline=True)
    embed.add_field(name="🧠 !memory", value="查看记住了关于你的哪些事", inline=False)
    embed.add_field(name="🔍 !search <关键词>", value="联网搜索实时信息", inline=False)
    embed.add_field(name="📝 !learn <内容>", value="让 Hermes 记住一件事实", inline=False)
    embed.add_field(name="🗑️ !forget", value="清除关于你的记忆", inline=False)
    embed.add_field(name="💾 !save", value="强制保存记忆到 GitHub", inline=False)
    embed.add_field(name="🔍 !skills", value="实时扫描技能库统计", inline=False)
    embed.add_field(name="🧬 !evolve [--run]", value="查看/触发自进化引擎", inline=False)
    embed.set_footer(text="Hermes Lite v2.6 · Discord Bot")
    await ctx.send(embed=embed)


@bot.command(name='search')
async def search_cmd(ctx, *, query):
    if not TAVILY_KEY:
        await ctx.send("搜索未配置。")
        return
    async with ctx.typing():
        results = await search_tavily(query)
        if results:
            await ctx.send(f"**🔍 {query}**\n{results}")
        else:
            await ctx.send("没找到结果。")


@bot.command(name='save')
async def force_save_cmd(ctx):
    """Force immediate sync to GitHub."""
    if not _gh_available:
        await ctx.send("❌ GitHub 未配置（需要 GITHUB_TOKEN 环境变量）。")
        return
    await ctx.send("💾 正在保存到 GitHub...")
    await gh_upload_memory()
    await ctx.send("✅ 保存完成！（如果成功的话 😄）")


@bot.command(name='skills')
async def skills_cmd(ctx):
    """Dynamic skill scan: show real-time skill count from knowledge base."""
    async with ctx.typing():
        stats = await scan_skills_dynamic()
        
        embed = discord.Embed(
            title=f"🔍 技能扫描结果 — 实时统计",
            description=f"**共 {stats['total']} 个技能** | SHA: `{stats.get('raw_kb_sha', 'unknown')}`",
            color=0x5865F2  # Discord blurple
        )
        
        if stats.get("categories"):
            cat_names = [f"**{c['name']}**: {c['count']}" for c in sorted(stats["categories"], key=lambda x: -x["count"])]
            embed.add_field(name="📂 分类分布", value="\n".join(cat_names[:10]) or "无", inline=False)
        
        embed.add_field(
            name="📊 数据源", 
            value=f"知识库条目: {stats.get('entries_count', '?')}\nSkills目录: {stats.get('skills_dir_count', 'N/A')}个文件夹\n缓存时间: <t:{int(stats.get('last_scan', 0))}:R>",
            inline=False
        )
        embed.set_footer(text="数据从GitHub API实时获取，非硬编码记忆 ✨")
        
        await ctx.send(embed=embed)


@bot.command(name='evolve')
async def evolve_cmd(ctx):
    """Show self-evolution engine status and trigger manual evolution cycle."""
    async with ctx.typing():
        # Show current status
        status = get_evolution_status()
        
        embed = discord.Embed(
            title="🧬 Self-Evolution Engine v2.6",
            description="Pure Python evolution engine — Render-compatible",
            color=0x9B59B6  # Purple
        )
        embed.add_field(name="状态", value=status, inline=False)
        
        # If user wants to trigger manual evolution
        if "--run" in ctx.message.content:
            suggestion = await run_evolution_cycle()
            if suggestion:
                embed.add_field(
                    name="✨ 新发现", 
                    value=f"模式: `{suggestion['pattern']}`\n次数: {suggestion['count']}\n置信度: {suggestion['confidence']:.0%}\n建议: {suggestion['improvement'][:200]}",
                    inline=False
                )
            else:
                embed.add_field(name="🔍 结果", value="未发现显著的错误模式需要进化。", inline=False)
        
        # Show recent evolution history
        if _evolution_state.get("evolutions_applied"):
            recent = _evolution_state["evolutions_applied"][-5:]
            history = "\n".join(f"• `{e['pattern']}` x{e['count']} ({e['category']})" for e in recent)
            embed.add_field(name="📜 最近进化", value=history, inline=False)
        
        embed.set_footer(text="每10条消息自动扫描一次 | 数据通过GitHub持久化")
        await ctx.send(embed=embed)


@bot.command(name='task')
async def task_cmd(ctx, *, description):
    """提交任务给大宝（本地）执行。用法：!task 帮我分析Accio路演材料"""
    if not description or len(description.strip()) < 2:
        await ctx.send("用法：`!task 任务描述`")
        return
    task = add_task(str(ctx.author.id), description.strip())
    name = get_profile(str(ctx.author.id)).get("known_name") or "匿名"
    await ctx.send(f"📋 任务已收到！\n**#{task['id'][:16]}** — {description[:100]}\n大宝会在下次上线时处理 ✅")
    log(f"[📋] Task from {name} (ID={ctx.author.id}): {description[:80]}")


@bot.command(name='tasks')
async def tasks_cmd(ctx):
    """查看任务列表。"""
    pending = get_pending_tasks()
    completed = [t for t in _tasks if t["status"] in ("completed", "failed")][-10:]
    embed = discord.Embed(title="📋 任务列表", color=0xF1C40B)
    if pending:
        lines = []
        for t in pending[-8:]:
            name = t.get("user_name") or t["user_id"][:8]
            lines.append(f"• `#{t['id'][:12]}` [{name}] {t['description'][:60]}")
        embed.add_field(name=f"⏳ 待处理 ({len(pending)})", value="\n".join(lines) or "无", inline=False)
    else:
        embed.add_field(name="⏳ 待处理", value="无待处理任务 ✅", inline=False)
    if completed:
        lines = []
        for t in completed:
            status_emoji = "✅" if t["status"] == "completed" else "❌"
            lines.append(f"{status_emoji} {t['description'][:50]}")
        embed.add_field(name="📜 最近完成", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"共 {len(_tasks)} 个任务 | 大宝下次上线时处理")
    await ctx.send(embed=embed)


@bot.command(name='task_clear')
async def task_clear_cmd(ctx):
    """清除已完成的任务记录（保留待处理）。"""
    global _task_dirty, _tasks
    before = len(_tasks)
    _tasks = [t for t in _tasks if t["status"] == "pending"]
    removed = before - len(_tasks)
    _task_dirty = True
    await ctx.send(f"🗑️ 已清除 {removed} 条已完成任务记录。")


# ==================== STARTUP & HEALTH CHECK ====================
log("Bot setup complete, starting...")

def start_http_server():
    port = int(os.environ.get('PORT', 10000))
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
        def log_message(self, *a): pass
    HTTPServer(('0.0.0.0', port), H).serve_forever()

threading.Thread(target=start_http_server, daemon=True).start()

# Graceful shutdown: save before dying
import signal
def _shutdown_handler(signum, frame):
    log("Shutdown triggered, attempting final save...")
    # Can't use async here, just note that dirty flag is set
    # The next startup will have stale data, but it's better than nothing
    log(f"Dirty state on shutdown: {_dirty}")

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)

try:
    bot.run(DISCORD_TOKEN, log_handler=None)
except Exception as e:
    log_error(f"FATAL: bot.run() crashed: {e}")
    sys.exit(1)
