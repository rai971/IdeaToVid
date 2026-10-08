#!/usr/bin/env python3
"""The Finance Brief: local-first research and reel approval workbench."""
from __future__ import annotations

import json
import html
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL_PACKAGES = ROOT / "work" / "pydeps"
if LOCAL_PACKAGES.is_dir():
    sys.path.insert(0, str(LOCAL_PACKAGES))
DB_PATH = ROOT / "finance_brief.db"
ASSET_DIR = ROOT / "assets"
ASSET_DIR.mkdir(exist_ok=True)
PORT = int(os.environ.get("PORT", "8787"))


def load_env_file():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()
def google_news_feed(name, query, topic):
    params = urllib.parse.urlencode({"q": f"{query} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return {"name": name, "url": "https://news.google.com/rss/search?" + params, "topic": topic}


FEEDS = [
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "topic": "Economy & rates"},
    {"name": "U.S. Bureau of Labor Statistics", "url": "https://www.bls.gov/feed/bls_latest.rss", "topic": "Jobs & inflation"},
    {"name": "U.S. SEC", "url": "https://www.sec.gov/news/pressreleases.rss", "topic": "Markets & companies"},
    google_news_feed("U.S. news search · economy", "US economy OR inflation OR jobs OR wages OR tariffs OR gas prices", "What is moving the economy"),
    google_news_feed("U.S. news search · household money", "US mortgage rates OR rent OR groceries OR credit card debt OR student loans OR healthcare costs", "Everyday money pressures"),
    google_news_feed("U.S. news search · business", "US business earnings OR layoffs OR retail sales OR banks OR oil prices OR housing market", "Business & consumers"),
    google_news_feed("U.S. news search · markets & policy", "US markets OR Federal Reserve OR Treasury OR taxes OR trade policy", "Markets & policy"),
    {"name": "Google Trends · United States", "url": "https://trends.google.com/trending/rss?geo=US", "topic": "Search-interest signals"},
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, source TEXT NOT NULL,
        url TEXT NOT NULL, summary TEXT DEFAULT '', topic TEXT DEFAULT 'Economy',
        published TEXT DEFAULT '', created TEXT NOT NULL, script TEXT DEFAULT '',
        caption TEXT DEFAULT '', status TEXT DEFAULT 'research', notes TEXT DEFAULT ''
    )""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(stories)")}
    for name in ("audio_file", "clip_file", "clip_url", "clip_author", "clip_page", "render_file", "visual_plan"):
        if name not in columns:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {name} TEXT DEFAULT ''")
    for name in ("fb_publish_state", "fb_video_id", "fb_upload_url", "fb_permalink", "fb_published_at"):
        if name not in columns:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {name} TEXT DEFAULT ''")
    if "visual_mode" not in columns:
        conn.execute("ALTER TABLE stories ADD COLUMN visual_mode TEXT DEFAULT ''")
    conn.execute("CREATE TABLE IF NOT EXISTS media_search_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS story_clips (
        story_id INTEGER NOT NULL, pixabay_id TEXT NOT NULL, position INTEGER NOT NULL,
        clip_file TEXT NOT NULL, clip_url TEXT NOT NULL, clip_author TEXT DEFAULT '', clip_page TEXT DEFAULT '',
        source_duration REAL DEFAULT 0, clip_tags TEXT DEFAULT '', search_query TEXT DEFAULT '',
        PRIMARY KEY(story_id, pixabay_id), UNIQUE(story_id, position)
    )""")
    clip_columns = {r[1] for r in conn.execute("PRAGMA table_info(story_clips)")}
    for name, definition in (("source_duration", "REAL DEFAULT 0"), ("clip_tags", "TEXT DEFAULT ''"), ("search_query", "TEXT DEFAULT ''")):
        if name not in clip_columns:
            conn.execute(f"ALTER TABLE story_clips ADD COLUMN {name} {definition}")
    # Preserve clips selected before multi-clip support was added.
    for story in conn.execute("SELECT id,clip_file,clip_url,clip_author,clip_page FROM stories WHERE COALESCE(clip_file,'')!=''"):
        conn.execute("INSERT OR IGNORE INTO story_clips(story_id,pixabay_id,position,clip_file,clip_url,clip_author,clip_page) VALUES(?,?,?,?,?,?,?)",
                     (story[0], f"legacy-{story[0]}", 0, story[1], story[2] or '', story[3] or '', story[4] or ''))
    conn.commit()
    return conn


def row_dict(row):
    return dict(row) if row else None


def ffmpeg_path():
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def kokoro_python():
    candidates = [os.environ.get("KOKORO_PYTHON"), shutil.which("python3.12"),
                  str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                version = subprocess.run([candidate, "-c", "import sys; print(sys.version_info >= (3, 12))"],
                                         capture_output=True, text=True, timeout=8)
                if version.returncode == 0 and version.stdout.strip() == "True":
                    return candidate
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def kokoro_ready():
    return bool(kokoro_python() and (ROOT / "work/kokoro_pydeps/kokoro").is_dir()
                and (ROOT / "work/kokoro_cache/hub").is_dir())


def media_duration(path, ffmpeg):
    result = subprocess.run([ffmpeg, "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError("Could not read the narration duration.")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def srt_time(seconds):
    milliseconds = int((seconds % 1) * 1000)
    whole = int(seconds)
    hours, whole = divmod(whole, 3600)
    minutes, whole = divmod(whole, 60)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


def make_captions(script, seconds):
    words = re.sub(r"[{}<>]", "", safe_text(script)).split()
    if not words:
        return ""
    groups, group = [], []
    for word in words:
        group.append(word)
        if len(group) >= 7 or len(" ".join(group)) > 38:
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    weights = [max(1, len(" ".join(g))) for g in groups]
    total = sum(weights)
    cursor = 0.0
    blocks = []
    for index, (group, weight) in enumerate(zip(groups, weights), 1):
        end = seconds * (sum(weights[:index]) / total)
        blocks.extend([str(index), f"{srt_time(cursor)} --> {srt_time(end)}", "{\\an2}" + " ".join(group), ""])
        cursor = end
    return "\n".join(blocks)


def video_query_candidates(raw_query):
    """Try the supplied text first, then short visual subjects for stock footage."""
    primary = safe_text(raw_query)[:100].strip()
    candidates = [primary] if primary else []
    lower = primary.casefold()
    topic_fallbacks = [
        (r"\b(iran|middle east|war|strike|missile|military|conflict|ukraine)\b", ["oil refinery", "military aircraft", "city skyline"]),
        (r"\b(oil|gasoline|gas prices|crude|opec|energy)\b", ["oil refinery", "gas station", "oil pump"]),
        (r"\b(stock|stocks|market|nasdaq|dow|wall street|trading)\b", ["stock market", "business office", "city financial district"]),
        (r"\b(inflation|prices|groceries|cost of living)\b", ["grocery shopping", "supermarket", "shopping cart"]),
        (r"\b(jobs|employment|layoff|unemployment|workers)\b", ["office workers", "factory workers", "job interview"]),
        (r"\b(fed|federal reserve|interest rate|mortgage|bank)\b", ["bank building", "business meeting", "city skyline"]),
        (r"\b(housing|home sales|rent|real estate)\b", ["house for sale", "apartment building", "construction"]),
    ]
    for pattern, fallbacks in topic_fallbacks:
        if re.search(pattern, lower):
            candidates.extend(fallbacks)
            break
    stop_words = {"the", "and", "for", "with", "from", "that", "this", "what", "when", "where", "will", "would", "could", "should", "about", "after", "before", "over", "under", "into", "amid", "plan", "plans", "says", "said", "report", "reports", "news", "today", "is", "are", "to", "a", "an", "in", "on", "of", "by", "as", "at", "be", "it", "its", "he", "she", "they", "his", "her", "their", "how", "why"}
    keywords = [w for w in re.findall(r"[a-z][a-z'-]{2,}", lower) if w not in stop_words]
    if keywords:
        candidates.append(" ".join(keywords[:3]))
    candidates.extend(["business", "finance economy"])
    unique = []
    seen = set()
    for item in candidates:
        key = item.casefold().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(item[:100])
    return unique[:5]


def configured_ai_provider():
    """Choose an explicitly configured provider or auto-detect available credentials."""
    choice = os.environ.get("AI_PROVIDER", "auto").strip().lower()
    if choice in ("none", "off", "disabled"):
        return ""
    if choice in ("openai", "gemini"):
        key_name = "OPENAI_API_KEY" if choice == "openai" else "GEMINI_API_KEY"
        return choice if os.environ.get(key_name) else ""
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return ""


def configured_ai_model(provider=None):
    provider = provider or configured_ai_provider()
    if provider == "gemini":
        return os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    return os.environ.get("OPENAI_MODEL", "gpt-5-mini")


def save_local_settings(updates):
    """Persist selected app settings locally without ever echoing credential values."""
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    written = set()
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            if key not in written:
                output.append(f"{key}={updates[key]}")
                written.add(key)
            else:
                output.append(f"# duplicate {key} entry removed by app settings")
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in written:
            output.append(f"{key}={value}")
        os.environ[key] = value
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def meta_configured():
    return bool(os.environ.get("META_PAGE_ID") and os.environ.get("META_PAGE_ACCESS_TOKEN"))


def meta_graph_post(path, fields, token):
    version = os.environ.get("META_GRAPH_API_VERSION", "v26.0").strip()
    url = f"https://graph.facebook.com/{version}/{path.lstrip('/')}"
    body = urllib.parse.urlencode({**fields, "access_token": token}).encode()
    request = urllib.request.Request(url, data=body,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(4096))
            detail = safe_text(payload.get("error", {}).get("message", ""))
        except Exception:
            detail = ""
        raise RuntimeError(f"Meta API returned HTTP {exc.code}" + (f": {detail[:220]}" if detail else ".")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Could not reach Meta. Check the internet connection and try again.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Meta returned an unexpected response.")
    return data


def meta_upload_reel(upload_url, token, video_path):
    if urllib.parse.urlparse(upload_url).hostname != "rupload.facebook.com":
        raise RuntimeError("Meta returned an unexpected Reel upload host.")
    size = video_path.stat().st_size
    request = urllib.request.Request(upload_url, data=video_path.read_bytes(),
                                    headers={"Authorization": "OAuth " + token,
                                             "offset": "0", "file_size": str(size),
                                             "Content-Type": "application/octet-stream"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(4096))
            detail = safe_text(payload.get("error", {}).get("message", ""))
        except Exception:
            detail = ""
        raise RuntimeError(f"Meta Reel upload returned HTTP {exc.code}" + (f": {detail[:220]}" if detail else ".")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Could not upload the Reel to Meta. Check the internet connection and retry.") from exc
    if not isinstance(result, dict) or result.get("success") is not True:
        raise RuntimeError("Meta did not confirm the Reel upload.")
    return result


def call_ai(system_prompt, user_prompt, schema=None, max_output_tokens=500):
    """Call configured text model and return plain text or schema-constrained JSON text."""
    provider = configured_ai_provider()
    if not provider:
        raise RuntimeError("No AI provider is configured. Add a Gemini or OpenAI API key to .env.")
    model = configured_ai_model(provider)
    if provider == "openai":
        payload = {"model": model, "store": False,
                   "input": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                   "max_output_tokens": max_output_tokens}
        if schema:
            payload["text"] = {"format": {"type": "json_schema", "name": "finance_brief_output", "strict": True, "schema": schema}}
        url = "https://api.openai.com/v1/responses"
        api_key = os.environ["OPENAI_API_KEY"]
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    else:
        generation_config = {"maxOutputTokens": max_output_tokens}
        if schema:
            allowed_schema_keys = {"type", "properties", "required", "items", "enum", "description", "format", "nullable", "minimum", "maximum", "minItems", "maxItems"}
            def gemini_schema(value):
                if isinstance(value, list):
                    return [gemini_schema(item) for item in value]
                if isinstance(value, dict):
                    return {key: gemini_schema(child) for key, child in value.items() if key in allowed_schema_keys}
                return value
            generation_config.update({"responseMimeType": "application/json", "responseSchema": gemini_schema(schema)})
        payload = {"systemInstruction": {"parts": [{"text": system_prompt}]},
                   "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                   "generationConfig": generation_config}
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + urllib.parse.quote(model, safe="") + ":generateContent"
        api_key = os.environ["GEMINI_API_KEY"]
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=50) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            error_data = json.loads(exc.read(4096))
            detail = safe_text(error_data.get("error", {}).get("message", ""))
        except Exception:
            detail = ""
        suffix = f": {detail[:220]}" if detail else ""
        raise RuntimeError(f"{provider.title()} API returned HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the {provider.title()} API. Check your internet connection and try again.") from exc
    if provider == "openai":
        return "".join(part.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", []) if part.get("type") == "output_text").strip()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"{provider.title()} returned no text. Check the prompt or model access.")
    return "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", []) if part.get("text")).strip()


def ai_video_query_candidates(story, raw_query):
    """Ask the configured LLM for literal, story-relevant stock-video searches."""
    fallback = video_query_candidates(raw_query or story.get("title", ""))
    provider = configured_ai_provider()
    if not provider:
        return fallback, "topic fallback", "Add GEMINI_API_KEY or OPENAI_API_KEY to enable AI-selected footage keywords."
    schema = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
              "required": ["queries"], "additionalProperties": False}
    try:
        output = call_ai("You select Pixabay stock-video search phrases for a U.S. finance news reel. Return 4 concise, searchable visual subjects, not full headlines, article wording, hashtags, or opinions. Prioritize visuals that directly relate to the supplied story and include a safe, neutral b-roll alternative when footage of the event itself may be unavailable. Never suggest fabricated event footage, identifiable people as if they took part, or visual claims not established by the story. Return only the requested JSON.",
                         f"Headline: {story.get('title','')}\nTopic: {story.get('topic','')}\nSource summary: {safe_text(story.get('summary',''))[:1500]}\nOptional user search text: {safe_text(raw_query)[:250]}", schema, 200)
        parsed = json.loads(output)
        ai_terms = parsed.get("queries", [])
        cleaned = []
        for value in ai_terms:
            term = safe_text(value)[:70]
            if term and term.casefold() not in {x.casefold() for x in cleaned}:
                cleaned.append(term)
            if len(cleaned) == 4:
                break
        if cleaned:
            merged, seen = [], set()
            for term in cleaned + fallback:
                key = term.casefold()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(term)
            return merged[:6], provider, ""
    except Exception:
        pass
    return fallback, "topic fallback", f"{provider.title()} keyword generation did not complete; used built-in topic search instead."


def split_script_beats(script):
    sentences = re.split(r"(?<=[.!?])\s+", safe_text(script))
    beats, current = [], []
    for sentence in sentences:
        words = sentence.split()
        while words:
            room = 17 - len(current)
            current.extend(words[:room])
            words = words[room:]
            if len(current) >= 13 or words:
                beats.append(" ".join(current))
                current = []
    if current:
        if len(current) < 7 and beats:
            beats[-1] += " " + " ".join(current)
        else:
            beats.append(" ".join(current))
    return beats or [safe_text(script)]


def plan_visuals(story, clips, audio_duration):
    beats = split_script_beats(story.get("script", ""))
    valid_ids = [str(c["pixabay_id"]) for c in clips]
    assignments, method, note = {}, "topic matching", "Matched narration wording to Pixabay tags and search terms."
    provider = configured_ai_provider()
    if provider and clips:
        schema = {"type": "object", "properties": {"assignments": {"type": "array", "items": {
            "type": "object", "properties": {"beat_index": {"type": "integer"}, "clip_id": {"type": "string", "enum": valid_ids}, "reason": {"type": "string"}},
            "required": ["beat_index", "clip_id", "reason"], "additionalProperties": False}}}, "required": ["assignments"], "additionalProperties": False}
        prompt = {"beats": [{"beat_index": i, "text": b} for i, b in enumerate(beats)],
                  "clips": [{"id": str(c["pixabay_id"]), "duration_seconds": c.get("source_duration", 0),
                             "tags": c.get("clip_tags", ""), "search_query": c.get("search_query", "")} for c in clips]}
        try:
            out = call_ai("Create an editorial stock-footage storyboard. Assign every narration beat to exactly one provided clip ID using only its tags/search query and the beat text. Do not imply the stock video depicts the real event. Prefer variety when equally relevant. Return a short factual reason.",
                          json.dumps(prompt), schema, min(1500, 180 + len(beats) * 55))
            parsed = json.loads(out)
            for item in parsed.get("assignments", []):
                idx, clip_id = item.get("beat_index"), str(item.get("clip_id", ""))
                if isinstance(idx, int) and 0 <= idx < len(beats) and clip_id in valid_ids:
                    assignments[idx] = (clip_id, safe_text(item.get("reason", "Relevant stock footage.")))
            if len(assignments) == len(beats):
                method, note = f"{provider.title()} storyboard", "AI matched beats using clip tags and search terms; it did not inspect the video frames. Review the preview for visual fit."
            else:
                assignments = {}
        except Exception:
            assignments = {}
            note = f"{provider.title()} storyboard was unavailable; used topic matching based on clip tags and search terms."
    if not assignments:
        stop = {"the", "and", "for", "with", "from", "that", "this", "your", "about", "into", "after", "before", "what", "here", "today", "source", "story", "says", "said", "will", "are", "was", "were", "its", "has", "have", "not", "but", "than", "then", "they", "their", "you", "our", "news"}
        usage = {cid: 0 for cid in valid_ids}
        for idx, beat in enumerate(beats):
            terms = {w for w in re.findall(r"[a-z]{3,}", beat.lower()) if w not in stop}
            scored = []
            for clip in clips:
                cid = str(clip["pixabay_id"])
                metadata = (clip.get("clip_tags", "") + " " + clip.get("search_query", "")).lower()
                tags = set(re.findall(r"[a-z]{3,}", metadata))
                score = len(terms & tags) - usage[cid] * 0.15
                scored.append((score, -clips.index(clip), cid))
            cid = max(scored)[2]
            assignments[idx] = (cid, "Selected by matching narration words with clip tags and search terms.")
            usage[cid] += 1
    by_id = {str(c["pixabay_id"]): c for c in clips}
    weights = [max(1, len(b.split())) for b in beats]
    total_weight = sum(weights)
    render_duration = audio_duration + 1.5
    cursor, occurrences, scenes = 0.0, {}, []
    for idx, beat in enumerate(beats):
        clip_id, reason = assignments[idx]
        clip = by_id[clip_id]
        end = render_duration if idx == len(beats) - 1 else cursor + render_duration * weights[idx] / total_weight
        scene_duration = end - cursor
        source_duration = max(0.1, float(clip.get("source_duration") or 0))
        occurrence = occurrences.get(clip_id, 0)
        source_window = min(source_duration, max(3.0, scene_duration))
        max_start = max(0.0, source_duration - source_window)
        source_start = (max_start * occurrence / max(1, sum(1 for a in assignments.values() if a[0] == clip_id) - 1)) if max_start else 0.0
        occurrences[clip_id] = occurrence + 1
        scenes.append({"beat_index": idx, "spoken_text": beat, "start": round(cursor, 3), "end": round(end, 3),
                       "pixabay_id": clip_id, "clip_file": clip["clip_file"], "clip_author": clip.get("clip_author", ""),
                       "clip_page": clip.get("clip_page", ""), "search_query": clip.get("search_query", ""), "reason": reason,
                       "source_start": round(source_start, 3), "source_end": round(min(source_duration, source_start + source_window), 3)})
        cursor = end
    return {"method": method, "note": note, "total_duration": round(render_duration, 3), "scenes": scenes}


def search_pixabay_videos(conn, api_key, query):
    cache_key = query.casefold()
    cached = conn.execute("SELECT payload,fetched_at FROM media_search_cache WHERE cache_key=?", (cache_key,)).fetchone()
    if cached and datetime.now().timestamp() - cached["fetched_at"] < 86400:
        return json.loads(cached["payload"])
    params = urllib.parse.urlencode({"key": api_key, "q": query, "lang": "en", "per_page": 6, "safesearch": "true", "order": "popular"})
    req = urllib.request.Request("https://pixabay.com/api/videos/?" + params, headers={"User-Agent": "TheFinanceBrief/0.3"})
    with urllib.request.urlopen(req, timeout=30) as response:
        response_data = json.loads(response.read())
    clips = []
    for video in response_data.get("hits", []):
        variants = video.get("videos", {})
        chosen = next((variants.get(size) for size in ("medium", "small", "large", "tiny")
                       if variants.get(size, {}).get("url")), None)
        if chosen:
            clips.append({"id": video.get("id"), "duration": video.get("duration"), "tags": video.get("tags", ""), "thumbnail": chosen.get("thumbnail"), "search_query": query,
                          "author": video.get("user", "Pixabay contributor"),
                          "page_url": video.get("pageURL", "https://pixabay.com/videos/"),
                          "download_url": chosen["url"], "width": chosen.get("width"), "height": chosen.get("height")})
    conn.execute("INSERT OR REPLACE INTO media_search_cache(cache_key,payload,fetched_at) VALUES(?,?,?)",
                 (cache_key, json.dumps(clips), datetime.now().timestamp()))
    conn.commit()
    return clips


def safe_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_feed(feed):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "TheFinanceBrief/0.1 (editorial research)"})
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read(2_000_000)
    root = ET.fromstring(raw)
    found = []
    for item in root.findall(".//item")[:12]:
        title = safe_text(item.findtext("title"))
        link = safe_text(item.findtext("link"))
        desc = safe_text(html.unescape(item.findtext("description") or ""))
        publisher = safe_text(item.findtext("source"))
        pub = safe_text(item.findtext("pubDate"))
        if title and link:
            found.append({"title": title, "url": link, "summary": re.sub(r"<[^>]+>", "", desc), "published": pub,
                          "source": publisher or feed["name"], "topic": feed["topic"], "feed": feed["name"]})
    # Atom feeds are also accepted.
    if not found:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for item in root.findall("a:entry", ns)[:12]:
            title = safe_text(item.findtext("a:title", default="", namespaces=ns))
            link_el = item.find("a:link", ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            desc = safe_text(html.unescape(item.findtext("a:summary", default="", namespaces=ns)))
            if title and link:
                found.append({"title": title, "url": link, "summary": desc, "published": "",
                              "source": feed["name"], "topic": feed["topic"], "feed": feed["name"]})
    return found


def make_script(story):
    """Generate an attributed, non-advisory first draft; never imply independent verification."""
    title = story["title"].strip().rstrip(".")
    summary = story["summary"].strip()
    if not summary:
        summary = "The source has more detail, and the numbers behind this story matter."
    # Keep it factual and short until a configured LLM is available.
    provider = configured_ai_provider()
    if provider:
        try:
            out = call_ai("Write concise, engaging U.S. finance news reel scripts, around 90 words. Use only supplied source facts; do not invent numbers, causes, quotes, predictions, or advice. No buy/sell/hold guidance. Attribute the source in the script. Mark uncertainty. Use a strong but accurate first sentence, then explain why it matters, then a neutral close. Return only spoken script.",
                          f"Headline: {title}\nSource: {story['source']}\nSource URL: {story['url']}\nSource summary: {summary}", max_output_tokens=260)
            if out.strip():
                return out.strip()
            raise RuntimeError(f"{provider.title()} returned an empty script.")
        except urllib.error.HTTPError as exc:
            # Surface the provider's diagnosis, never request headers or keys.
            try:
                error_data = json.loads(exc.read(4096))
                detail = safe_text(error_data.get("error", {}).get("message", ""))
            except Exception:
                detail = ""
            suffix = f": {detail[:220]}" if detail else ""
            raise RuntimeError(f"{provider.title()} API returned HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach the {provider.title()} API. Check your internet connection and try again.") from exc
        except Exception as exc:
            if isinstance(exc, RuntimeError) and ("API returned HTTP" in str(exc) or "Could not reach" in str(exc)):
                raise
            raise RuntimeError(f"{provider.title()} drafting failed ({type(exc).__name__}). Check its API key and model, then try again.") from exc
    return (f"Here’s the number behind today’s headline: {title}. "
            f"{summary[:420]} "
            f"This update is based on reporting from {story['source']}. Check the source linked with this post for the full context. "
            "This is a news update, not financial advice.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        size = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/settings":
            provider = configured_ai_provider()
            self.send_json({"ai_provider": os.environ.get("AI_PROVIDER", "auto").strip().lower(),
                            "active_provider": provider or "fallback",
                            "openai_model": configured_ai_model("openai"),
                            "gemini_model": configured_ai_model("gemini"),
                            "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
                            "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
                            "meta_page_id": os.environ.get("META_PAGE_ID", ""),
                            "meta_page_token_set": bool(os.environ.get("META_PAGE_ACCESS_TOKEN")),
                            "meta_graph_api_version": os.environ.get("META_GRAPH_API_VERSION", "v26.0"),
                            "meta_publishing": meta_configured()})
            return
        if self.path == "/api/dashboard":
            conn = db()
            stories = [row_dict(r) for r in conn.execute("SELECT * FROM stories ORDER BY created DESC")]
            for story in stories:
                story["clips"] = [row_dict(r) for r in conn.execute(
                    "SELECT pixabay_id,position,clip_file,clip_url,clip_author,clip_page,source_duration,clip_tags,search_query FROM story_clips WHERE story_id=? ORDER BY position", (story["id"],))]
            conn.close()
            self.send_json({"stories": stories, "feeds": FEEDS,
                            "features": {"openai": bool(os.environ.get("OPENAI_API_KEY")),
                                         "gemini": bool(os.environ.get("GEMINI_API_KEY")),
                                         "ai": bool(configured_ai_provider()),
                                         "ai_provider": configured_ai_provider() or "fallback",
                                         "ai_model": configured_ai_model() if configured_ai_provider() else "Built-in fallback",
                                         "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("ELEVENLABS_VOICE_ID")),
                                         "kokoro": kokoro_ready(),
                                         "voice_provider": os.environ.get("VOICE_PROVIDER", "kokoro").strip().lower(),
                                         "pixabay": bool(os.environ.get("PIXABAY_API_KEY")),
                                         "ffmpeg": bool(ffmpeg_path()),
                                         "metaPublishing": meta_configured()}})
            return
        media = re.fullmatch(r"/media/([A-Za-z0-9_.-]+\.(?:mp3|wav|mp4))", urllib.parse.urlparse(self.path).path)
        if media:
            path = ASSET_DIR / media.group(1)
            if path.is_file():
                data = path.read_bytes()
                self.send_response(200)
                content_type = "audio/wav" if path.suffix.lower() == ".wav" else mimetypes.guess_type(path.name)[0]
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_json({"error": "Media file not found."}, 404)
            return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        try:
            payload = self.body()
            conn = db()
            if self.path == "/api/settings":
                provider = str(payload.get("ai_provider", "auto")).strip().lower()
                if provider not in ("auto", "openai", "gemini", "none"):
                    conn.close()
                    return self.send_json({"error": "Choose Auto, OpenAI, Gemini, or built-in fallback."}, 400)
                updates = {"AI_PROVIDER": provider}
                for field, env_name, default in (("openai_model", "OPENAI_MODEL", "gpt-5-mini"),
                                                  ("gemini_model", "GEMINI_MODEL", "gemini-3.1-flash-lite")):
                    model = str(payload.get(field, default)).strip()
                    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", model):
                        conn.close()
                        return self.send_json({"error": "Model names can use letters, numbers, dots, dashes, underscores, and colons."}, 400)
                    updates[env_name] = model
                for field, clear_field, env_name in (("openai_key", "clear_openai_key", "OPENAI_API_KEY"),
                                                     ("gemini_key", "clear_gemini_key", "GEMINI_API_KEY")):
                    secret = str(payload.get(field, "")).strip()
                    if "\n" in secret or "\r" in secret:
                        conn.close()
                        return self.send_json({"error": "API keys must be entered on a single line."}, 400)
                    if payload.get(clear_field):
                        updates[env_name] = ""
                    elif secret:
                        updates[env_name] = secret
                page_id = str(payload.get("meta_page_id", os.environ.get("META_PAGE_ID", ""))).strip()
                if page_id and not re.fullmatch(r"\d{5,30}", page_id):
                    conn.close()
                    return self.send_json({"error": "Facebook Page ID must contain 5 to 30 digits."}, 400)
                version = str(payload.get("meta_graph_api_version", "v26.0")).strip()
                if not re.fullmatch(r"v\d{1,2}\.\d", version):
                    conn.close()
                    return self.send_json({"error": "Graph API version should look like v26.0."}, 400)
                updates["META_PAGE_ID"] = page_id
                updates["META_GRAPH_API_VERSION"] = version
                token = str(payload.get("meta_page_access_token", "")).strip()
                if "\n" in token or "\r" in token:
                    conn.close()
                    return self.send_json({"error": "The Page access token must be a single line."}, 400)
                if payload.get("clear_meta_page_access_token"):
                    updates["META_PAGE_ACCESS_TOKEN"] = ""
                elif token:
                    updates["META_PAGE_ACCESS_TOKEN"] = token
                save_local_settings(updates)
                active = configured_ai_provider() or "fallback"
                conn.close()
                return self.send_json({"ok": True, "active_provider": active,
                                       "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
                                       "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
                                       "meta_page_token_set": bool(os.environ.get("META_PAGE_ACCESS_TOKEN")),
                                       "meta_publishing": meta_configured(),
                                       "message": "Settings saved locally and applied to the app."})
            media_match = re.fullmatch(r"/api/stories/(\d+)/(voice|videos|select-video|graphic|plan-visuals|render)", self.path)
            if media_match:
                sid, action = int(media_match.group(1)), media_match.group(2)
                story = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
                if not story:
                    conn.close()
                    return self.send_json({"error": "Story not found."}, 404)
                if not story["script"]:
                    conn.close()
                    return self.send_json({"error": "Create and review a script before making a reel."}, 400)
                story = dict(story)
                if action == "render" and story["status"] == "approved":
                    conn.close()
                    return self.send_json({"error": "This reel is approved. Choose Make changes first to send it back for review."}, 400)
                if action == "voice":
                    key, voice_id = os.environ.get("ELEVENLABS_API_KEY"), os.environ.get("ELEVENLABS_VOICE_ID")
                    use_elevenlabs = os.environ.get("VOICE_PROVIDER", "kokoro").strip().lower() == "elevenlabs"
                    if use_elevenlabs and key and voice_id:
                        voice_url = "https://api.elevenlabs.io/v1/text-to-speech/" + urllib.parse.quote(voice_id, safe="") + "?output_format=mp3_44100_128"
                        req = urllib.request.Request(voice_url, data=json.dumps({"text": story["script"], "model_id": os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"), "voice_settings": {"stability": 0.55, "similarity_boost": 0.75, "style": 0.2, "use_speaker_boost": True}}).encode(),
                                                     headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}, method="POST")
                        with urllib.request.urlopen(req, timeout=90) as response:
                            audio = response.read(20_000_000)
                        filename = f"voice-{sid}.mp3"
                        (ASSET_DIR / filename).write_bytes(audio)
                    elif kokoro_ready():
                        runtime = kokoro_python()
                        filename = f"voice-{sid}.wav"
                        output_path = ASSET_DIR / filename
                        env = os.environ.copy()
                        env["PYTHONPATH"] = str(ROOT / "work/kokoro_pydeps")
                        env["HF_HOME"] = str(ROOT / "work/kokoro_cache")
                        env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
                        helper = ROOT / "work/kokoro_tts.py"
                        result = subprocess.run([runtime, str(helper), str(output_path), os.environ.get("KOKORO_VOICE", "af_heart")],
                                                input=story["script"], capture_output=True, text=True, timeout=300, env=env)
                        if result.returncode != 0 or not output_path.is_file():
                            conn.close()
                            detail = (result.stderr or result.stdout or "Local speech generation failed.")[-1200:]
                            return self.send_json({"error": detail}, 500)
                    else:
                        conn.close()
                        return self.send_json({"error": "Local Kokoro voice is not installed. Install the Kokoro dependencies and model, or configure ElevenLabs."}, 400)
                    conn.execute("UPDATE stories SET audio_file=?,render_file='',visual_plan='',status='draft' WHERE id=?", (filename, sid))
                    conn.commit()
                    updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                    conn.close()
                    return self.send_json({"story": updated, "audio_url": "/media/" + filename})
                if action == "videos":
                    key = os.environ.get("PIXABAY_API_KEY")
                    if not key:
                        conn.close()
                        return self.send_json({"error": "Set PIXABAY_API_KEY in your local .env file, then restart the app. Or use the original graphic background without a stock API key."}, 400)
                    queries, keyword_source, keyword_note = ai_video_query_candidates(story, payload.get("query") or story["title"])
                    clips, searched = [], []
                    for candidate in queries:
                        searched.append(candidate)
                        for clip in search_pixabay_videos(conn, key, candidate):
                            clip.setdefault("search_query", candidate)
                            if not any(str(saved.get("id")) == str(clip.get("id")) for saved in clips):
                                clips.append(clip)
                        if len(clips) >= 18:
                            break
                    conn.close()
                    return self.send_json({"clips": clips[:18], "queries": searched, "keyword_source": keyword_source,
                                           "keyword_note": keyword_note, "provider": "Pixabay", "pixabay_url": "https://pixabay.com"})
                if action == "select-video":
                    clip = payload.get("clip", {})
                    pixabay_id = str(clip.get("id", ""))
                    existing = conn.execute("SELECT clip_file FROM story_clips WHERE story_id=? AND pixabay_id=?", (sid, pixabay_id)).fetchone()
                    if existing:
                        conn.execute("DELETE FROM story_clips WHERE story_id=? AND pixabay_id=?", (sid, pixabay_id))
                    else:
                        if not pixabay_id.isdigit():
                            conn.close()
                            return self.send_json({"error": "Choose a valid clip from Pixabay search results."}, 400)
                        current_count = conn.execute("SELECT COUNT(*) FROM story_clips WHERE story_id=?", (sid,)).fetchone()[0]
                        if current_count >= 5:
                            conn.close()
                            return self.send_json({"error": "A reel can use up to five clips. Remove one before adding another."}, 400)
                        download_url = str(clip.get("download_url", ""))
                        page_url = str(clip.get("page_url", ""))
                        if urllib.parse.urlparse(download_url).hostname != "cdn.pixabay.com" or urllib.parse.urlparse(page_url).hostname not in ("pixabay.com", "www.pixabay.com"):
                            conn.close()
                            return self.send_json({"error": "Choose a video from Pixabay search results."}, 400)
                        req = urllib.request.Request(download_url, headers={"User-Agent": "TheFinanceBrief/0.3"})
                        with urllib.request.urlopen(req, timeout=90) as response:
                            if int(response.headers.get("Content-Length", "0") or 0) > 120_000_000:
                                conn.close()
                                return self.send_json({"error": "That clip is too large. Choose another Pixabay result."}, 400)
                            clip_bytes = response.read(120_000_001)
                        if len(clip_bytes) > 120_000_000:
                            conn.close()
                            return self.send_json({"error": "That clip is too large. Choose another Pixabay result."}, 400)
                        filename = f"clip-{sid}-{pixabay_id}.mp4"
                        (ASSET_DIR / filename).write_bytes(clip_bytes)
                        position = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM story_clips WHERE story_id=?", (sid,)).fetchone()[0]
                        conn.execute("INSERT INTO story_clips(story_id,pixabay_id,position,clip_file,clip_url,clip_author,clip_page,source_duration,clip_tags,search_query) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                     (sid, pixabay_id, position, filename, download_url, safe_text(clip.get("author", "")), page_url,
                                      float(clip.get("duration") or 0), safe_text(clip.get("tags", "")), safe_text(clip.get("search_query", ""))))
                    first = conn.execute("SELECT clip_file,clip_url,clip_author,clip_page FROM story_clips WHERE story_id=? ORDER BY position LIMIT 1", (sid,)).fetchone()
                    conn.execute("UPDATE stories SET clip_file=?,clip_url=?,clip_author=?,clip_page=?,visual_mode='',visual_plan='',render_file='',status='draft' WHERE id=?",
                                 tuple(first) + (sid,) if first else ("", "", "", "", sid))
                    conn.commit()
                    updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                    updated["clips"] = [row_dict(r) for r in conn.execute(
                        "SELECT pixabay_id,position,clip_file,clip_url,clip_author,clip_page,source_duration,clip_tags,search_query FROM story_clips WHERE story_id=? ORDER BY position", (sid,))]
                    conn.close()
                    return self.send_json({"story": updated, "clips": updated["clips"]})
                if action == "graphic":
                    conn.execute("DELETE FROM story_clips WHERE story_id=?", (sid,))
                    conn.execute("UPDATE stories SET visual_mode='graphic',visual_plan='',clip_file='',clip_url='',clip_author='',clip_page='',render_file='',status='draft' WHERE id=?", (sid,))
                    conn.commit()
                    updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                    conn.close()
                    return self.send_json({"story": updated, "message": "Original graphic background selected."})
                if action == "plan-visuals":
                    if not story.get("audio_file"):
                        conn.close()
                        return self.send_json({"error": "Generate narration first so the storyboard can match the voiceover timing."}, 400)
                    clips = [row_dict(r) for r in conn.execute("SELECT * FROM story_clips WHERE story_id=? ORDER BY position", (sid,))]
                    if not clips:
                        conn.close()
                        return self.send_json({"error": "Select at least one Pixabay clip before planning visuals."}, 400)
                    ffmpeg = ffmpeg_path()
                    if not ffmpeg or not (ASSET_DIR / story["audio_file"]).is_file():
                        conn.close()
                        return self.send_json({"error": "Narration or video timing support is unavailable. Generate narration again and restart the app."}, 400)
                    plan = plan_visuals(story, clips, media_duration(ASSET_DIR / story["audio_file"], ffmpeg))
                    plan_json = json.dumps(plan, ensure_ascii=False)
                    conn.execute("UPDATE stories SET visual_plan=?,render_file='',status='draft' WHERE id=?", (plan_json, sid))
                    conn.commit()
                    updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                    conn.close()
                    return self.send_json({"story": updated, "plan": plan})
                if action == "render":
                    ffmpeg = ffmpeg_path()
                    if not ffmpeg:
                        conn.close()
                        return self.send_json({"error": "The FFmpeg renderer is missing. Install the listed Python dependency and restart the app."}, 400)
                    selected_clips = [row_dict(r) for r in conn.execute(
                        "SELECT pixabay_id,clip_file,clip_url,clip_author,clip_page,source_duration,clip_tags,search_query FROM story_clips WHERE story_id=? ORDER BY position", (sid,))]
                    if not selected_clips and story.get("clip_file"):
                        selected_clips = [{"clip_file": story["clip_file"]}]
                    if not story.get("audio_file") or not (selected_clips or story.get("visual_mode") == "graphic"):
                        conn.close()
                        return self.send_json({"error": "Generate narration and choose one or more videos or the original graphic background first."}, 400)
                    if selected_clips and not story.get("visual_plan"):
                        conn.close()
                        return self.send_json({"error": "Create the visual storyboard first so clips are matched to narration beats."}, 400)
                    audio_path = ASSET_DIR / story["audio_file"]
                    clip_paths = [ASSET_DIR / item["clip_file"] for item in selected_clips]
                    if not audio_path.is_file() or any(not p.is_file() for p in clip_paths):
                        conn.close()
                        return self.send_json({"error": "A required media file is missing. Generate it again."}, 400)
                    duration = media_duration(audio_path, ffmpeg)
                    srt_name = f"captions-{sid}.srt"
                    srt_path = ASSET_DIR / srt_name
                    srt_path.write_text(make_captions(story["script"], duration), encoding="utf-8")
                    hook_name = f"hook-{sid}.txt"
                    hook_path = ASSET_DIR / hook_name
                    title_words, title_lines, line = story["title"].split(), [], ""
                    for word in title_words:
                        if line and len(line) + len(word) + 1 > 25:
                            title_lines.append(line)
                            line = word
                        else:
                            line = (line + " " + word).strip()
                    if line:
                        title_lines.append(line)
                    hook_path.write_text("\n".join(title_lines[:4]), encoding="utf-8")
                    def filter_path(path):
                        return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                    subtitle_path, title_path = filter_path(srt_path), filter_path(hook_path)
                    common = ("drawtext=font='Arial':text='THE FINANCE BRIEF':fontcolor=0xe1bd82:fontsize=27:x=110:y=92:shadowcolor=black@0.35:shadowx=1:shadowy=2,"
                              "drawbox=x=110:y=145:w=860:h=3:color=0xe1bd82@0.85:t=fill,")
                    hook = f"drawtext=font='Arial':textfile='{title_path}':fontcolor=white:fontsize=58:line_spacing=18:x=112:y=200:box=1:boxcolor=0x0d1916@0.66:borderw=24:shadowcolor=black@0.3:shadowx=2:shadowy=3:enable='lt(t,4)',"
                    captions = f"subtitles='{subtitle_path}':force_style='FontName=Arial,FontSize=20,Outline=3,Shadow=1,Alignment=2,MarginV=180'"
                    if clip_paths:
                        try:
                            visual_plan = json.loads(story.get("visual_plan") or "")
                        except (TypeError, json.JSONDecodeError):
                            visual_plan = {}
                        scenes = visual_plan.get("scenes", [])
                        plan_ok = bool(scenes) and all((ASSET_DIR / scene.get("clip_file", "")).is_file() for scene in scenes)
                        if plan_ok:
                            input_args, graph_parts, segment_labels = [], [], []
                            for index, scene in enumerate(scenes):
                                scene_path = ASSET_DIR / scene["clip_file"]
                                segment_duration = max(0.1, float(scene["end"]) - float(scene["start"]))
                                input_args.extend(["-stream_loop", "-1", "-ss", str(max(0, float(scene.get("source_start", 0)))), "-i", str(scene_path)])
                                graph_parts.append(f"[{index}:v]trim=duration={segment_duration:.3f},setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1[vseg{index}]")
                                segment_labels.append(f"[vseg{index}]")
                            audio_index = len(scenes)
                            input_args.extend(["-i", str(audio_path)])
                            graph_parts.append("".join(segment_labels) + f"concat=n={len(scenes)}:v=1:a=0[base]")
                            graph_parts.append(f"[base]{common}{hook}{captions}[vout]")
                            render_filter = "-filter_complex"
                            filtergraph = ";".join(graph_parts)
                        else:
                            input_args = []
                            for path in clip_paths:
                                input_args.extend(["-stream_loop", "-1", "-i", str(path)])
                            audio_index = len(clip_paths)
                            input_args.extend(["-i", str(audio_path)])
                        if not plan_ok and len(clip_paths) == 1:
                            render_filter = "-vf"
                            audio_index = 1
                            filtergraph = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1,{common}{hook}{captions}"
                        elif not plan_ok:
                            render_filter = "-filter_complex"
                            total_duration = duration + 1.5
                            segments_per_clip = max(1, int((total_duration / (6 * len(clip_paths))) + 0.999))
                            segment_count = segments_per_clip * len(clip_paths)
                            segment_duration = total_duration / segment_count
                            source_uses = [sum(1 for idx in range(segment_count) if idx % len(clip_paths) == source)
                                           for source in range(len(clip_paths))]
                            graph_parts, source_labels, source_use_index = [], [], [0] * len(clip_paths)
                            for source, uses in enumerate(source_uses):
                                labels = [f"vsrc{source}_{part}" for part in range(uses)]
                                source_labels.append(labels)
                                if uses > 1:
                                    graph_parts.append(f"[{source}:v]split={uses}" + "".join(f"[{label}]" for label in labels))
                                else:
                                    graph_parts.append(f"[{source}:v]null[{labels[0]}]")
                            segment_labels = []
                            for index in range(segment_count):
                                source = index % len(clip_paths)
                                label = source_labels[source][source_use_index[source]]
                                source_use_index[source] += 1
                                segment_label = f"vseg{index}"
                                this_duration = min(segment_duration, max(0.1, total_duration - index * segment_duration))
                                graph_parts.append(f"[{label}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1,trim=duration={this_duration:.3f},setpts=PTS-STARTPTS[{segment_label}]")
                                segment_labels.append(f"[{segment_label}]")
                            graph_parts.append("".join(segment_labels) + f"concat=n={segment_count}:v=1:a=0[base]")
                            graph_parts.append(f"[base]{common}{hook}{captions}[vout]")
                            filtergraph = ";".join(graph_parts)
                    else:
                        input_args = ["-f", "lavfi", "-i", f"color=c=0x142720:s=1080x1920:r=30:d={duration + 1.5}", "-i", str(audio_path)]
                        audio_index = 1
                        render_filter = "-vf"
                        filtergraph = f"drawgrid=width=120:height=120:thickness=1:color=0xffffff@0.035,{common}{hook}{captions}"
                    output_name = f"reel-{sid}.mp4"
                    output_path = ASSET_DIR / output_name
                    map_args = (["-map", "[vout]", "-map", f"{audio_index}:a:0"] if render_filter == "-filter_complex"
                                else ["-map", "0:v:0", "-map", f"{audio_index}:a:0"])
                    result = subprocess.run([ffmpeg, "-y", *input_args,
                                             render_filter, filtergraph, *map_args, "-af", "apad=pad_dur=1.5", "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(output_path)],
                                            capture_output=True, text=True, timeout=240)
                    if result.returncode != 0 or not output_path.exists():
                        conn.close()
                        raise RuntimeError("Video rendering failed. Check FFmpeg includes subtitle support and choose another clip.")
                    used_source_files = list(dict.fromkeys(item["clip_file"] for item in selected_clips if item.get("clip_file")))
                    if selected_clips:
                        try:
                            saved_plan = json.loads(story.get("visual_plan") or "{}")
                        except (TypeError, json.JSONDecodeError):
                            saved_plan = {}
                        for scene in saved_plan.get("scenes", []):
                            scene["clip_file"] = ""
                        conn.execute("DELETE FROM story_clips WHERE story_id=?", (sid,))
                        conn.execute("UPDATE stories SET render_file=?,visual_plan=?,clip_file='',clip_url='',clip_author='',clip_page='',visual_mode='produced-video',status='draft' WHERE id=?",
                                     (output_name, json.dumps(saved_plan, ensure_ascii=False), sid))
                    else:
                        conn.execute("UPDATE stories SET render_file=?,status='draft' WHERE id=?", (output_name, sid))
                    conn.commit()
                    cleanup_complete = True
                    for filename in used_source_files:
                        candidate = ASSET_DIR / Path(filename).name
                        if candidate.parent == ASSET_DIR and candidate.is_file():
                            try:
                                candidate.unlink(missing_ok=True)
                            except OSError:
                                cleanup_complete = False
                    updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                    conn.close()
                    return self.send_json({"story": updated, "video_url": "/media/" + output_name, "source_cleanup_complete": cleanup_complete})
            if self.path == "/api/research":
                all_items, errors = [], []
                feed_results = []
                for feed in FEEDS:
                    try:
                        results = fetch_feed(feed)
                        all_items.extend(results)
                        feed_results.append({"name": feed["name"], "count": len(results), "ok": True})
                    except Exception as e:
                        message = f"{feed['name']}: {str(e)[:160]}"
                        errors.append(message)
                        feed_results.append({"name": feed["name"], "count": 0, "ok": False, "error": str(e)[:160]})
                existing = {r[0] for r in conn.execute("SELECT url FROM stories")}
                added = 0
                for item in all_items:
                    if item["url"] not in existing:
                        conn.execute("INSERT INTO stories(title,source,url,summary,topic,published,created,status) VALUES(?,?,?,?,?,?,?, 'research')",
                                     (item["title"], item["source"], item["url"], item["summary"], item["topic"], item["published"], now()))
                        added += 1
                conn.commit()
                conn.close()
                return self.send_json({"ok": True, "found": len(all_items), "added": added, "errors": errors, "feed_results": feed_results})
            if self.path == "/api/stories":
                title, url = safe_text(payload.get("title")), safe_text(payload.get("url"))
                if not title or not url.startswith(("https://", "http://")):
                    conn.close()
                    return self.send_json({"error": "Add a headline and a valid source link."}, 400)
                cur = conn.execute("INSERT INTO stories(title,source,url,summary,topic,created,status) VALUES(?,?,?,?,?,?, 'research')",
                                   (title, safe_text(payload.get("source")) or urllib.parse.urlparse(url).netloc,
                                    url, safe_text(payload.get("summary")), safe_text(payload.get("topic")) or "Economy", now()))
                conn.commit()
                story = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (cur.lastrowid,)).fetchone())
                conn.close()
                return self.send_json({"story": story}, 201)
            delete_match = re.fullmatch(r"/api/stories/(\d+)/delete", self.path)
            if delete_match:
                sid = int(delete_match.group(1))
                story = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
                if not story:
                    conn.close()
                    return self.send_json({"error": "Story not found."}, 404)
                if story["status"] != "rejected":
                    conn.close()
                    return self.send_json({"error": "Only rejected ideas can be permanently deleted."}, 400)
                filenames = {story[name] for name in ("audio_file", "clip_file", "render_file") if story[name]}
                filenames.update(row[0] for row in conn.execute("SELECT clip_file FROM story_clips WHERE story_id=? AND clip_file!=''", (sid,)))
                conn.execute("DELETE FROM story_clips WHERE story_id=?", (sid,))
                conn.execute("DELETE FROM stories WHERE id=?", (sid,))
                conn.commit()
                conn.close()
                for filename in filenames:
                    candidate = ASSET_DIR / Path(filename).name
                    if candidate.parent == ASSET_DIR and candidate.is_file():
                        candidate.unlink(missing_ok=True)
                return self.send_json({"ok": True, "message": "Rejected idea and its local media were deleted."})
            publish_match = re.fullmatch(r"/api/stories/(\d+)/publish", self.path)
            if publish_match:
                sid = int(publish_match.group(1))
                story = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
                if not story:
                    conn.close()
                    return self.send_json({"error": "Story not found."}, 404)
                story = dict(story)
                if not meta_configured():
                    conn.close()
                    return self.send_json({"error": "Connect your Facebook Page in Settings before publishing."}, 400)
                if story["status"] != "approved" or not story["script"] or not story["render_file"]:
                    conn.close()
                    return self.send_json({"error": "Only an approved reel with a rendered preview can be published."}, 400)
                state = story.get("fb_publish_state") or ""
                if state == "published":
                    conn.close()
                    return self.send_json({"error": "This reel has already been published."}, 409)
                if state in ("starting", "publishing", "publish_unknown"):
                    conn.close()
                    return self.send_json({"error": "A previous publishing attempt may still be processing. Check the Facebook Page before retrying."}, 409)
                render_name = Path(story["render_file"]).name
                video_path = (ASSET_DIR / render_name).resolve()
                if video_path.parent != ASSET_DIR.resolve() or video_path.suffix.lower() != ".mp4" or not video_path.is_file():
                    conn.close()
                    return self.send_json({"error": "The rendered reel file is missing. Render and review the reel again."}, 400)
                token = os.environ["META_PAGE_ACCESS_TOKEN"]
                page_id = os.environ["META_PAGE_ID"]
                video_id = story.get("fb_video_id") or ""
                upload_url = story.get("fb_upload_url") or ""
                if not video_id:
                    claimed = conn.execute("UPDATE stories SET fb_publish_state='starting' WHERE id=? AND COALESCE(fb_publish_state,'') IN ('','failed')", (sid,))
                    if claimed.rowcount != 1:
                        conn.close()
                        return self.send_json({"error": "Another publish request is already handling this reel."}, 409)
                    conn.commit()
                    try:
                        started = meta_graph_post(f"{page_id}/video_reels", {"upload_phase": "start"}, token)
                        video_id = str(started.get("video_id", ""))
                        upload_url = str(started.get("upload_url", ""))
                        if not video_id or not upload_url:
                            raise RuntimeError("Meta did not return a Reel upload session.")
                        if urllib.parse.urlparse(upload_url).hostname != "rupload.facebook.com":
                            raise RuntimeError("Meta returned an unexpected Reel upload host.")
                        conn.execute("UPDATE stories SET fb_publish_state='uploading',fb_video_id=?,fb_upload_url=? WHERE id=?",
                                     (video_id, upload_url, sid))
                        conn.commit()
                    except Exception as exc:
                        conn.execute("UPDATE stories SET fb_publish_state='failed' WHERE id=?", (sid,))
                        conn.commit()
                        conn.close()
                        return self.send_json({"error": str(exc)[:300]}, 502)
                if state != "uploaded":
                    try:
                        meta_upload_reel(upload_url, token, video_path)
                        conn.execute("UPDATE stories SET fb_publish_state='uploaded' WHERE id=?", (sid,))
                        conn.commit()
                    except Exception as exc:
                        conn.close()
                        return self.send_json({"error": f"Reel upload failed. Retry to resume this upload: {str(exc)[:220]}"}, 502)
                conn.execute("UPDATE stories SET fb_publish_state='publishing' WHERE id=?", (sid,))
                conn.commit()
                try:
                    finished = meta_graph_post(f"{page_id}/video_reels",
                                               {"video_id": video_id, "upload_phase": "finish",
                                                "video_state": "PUBLISHED", "title": story["title"],
                                                "description": story["caption"] or ""}, token)
                    if finished.get("success") is not True:
                        raise RuntimeError("Meta did not confirm publication.")
                except Exception as exc:
                    conn.execute("UPDATE stories SET fb_publish_state='publish_unknown' WHERE id=?", (sid,))
                    conn.commit()
                    conn.close()
                    return self.send_json({"error": "Meta may have received the publish request. Check your Facebook Page before trying again. " + str(exc)[:180]}, 502)
                conn.execute("UPDATE stories SET fb_publish_state='published',fb_published_at=? WHERE id=?", (now(), sid))
                conn.commit()
                updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                conn.close()
                return self.send_json({"story": updated, "message": "Reel published to your Facebook Page."})
            match = re.fullmatch(r"/api/stories/(\d+)/(draft|approve|reject|revise)", self.path)
            if match:
                sid, action = int(match.group(1)), match.group(2)
                story = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
                if not story:
                    conn.close()
                    return self.send_json({"error": "Story not found."}, 404)
                if action == "draft":
                    if story["status"] == "approved" or story["status"] == "rejected":
                        conn.close()
                        return self.send_json({"error": "This idea cannot be drafted from its current status."}, 400)
                    script = make_script(dict(story))
                    conn.execute("UPDATE stories SET script=?,caption=?,status='draft',audio_file='',render_file='',visual_plan='' WHERE id=?", (script, f"{story['title']} — via {story['source']}. News, not financial advice. #Finance #Economy", sid))
                elif action == "approve":
                    if not story["script"] or not story["render_file"]:
                        conn.close()
                        return self.send_json({"error": "Render and review the complete reel before approving it."}, 400)
                    conn.execute("UPDATE stories SET status='approved' WHERE id=?", (sid,))
                elif action == "revise":
                    if story["status"] != "approved" or (story["fb_publish_state"] or "") in ("published", "uploading", "uploaded", "starting", "publishing", "publish_unknown"):
                        conn.close()
                        return self.send_json({"error": "This reel cannot be revised now. Check its Facebook publishing status first."}, 400)
                    conn.execute("UPDATE stories SET status='draft' WHERE id=?", (sid,))
                else:
                    if (story["fb_publish_state"] or "") in ("published", "publishing", "publish_unknown"):
                        conn.close()
                        return self.send_json({"error": "A published or in-progress reel cannot be rejected here."}, 400)
                    conn.execute("UPDATE stories SET status='rejected' WHERE id=?", (sid,))
                conn.commit()
                updated = row_dict(conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone())
                conn.close()
                messages = {"approve": "Approval recorded. Publishing is still a separate action.",
                            "revise": "Reel returned to review. Save any changes and approve it again.",
                            "reject": "Idea rejected.", "draft": "Draft created."}
                return self.send_json({"story": updated, "message": messages.get(action, "Updated.")})
            match = re.fullmatch(r"/api/stories/(\d+)/edit", self.path)
            if match:
                sid = int(match.group(1))
                old = conn.execute("SELECT script,caption,status,fb_publish_state FROM stories WHERE id=?", (sid,)).fetchone()
                if not old:
                    conn.close()
                    return self.send_json({"error": "Story not found."}, 404)
                if old["status"] == "rejected" or (old["fb_publish_state"] or "") in ("published", "starting", "uploading", "uploaded", "publishing", "publish_unknown"):
                    conn.close()
                    return self.send_json({"error": "This reel cannot be edited after rejection or while Facebook publishing is active."}, 400)
                new_script = str(payload.get("script", ""))[:12000]
                new_caption = str(payload.get("caption", ""))[:2000]
                if new_script != old["script"]:
                    conn.execute("UPDATE stories SET script=?,caption=?,audio_file='',render_file='',visual_plan='',status='draft' WHERE id=?",
                                 (new_script, new_caption, sid))
                elif new_caption != old["caption"]:
                    conn.execute("UPDATE stories SET caption=?,status=CASE WHEN status='approved' THEN 'draft' ELSE status END WHERE id=?",
                                 (new_caption, sid))
                else:
                    conn.execute("UPDATE stories SET caption=? WHERE id=?", (new_caption, sid))
                conn.commit()
                updated = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
                conn.close()
                if not updated:
                    return self.send_json({"error": "Story not found."}, 404)
                return self.send_json({"story": row_dict(updated)})
            conn.close()
            self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)[:240]}, 500)


if __name__ == "__main__":
    db().close()
    print(f"The Finance Brief is ready at http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
