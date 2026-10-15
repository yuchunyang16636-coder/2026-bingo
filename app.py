import streamlit as st
import time
import random
import base64
import json
import os
import re
import uuid
from html import escape as html_escape
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

try:
    import segno  # 產生 QR Code 用 (純 Python，requirements.txt 請加上 segno)
except ImportError:
    segno = None

# 連成幾條線才算中獎 (手機端與主持人端共用)
WIN_LINES = 2

def detect_player_url():
    """嘗試從目前網址自動推測手機登入網址 (需要 Streamlit 1.37 以上)"""
    try:
        host = st.context.headers.get("Host", "")
    except Exception:
        host = ""
    if not host:
        return ""
    local_prefixes = ("localhost", "127.0.0.1", "192.168.", "10.")
    scheme = "http" if host.startswith(local_prefixes) else "https"
    return f"{scheme}://{host}/?mode=player"

def normalize_player_url(url):
    """確保網址結尾帶有 mode=player"""
    url = (url or "").strip()
    if not url:
        return ""
    if "mode=player" not in url:
        url += ("&" if "?" in url else "?") + "mode=player"
    return url

# ==========================================
# 0. 雲端共用狀態檔案設定 (用於同步主持人與手機)
# ==========================================
SYNC_FILE = "drawn_state.json"

@st.cache_resource(show_spinner=False)
def _state_holder():
    """所有連線共用的小容器，記住「最後一次成功讀到」的狀態"""
    return {"last_good": None}

def load_game_state():
    default = {"pool": list(range(1, 100)), "drawn": [], "round": "r0"}
    holder = _state_holder()
    if not os.path.exists(SYNC_FILE):
        return holder["last_good"] or default
    # 剛好撞到主持人寫檔時可能讀到不完整內容，重試幾次
    for _ in range(3):
        try:
            with open(SYNC_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            holder["last_good"] = data
            return data
        except Exception:
            time.sleep(0.05)
    # 還是讀不到就沿用上一次成功的內容，避免手機畫面閃成「全部沒中」
    return holder["last_good"] or default

def save_game_state(pool, drawn, round_id=None):
    # round_id 是「輪次編號」：主持人重置時會換成新的，所有舊卡片就自動失效
    if round_id is None:
        round_id = load_game_state().get("round", "r0")
    # 先寫到暫存檔，再一次性替換，讀取端永遠不會看到寫到一半的檔案
    tmp_file = SYNC_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump({"pool": pool, "drawn": drawn, "round": round_id}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, SYNC_FILE)
    _state_holder()["last_good"] = {"pool": list(pool), "drawn": list(drawn), "round": round_id}

# ==========================================
# 0-1. 玩家卡片 (每人一個檔案，60 人同時鎖定也不會互相覆蓋)
# ==========================================
CARD_DIR = "player_cards"
PID_PATTERN = re.compile(r"[0-9a-f]{32}")

def is_valid_pid(pid):
    # pid 來自網址或 cookie，一律檢查格式，避免被拿來亂指定檔案路徑
    return bool(pid) and PID_PATTERN.fullmatch(str(pid)) is not None

def _card_path(pid):
    return os.path.join(CARD_DIR, f"{pid}.json")

def load_card_data(pid, round_id):
    """讀取這位玩家「本輪」已鎖定的卡片資料 (含暱稱)，沒有就回傳 None"""
    try:
        with open(_card_path(pid), "r", encoding="utf-8") as f:
            data = json.load(f)
        nums = data.get("numbers")
        if data.get("round") == round_id and isinstance(nums, list) and len(nums) == 25:
            return data
    except Exception:
        pass
    return None

def load_card(pid, round_id):
    """只取 25 個數字，沒有就回傳 None"""
    data = load_card_data(pid, round_id)
    return data["numbers"] if data else None

def save_card(pid, round_id, numbers, nick=""):
    os.makedirs(CARD_DIR, exist_ok=True)
    path = _card_path(pid)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"round": round_id, "numbers": numbers, "nick": nick}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

def clear_all_cards():
    if os.path.isdir(CARD_DIR):
        for name in os.listdir(CARD_DIR):
            try:
                os.remove(os.path.join(CARD_DIR, name))
            except OSError:
                pass

# ==========================================
# 0-1b. 上線人數 (每支手機定時「打卡」，主持人端統計)
# ==========================================
PRESENCE_DIR = "presence"
ONLINE_WINDOW = 15   # 幾秒內有打卡過，就算「目前在線」

def touch_presence(pid):
    """手機端打卡：更新自己那個小檔案的修改時間 (內容不重要，只看時間)"""
    try:
        os.makedirs(PRESENCE_DIR, exist_ok=True)
        path = os.path.join(PRESENCE_DIR, f"{pid}.ping")
        try:
            os.utime(path, None)
        except FileNotFoundError:
            open(path, "w").close()
    except OSError:
        pass

def get_presence_counts():
    """回傳 (目前在線人數, 累計連線過的端末數)"""
    online = total = 0
    if os.path.isdir(PRESENCE_DIR):
        now = time.time()
        for name in os.listdir(PRESENCE_DIR):
            if not name.endswith(".ping"):
                continue
            total += 1
            try:
                if now - os.path.getmtime(os.path.join(PRESENCE_DIR, name)) <= ONLINE_WINDOW:
                    online += 1
            except OSError:
                pass
    return online, total

def count_locked_cards(round_id):
    """本輪已鎖定卡片的人數"""
    count = 0
    if os.path.isdir(CARD_DIR):
        for name in os.listdir(CARD_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(CARD_DIR, name), "r", encoding="utf-8") as f:
                    if json.load(f).get("round") == round_id:
                        count += 1
            except Exception:
                pass
    return count

def clear_presence():
    if os.path.isdir(PRESENCE_DIR):
        for name in os.listdir(PRESENCE_DIR):
            try:
                os.remove(os.path.join(PRESENCE_DIR, name))
            except OSError:
                pass

# ==========================================
# 0-2. 連線判斷與中獎者計算 (手機端與主持人端共用)
# ==========================================
def get_completed_lines(numbers, drawn_set):
    """回傳已完成的連線，例如 [('row', 0), ('col', 2), ('diag', 0), ('anti', 0)]"""
    m = [numbers[i:i+5] for i in range(0, 25, 5)]
    lines = []
    for r in range(5):
        if all(m[r][c] in drawn_set for c in range(5)):
            lines.append(("row", r))
    for c in range(5):
        if all(m[r][c] in drawn_set for r in range(5)):
            lines.append(("col", c))
    if all(m[i][i] in drawn_set for i in range(5)):
        lines.append(("diag", 0))
    if all(m[i][4-i] in drawn_set for i in range(5)):
        lines.append(("anti", 0))
    return lines

def build_line_overlay(lines):
    """把每條連線畫成一條明顯的黃色粗線 (純 CSS，疊在方格上方)"""
    out = ""
    for kind, i in lines:
        if kind == "row":
            out += f'<div class="win-line row" style="top:{i * 20 + 10}%"></div>'
        elif kind == "col":
            out += f'<div class="win-line col" style="left:{i * 20 + 10}%"></div>'
        elif kind == "diag":
            out += '<div class="win-line diag"></div>'
        else:
            out += '<div class="win-line anti"></div>'
    return out

def first_win_index(numbers, drawn):
    """依開球順序，回傳這張卡是在『第幾顆球』達成中獎條件的；沒中獎回傳 None"""
    seen = set()
    for k, n in enumerate(drawn, start=1):
        seen.add(n)
        if len(get_completed_lines(numbers, seen)) >= WIN_LINES:
            return k
    return None

def get_winners(drawn, round_id):
    """掃描本輪所有已鎖定的卡片，回傳中獎者清單 (依達成順序排序)"""
    winners = []
    if not drawn or not os.path.isdir(CARD_DIR):
        return winners
    drawn_set = set(drawn)
    for name in os.listdir(CARD_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CARD_DIR, name), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        nums = data.get("numbers")
        if data.get("round") != round_id or not isinstance(nums, list) or len(nums) != 25:
            continue
        k = first_win_index(nums, drawn)
        if k is not None:
            winners.append({
                "nick": data.get("nick") or "（未入力）",
                "ball_no": k,
                "lines": len(get_completed_lines(nums, drawn_set)),
            })
    winners.sort(key=lambda w: (w["ball_no"], w["nick"]))
    return winners

# ==========================================
# 1. 圖片轉 Base64 內嵌工具
# ==========================================
@st.cache_resource(show_spinner=False)
def get_base64_of_bin_file(bin_file):
    try:
        with open(bin_file, 'rb') as f:
            data = f.read()
        return base64.b64encode(data).decode()
    except:
        return ""

img_base64 = get_base64_of_bin_file("0003.jpg")

# ==========================================
# 2. 50 顆球動畫生成器
# ==========================================
@st.cache_resource(show_spinner=False)
def build_balls_css():
    random.seed(2026) 
    balls_css = ""
    colors = ["#ff4b4b", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#e377c2", "#17becf"]

    for i in range(1, 51):
        color = random.choice(colors)
        start_x = random.randint(-160, 160)
        start_y = random.randint(80, 180)
    
        mid1_x = random.randint(-170, 170)
        mid1_y = random.randint(-40, 60)
        mid2_x = random.randint(-150, 150)
        mid2_y = random.randint(-130, -40)
    
        end_x = random.randint(-170, 170)
        end_y = random.randint(-180, -70)
    
        dur = random.uniform(0.4, 0.8)
    
        balls_css += f".bg-ball.b{i} {{ background: radial-gradient(circle at 30% 30%, #fff, {color}); transform: translate({start_x}px, {start_y}px) rotate(0deg); }}\n"
        balls_css += f".rolling .bg-ball.b{i} {{ animation: float{i} {dur:.2f}s infinite ease-in-out alternate; }}\n"
        balls_css += f"@keyframes float{i} {{\n"
        balls_css += f"  0% {{ transform: translate({start_x}px, {start_y}px) rotate(0deg); }}\n"
        balls_css += f"  33% {{ transform: translate({mid1_x}px, {mid1_y}px) rotate(120deg); }}\n"
        balls_css += f"  66% {{ transform: translate({mid2_x}px, {mid2_y}px) rotate(240deg); }}\n"
        balls_css += f"  100% {{ transform: translate({end_x}px, {end_y}px) rotate(360deg); }}\n"
        balls_css += f"}}\n"

    random.seed() 
    return balls_css

# 手機端用不到球的動畫，只有主持人端才產生
balls_css = "" if st.query_params.get("mode", "host") == "player" else build_balls_css()

# ==========================================
# 3. 基礎設定與 CSS 樣式
# ==========================================
st.set_page_config(layout="wide", initial_sidebar_state="collapsed")

st.markdown(f"<style>{balls_css}</style>", unsafe_allow_html=True)

bg_style = f"""
    <style>
    .stApp {{
        background: linear-gradient(rgba(0,0,0,0.7), rgba(0,0,0,0.7)), url("data:image/jpeg;base64,{img_base64}") !important;
        background-size: cover !important;
        background-position: center !important;
        background-attachment: fixed !important;
    }}
    </style>
"""
st.markdown(bg_style, unsafe_allow_html=True)

st.markdown("""
    <style>
    header[data-testid="stHeader"] { display: none !important; }
    section[data-testid="stSidebar"] { display: none !important; }
    
    .bingo-title {
        text-align: center; font-size: 6rem; font-weight: 900;
        background: linear-gradient(90deg, #00FFFF, #FF00FF, #FFFF00, #00FF00, #00FFFF);
        background-size: 200% auto;
        -webkit-background-clip: text; 
        -webkit-text-fill-color: transparent;
        filter: drop-shadow(0px 0px 8px rgba(255, 255, 255, 0.7));
        animation: textShine 3s linear infinite;
        margin-top: -10px;
        margin-bottom: 0px;
    }
    @keyframes textShine {
        to { background-position: 200% center; }
    }
    
    .bingo-subtitle { text-align: center; font-size: 1rem; color: #dddddd; margin-bottom: 10px; text-shadow: 1px 1px 3px rgba(0,0,0,0.8); }

    /* ==========================================
        🌟 手機 5x5 方格 (鎖定後的顯示用，整個網格由單一 HTML 輸出)
        格子之間的間距改用 margin，這樣每格的中心剛好落在 10%、30%、50%、70%、90%，
        連線的線條才能精準對齊。
        ========================================== */
    .grid-wrap {
        position: relative;
        max-width: 400px;
        margin: 0 auto;
    }

    .mobile-grid {
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 0;
    }
    
    .mobile-cell {
        margin: 3px;
        aspect-ratio: 1 / 1;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(255, 255, 255, 0.15);
        border: 1px solid rgba(255, 255, 255, 0.3);
        border-radius: 10px;
        color: white;
        font-size: 20px;
        font-weight: bold;
        backdrop-filter: blur(5px);
    }

    .mobile-cell.hit {
        background: linear-gradient(135deg, #ff4b4b, #d32f2f);
        border: 2px solid #ffeb3b;
        box-shadow: 0 4px 10px rgba(0,0,0,0.5);
    }

    /* 🟡 連線的明顯線條 */
    .win-line {
        position: absolute;
        z-index: 5;
        pointer-events: none;
        border-radius: 999px;
        background: #fff200;
        box-shadow: 0 0 0 2px rgba(0, 0, 0, 0.65), 0 0 14px 3px rgba(255, 242, 0, 0.85);
    }
    .win-line.row  { left: 2%; width: 96%; height: 7px; transform: translateY(-50%); }
    .win-line.col  { top: 2%; height: 96%; width: 7px; transform: translateX(-50%); }
    .win-line.diag { top: 50%; left: 50%; width: 135.7%; height: 7px; transform: translate(-50%, -50%) rotate(45deg); }
    .win-line.anti { top: 50%; left: 50%; width: 135.7%; height: 7px; transform: translate(-50%, -50%) rotate(-45deg); }

    /* ==========================================
        🌟 手機 5x5 輸入格 (填寫階段)
        ========================================== */
    div[data-testid="stForm"] div[data-testid="stHorizontalBlock"] {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 6px !important;
        max-width: 400px;
        margin: 0 auto 6px auto;
    }
    div[data-testid="stForm"] div[data-testid="stColumn"],
    div[data-testid="stForm"] div[data-testid="column"] {
        flex: 1 1 0 !important;
        width: 20% !important;
        min-width: 0 !important;
    }
    div[data-testid="stForm"] div[data-baseweb="input"],
    div[data-testid="stForm"] div[data-baseweb="base-input"] {
        background: rgba(255, 255, 255, 0.92) !important;
        border-radius: 10px !important;
    }
    div[data-testid="stForm"] div[data-baseweb="input"] {
        border: 2px solid rgba(255, 255, 255, 0.9) !important;
    }
    div[data-testid="stForm"] input {
        text-align: center !important;
        font-size: 18px !important;
        font-weight: bold !important;
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
        caret-color: #000000 !important;
        padding: 0.5rem 0 !important;
        min-width: 0 !important;
    }
    div[data-testid="stForm"] label p {
        color: #ffffff !important;
        font-weight: 700 !important;
    }
    div[data-testid="InputInstructions"] { display: none !important; }

    @media (max-width: 640px) {
        .block-container { padding-left: 0.8rem !important; padding-right: 0.8rem !important; }
        .mobile-cell { font-size: 18px; }
    }

    /* 📷 大螢幕右上角 QR Code */
    .qr-box {
        position: fixed; top: 14px; right: 18px; z-index: 9999;
        width: 170px; padding: 10px;
        background: #ffffff; border-radius: 14px;
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.6);
        text-align: center;
    }
    .qr-box img { width: 100%; display: block; image-rendering: pixelated; }
    .qr-caption { color: #111; font-size: 13px; font-weight: bold; margin-top: 6px; }

    /* 🎱 已開球數 */
    .count-badge {
        text-align: center; font-size: 1.5rem; font-weight: 800; color: #ffffff;
        margin: 2px 0 8px 0; text-shadow: 1px 1px 4px rgba(0, 0, 0, 0.9);
    }
    .count-badge span { color: #FFD700; font-size: 2.2rem; margin: 0 4px; }

    .machine-wrapper { display: flex; flex-direction: column; align-items: center; margin-top: 5px; }
    
    .ball-pool {
        position: relative; width: 380px; height: 380px;
        background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.15), rgba(0,0,0,0.6));
        border-radius: 50%; border: 12px solid #222;
        box-shadow: inset -20px -20px 40px rgba(0,0,0,0.9), 0 15px 35px rgba(0,0,0,0.7);
        overflow: hidden; display: flex; justify-content: center; align-items: center;
    }
    
    .bg-ball { 
        position: absolute; width: 40px; height: 40px; 
        border-radius: 50%; opacity: 0.85; 
        box-shadow: 2px 2px 5px rgba(0,0,0,0.6); 
    }
    
    .center-ball {
        z-index: 10; width: 180px; height: 180px;
        border-radius: 50%; display: flex; align-items: center; justify-content: center;
        font-size: 90px; font-weight: bold; color: white;
        opacity: 0; 
    }
    
    @keyframes popUp {
        0% { transform: scale(0); opacity: 0; }
        60% { transform: scale(1.3); opacity: 1; }
        100% { transform: scale(1.15); opacity: 1; }
    }
    
    @keyframes goldGlow {
        0% { box-shadow: 0 0 20px rgba(255, 215, 0, 0.6), inset -5px -5px 15px rgba(180, 120, 0, 0.8); }
        50% { box-shadow: 0 0 45px rgba(255, 223, 0, 1), 0 0 80px rgba(255, 165, 0, 0.8), inset -5px -5px 15px rgba(180, 120, 0, 0.8); }
        100% { box-shadow: 0 0 20px rgba(255, 215, 0, 0.6), inset -5px -5px 15px rgba(180, 120, 0, 0.8); }
    }

    .center-ball.golden {
        background: radial-gradient(circle at 35% 35%, #fffdf0 10%, #ffd700 50%, #ff8c00 90%);
        border: 6px solid #fff3cd; 
        color: #b71c1c;
        text-shadow: 2px 2px 5px rgba(255, 255, 255, 0.8);
        opacity: 1; 
        animation: popUp 0.6s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards, goldGlow 1.5s infinite ease-in-out;
    }
    
    @keyframes dropDown {
        0% { transform: scale(1.15); opacity: 1; }
        100% { transform: translateY(240px) scale(0.3); opacity: 0; }
    }
    .center-ball.dropping {
        background: radial-gradient(circle at 35% 35%, #fffdf0 10%, #ffd700 50%, #ff8c00 90%);
        border: 6px solid #fff3cd; 
        color: #b71c1c;
        box-shadow: 0 0 30px rgba(255, 215, 0, 0.8);
        animation: dropDown 0.8s cubic-bezier(0.55, 0.085, 0.68, 0.53) forwards !important; 
    }

    .pipe {
        width: 80px; height: 40px; 
        background: linear-gradient(to right, #111, #555, #111);
        border-left: 2px solid #777; border-right: 2px solid #777;
        box-shadow: inset 0px 10px 15px rgba(0,0,0,0.8); z-index: 5;
    }

    .ball-track {
        background: linear-gradient(to bottom, rgba(26, 27, 32, 0.8), rgba(38, 39, 48, 0.8)); padding: 12px;
        border-radius: 15px; border-top: 5px solid #000;
        box-shadow: inset 0px 15px 15px -10px rgba(0,0,0,0.8); min-height: 80px;
        display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-start;
        margin-top: 5px; 
        backdrop-filter: blur(5px); 
    }

    .track-ball {
        display: inline-flex; width: 50px; height: 50px;
        background: radial-gradient(circle at 35% 35%, #ffffff, #1f77b4 60%);
        border-radius: 50%; align-items: center; justify-content: center;
        font-size: 20px; font-weight: bold; color: #111; margin: 4px;
        box-shadow: 2px 4px 8px rgba(0,0,0,0.5);
    }
    
    div[data-testid="stButton"] {
        display: flex !important;
        justify-content: center !important;
        width: 100% !important;
        margin-top: 5px !important;
    }

    div[data-testid="stButton"] > button {
        background: linear-gradient(to bottom, #4CAF50, #2E7D32) !important;
        color: white !important;
        width: 450px !important;
        height: 95px !important;
        font-weight: 900 !important;
        border-radius: 45px !important;
        border: 3px solid #1B5E20 !important;
        box-shadow: 0 8px 20px rgba(0, 0, 0, 0.5) !important;
        cursor: pointer !important;
    }

    div[data-testid="stButton"] > button p {
        font-size: 38px !important;
        font-weight: 900 !important;
        color: white !important;
        line-height: 1 !important;
    }

    /* 🔄 「人數を更新」小按鈕 (需要 Streamlit 1.39 以上才會套用 key 樣式) */
    .st-key-refresh_presence div[data-testid="stButton"] { justify-content: flex-start !important; }
    .st-key-refresh_presence button {
        width: auto !important; height: 44px !important;
        padding: 0 18px !important; border-radius: 10px !important;
        background: rgba(255, 255, 255, 0.15) !important;
        border: 1px solid rgba(255, 255, 255, 0.4) !important;
        box-shadow: none !important;
    }
    .st-key-refresh_presence button p { font-size: 16px !important; font-weight: 700 !important; }

    /* ⚫ 自動抽球中：幸運ボタン 反黑 (無法按) */
    div[data-testid="stButton"] > button:disabled {
        background: linear-gradient(to bottom, #3a3a3a, #000000) !important;
        border-color: #000000 !important;
        box-shadow: none !important;
        cursor: not-allowed !important;
        opacity: 1 !important;
    }
    div[data-testid="stButton"] > button:disabled p {
        color: #8a8a8a !important;
    }

    @media (max-width: 640px) {
        div[data-testid="stButton"] > button,
        div[data-testid="stFormSubmitButton"] > button {
            width: 100% !important;
            max-width: 400px !important;
            height: 60px !important;
        }
        div[data-testid="stButton"] > button p,
        div[data-testid="stFormSubmitButton"] > button p {
            font-size: 22px !important;
        }
    }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 4. 判斷模式：主持人端 vs 手機端
# ==========================================
query_params = st.query_params
mode = query_params.get("mode", "host")

if mode == "player":
    # ==========================================
    # 📱 手機端：5x5 賓果卡
    # ==========================================
    
    if 'card_locked' not in st.session_state:
        st.session_state['card_locked'] = False
    if 'my_card_numbers' not in st.session_state:
        st.session_state['my_card_numbers'] = []
    st.session_state.setdefault("nick", "")        # 填寫欄位 (暱稱或卡號)
    st.session_state.setdefault("my_nick", "")     # 已鎖定的暱稱

    if "t_0" not in st.session_state:
        for i in range(25):
            st.session_state[f"t_{i}"] = ""

    # 🆔 辨識這支手機：重新整理、關掉再重新掃 QR 都能找回同一張卡
    # 優先順序：網址上的 pid → 瀏覽器 cookie → 產生新的
    def get_player_id():
        pid = st.session_state.get("pid")
        if is_valid_pid(pid):
            return pid
        candidate = st.query_params.get("pid", "")
        if not is_valid_pid(candidate):
            try:
                candidate = st.context.cookies.get("bingo_pid", "")
            except Exception:
                candidate = ""
        if not is_valid_pid(candidate):
            candidate = uuid.uuid4().hex
        st.session_state["pid"] = candidate
        st.query_params["pid"] = candidate   # 寫進網址，重新整理時還在
        return candidate

    pid = get_player_id()

    # 📶 上線打卡：每 5 秒更新一次，主持人端靠這個統計目前在線人數
    # (放在最前面，這樣「報名已截止」畫面的人也會被計入)
    def heartbeat():
        touch_presence(pid)

    if hasattr(st, "fragment"):
        st.fragment(run_every="5s")(heartbeat)()
    else:
        heartbeat()   # 舊版 Streamlit：只有載入頁面時會打卡

    current_round = load_game_state().get("round", "r0")
    saved_data = load_card_data(pid, current_round)

    if saved_data:
        # 伺服器上已有這位玩家本輪鎖定的卡片 → 直接進入鎖定畫面
        st.session_state['card_locked'] = True
        st.session_state['my_card_numbers'] = saved_data["numbers"]
        st.session_state['my_nick'] = saved_data.get("nick", "")
    elif st.session_state['card_locked']:
        # 先前鎖定過，但這一輪沒有這張卡 → 主持人已重置，回到填寫畫面
        st.session_state['card_locked'] = False
        st.session_state['my_card_numbers'] = []
        st.session_state['my_nick'] = ""
        st.session_state['balloons_done'] = False
        for i in range(25):
            st.session_state[f"t_{i}"] = ""

    st.markdown('<div class="bingo-title" style="font-size: 2rem;">マイ BINGO カード</div>', unsafe_allow_html=True)

    # 🚫 防呆：第一顆球開出後，還沒鎖定卡片的人就不能再加入
    # (否則可以照著大螢幕已開出的號碼填，直接連線)
    if not st.session_state['card_locked'] and len(load_game_state().get("drawn", [])) > 0:
        st.markdown("""
            <div style="background: rgba(255,255,255,0.15); padding: 18px; border-radius: 14px; text-align: center; border: 2px solid #ffeb3b; margin-top: 20px; color: #fff; font-weight: bold; line-height: 1.6;">
                ⏰ ゲームが開始しました。参加受付は終了いたしました。<br>
                <span style="font-size: 14px; font-weight: normal;">只有在第一顆球開出前鎖定卡片的人可以參加。<br>主持人重置遊戲後，這個畫面會自動回到填寫頁。</span>
            </div>
        """, unsafe_allow_html=True)

        # 主持人重置後自動回到填寫畫面
        def check_reset():
            if len(load_game_state().get("drawn", [])) == 0:
                st.rerun()

        if hasattr(st, "fragment"):
            st.fragment(run_every="3s")(check_reset)()
        else:
            st_autorefresh(interval=3000, limit=None, key="closed_sync")
        st.stop()
    
    if not st.session_state['card_locked']:
        st.markdown('<div class="bingo-subtitle">請在下方填入 25 個數字（1-99），鎖定後無法修改！</div>', unsafe_allow_html=True)
        
        if st.button("🎲 一括ランダム生成"):
            new_nums = random.sample(range(1, 100), 25)
            st.session_state['my_card_numbers'] = new_nums
            for i, n in enumerate(new_nums):
                st.session_state[f"t_{i}"] = str(n)
            st.rerun()

        st.write("")
        
        with st.form("bingo_form"):
            # 👤 暱稱或卡號 (中獎時主持人要靠這個辨識是誰)
            nick_val = st.text_input(
                "👤 ニックネーム / カード番号",
                key="nick",
                max_chars=20,
                placeholder="例：たろう / A-023",
            )

            form_vals = []
            for r in range(5):
                cols = st.columns(5, gap="small")
                for c in range(5):
                    idx = r * 5 + c
                    with cols[c]:
                        val = st.text_input(
                            f"cell_{idx}",
                            key=f"t_{idx}",
                            label_visibility="collapsed",
                        )
                        form_vals.append(val)
            
            st.write("")
            submitted = st.form_submit_button("🔒 数字確定 ＆ カードロック")

        # 🔢 只有 5x5 的數字格跳出數字鍵盤 (暱稱欄位不受影響)
        components.html("""
            <script>
            const applyNumeric = () => {
                const doc = window.parent.document;
                doc.querySelectorAll('div[data-testid="stForm"] div[data-testid="stHorizontalBlock"] input').forEach(el => {
                    el.setAttribute('inputmode', 'numeric');
                    el.setAttribute('pattern', '[0-9]*');
                    el.setAttribute('maxlength', '2');
                    el.setAttribute('autocomplete', 'off');
                });
            };
            applyNumeric();
            new MutationObserver(applyNumeric).observe(
                window.parent.document.body, { childList: true, subtree: true }
            );
            </script>
        """, height=0)
            
        if submitted:
            try:
                if not (nick_val or "").strip():
                    st.error("⚠️ ニックネームまたはカード番号を入力してください")
                    st.stop()
                if any(not v.strip() for v in form_vals):
                    st.error("⚠️ マスが埋まっていません！一括ランダム埋めをご利用ください。")
                    st.stop()
                int_vals = [int(v) for v in form_vals]
                if any(v < 1 or v > 99 for v in int_vals):
                    st.error("⚠️ 1〜99のみ有効")
                elif len(set(int_vals)) < 25:
                    st.error("‼️‼️ 数字の重複不可 ‼️‼️")
                else:
                    # 💾 存到伺服器；如果同一個人已經鎖過，以先前鎖定的為準，不能覆蓋
                    round_now = load_game_state().get("round", "r0")
                    existing = load_card_data(pid, round_now)
                    if existing:
                        int_vals = existing["numbers"]
                        nick_final = existing.get("nick", "")
                    else:
                        # 填寫期間第一顆球剛好開出 → 不能鎖定，回到「報名已截止」畫面
                        if len(load_game_state().get("drawn", [])) > 0:
                            st.rerun()
                        nick_final = nick_val.strip()
                        save_card(pid, round_now, int_vals, nick_final)
                    st.session_state['my_card_numbers'] = int_vals
                    st.session_state['my_nick'] = nick_final
                    st.session_state['card_locked'] = True
                    st.success("🎰 ゲーム開始 🎰")
                    st.rerun()
            except ValueError:
                st.error("⚠️ すべてのマスに正しい数字を入力してください")

    else:
        st.markdown('<div class="bingo-subtitle">ロックOK！あとは神頼み…？！ドキドキで結果を待とう！！🎯</div>', unsafe_allow_html=True)
        if st.session_state.get("my_nick"):
            st.markdown(
                f'<div class="bingo-subtitle">👤 {html_escape(st.session_state["my_nick"])}</div>',
                unsafe_allow_html=True,
            )
        st.write("")

        def render_live_card():
            state_data = load_game_state()
            if load_card(pid, state_data.get("round", "r0")) is None:
                st.rerun()   # 主持人已重置 → 整頁重跑，回到填寫畫面
            card_numbers = st.session_state['my_card_numbers']
            drawn_list = state_data.get("drawn", [])
            drawn_set = set(drawn_list)

            # 已完成的連線 → 在方格上方畫出明顯的線條
            completed_lines = get_completed_lines(card_numbers, drawn_set)
            bingo_lines = len(completed_lines)

            cells_html = ""
            for num in card_numbers:
                cls = "mobile-cell hit" if num in drawn_set else "mobile-cell"
                cells_html += f'<div class="{cls}">{num}</div>'
            st.markdown(
                f'<div class="grid-wrap"><div class="mobile-grid">{cells_html}</div>'
                f'{build_line_overlay(completed_lines)}</div>',
                unsafe_allow_html=True,
            )

            if 0 < bingo_lines < WIN_LINES:
                st.markdown(f"""
                    <div style="background: rgba(255,255,255,0.15); padding: 12px; border-radius: 12px; text-align: center; border: 2px solid #ffeb3b; margin-top: 15px; color: #fff; font-weight: bold;">
                        🔥 スゴい！すでに {bingo_lines} ライン！あと {WIN_LINES - bingo_lines} ラインで勝利だ‼️🔥
                    </div>
                """, unsafe_allow_html=True)

            if bingo_lines >= WIN_LINES:
                # 🎈 氣球只在「剛達成」時放一次，不會每次同步都重播
                if not st.session_state.get("balloons_done", False):
                    st.balloons()
                    st.session_state["balloons_done"] = True
                st.markdown(f"""
                    <div style="background: linear-gradient(135deg, #FFD700, #FF8C00); padding: 15px; border-radius: 15px; text-align: center; box-shadow: 0 10px 25px rgba(0,0,0,0.7); border: 3px solid #FFF; margin-top: 15px;">
                        <h1 style="color: #B71C1C; margin: 0; font-size: 26px; font-weight: 900;">🎉 やったー！BINGO成立 🎉</h1>
                        <p style="color: #fff; font-size: 15px; font-weight: bold; margin-top: 5px;">已達成 <span style="font-size: 22px; color: #FFF000;">{bingo_lines}</span> ライン達成！さあ、全力で「当たり！」叫んじゃおう！！</p>
                    </div>
                """, unsafe_allow_html=True)
            else:
                st.session_state["balloons_done"] = False   # 主持人重置後可再次觸發

            st.info(f"💡 大螢幕已開出 {len(drawn_list)} 顆球，畫面每 1.5 秒自動同步中！")

        # ⚡ 只讓這一小塊每 1.5 秒自己更新，不用整份程式重跑 (需要 Streamlit 1.37 以上)
        if hasattr(st, "fragment"):
            live_card = st.fragment(run_every="1.5s")(render_live_card)
        else:
            # 舊版 Streamlit 的備案：退回整頁自動重整
            st_autorefresh(interval=1500, limit=None, key="player_sync")
            live_card = render_live_card
        live_card()

    # 🍪 把玩家 ID 也記在瀏覽器 cookie 裡 (一天有效)
    # 這樣就算網址上的 pid 不見了 (例如重新掃 QR Code)，也能找回同一張卡
    components.html(f"""
        <script>
        try {{
            window.parent.document.cookie = "bingo_pid={pid}; path=/; max-age=86400; SameSite=Lax";
        }} catch (e) {{}}
        </script>
    """, height=0)

else:
    # ==========================================
    # 🖥️ 主持人端：大螢幕抽獎機
    # ==========================================
    st.markdown('<div class="bingo-title">🎉 2026 忘年会 BINGO 大抽選会 🎉</div>', unsafe_allow_html=True)
    st.markdown('<div class="bingo-subtitle">もしかして、今年の強運の持ち主はあなた！？ 🔎</div>', unsafe_allow_html=True)

    state_data = load_game_state()
    pool = state_data["pool"]
    drawn = state_data["drawn"]
    round_id = state_data.get("round", "r0")

    # 自動抽球相關設定的預設值 (必須在 toggle / slider 建立之前設定)
    st.session_state.setdefault("auto_draw", False)
    st.session_state.setdefault("auto_interval", 120)
    st.session_state.setdefault("winners_baseline", 0)
    st.session_state.setdefault("auto_stopped_by_winner", False)

    # 🏆 掃描所有已鎖定的卡片，算出目前的中獎者
    winners = get_winners(drawn, round_id)
    n_winners = len(winners)

    # 🛑 自動抽球中只要「中獎人數增加」就自動取消自動模式
    # (這段要放在 toggle 建立之前，才能直接改 session_state)
    if n_winners < st.session_state["winners_baseline"]:
        st.session_state["winners_baseline"] = n_winners        # 主持人重置後歸零
    if not st.session_state["auto_draw"]:
        st.session_state["winners_baseline"] = n_winners        # 手動模式下持續記錄目前人數
    elif n_winners > st.session_state["winners_baseline"]:
        st.session_state["auto_draw"] = False
        st.session_state["winners_baseline"] = n_winners
        st.session_state["auto_stopped_by_winner"] = True
    if st.session_state["auto_draw"]:
        st.session_state["auto_stopped_by_winner"] = False      # 重新開啟自動模式就清除提示

    # 📷 右上角 QR Code
    if "player_url" not in st.session_state:
        st.session_state["player_url"] = detect_player_url()

    qr_url = normalize_player_url(st.session_state.get("player_url", ""))
    if qr_url and segno is not None:
        qr_uri = segno.make(qr_url, error="m").png_data_uri(
            scale=6, border=1, dark="#000000", light="#ffffff"
        )
        st.markdown(
            f'<div class="qr-box"><img src="{qr_uri}" alt="QR Code">'
            f'<div class="qr-caption">📱 ビンゴカードスキャン</div></div>',
            unsafe_allow_html=True,
        )

    # 🎱 目前已開出的球數
    st.markdown(
        f'<div class="count-badge">🎱 排出濟<span>{len(drawn)}</span>個 残り<span>{len(pool)}</span>個</div>',
        unsafe_allow_html=True,
    )

    def render_machine(number="", state="idle"):
        wrapper_class = "machine-wrapper rolling" if state == "rolling" else "machine-wrapper"
        ball_class = "center-ball"
        if state == "golden": ball_class += " golden"
        elif state == "dropping": ball_class += " dropping"
        bg_balls_html = "".join([f'<div class="bg-ball b{i}"></div>' for i in range(1, 51)])
        return f'<div class="{wrapper_class}"><div class="ball-pool">{bg_balls_html}<div class="{ball_class}">{number}</div></div><div class="pipe"></div></div>'

    col_left, col_center, col_right = st.columns([1, 2, 1])

    with col_center:
        machine_placeholder = st.empty()
        machine_placeholder.markdown(render_machine("", "idle"), unsafe_allow_html=True)
        # 自動抽球中按鈕會反黑且不能按；有人中獎自動停止後會恢復成綠色
        draw_clicked = st.button(
            "🚀 幸運ボタン ",
            use_container_width=True,
            disabled=st.session_state["auto_draw"],
        )
        if st.session_state["auto_stopped_by_winner"]:
            st.success(f"🎉 当選者が出ました！自動抽球を停止しました（現在 {n_winners} 名）")

    # ⏰ 自動定時抽球
    # 用 st_autorefresh 當計時器：每隔「設定秒數」才會讓頁面重跑一次，
    # 並用它回傳的計數器判斷「這次重跑是不是計時器觸發的」。
    # 這樣按按鈕、拉滑桿等其他操作造成的重跑，都不會誤觸發抽球。
    auto_tick_trigger = False
    if st.session_state["auto_draw"]:
        interval_s = int(st.session_state["auto_interval"])
        tick_key = f"auto_tick_{interval_s}"
        last_key = f"auto_last_{interval_s}"

        tick_count = st_autorefresh(interval=interval_s * 1000, limit=None, key=tick_key)

        # 秒數改變時，清掉舊秒數留下的紀錄
        for k in [k for k in list(st.session_state.keys()) if k.startswith("auto_last_") and k != last_key]:
            del st.session_state[k]

        if last_key not in st.session_state:
            st.session_state[last_key] = tick_count   # 剛開啟時只記錄起點，不抽球
        if tick_count > st.session_state[last_key]:
            st.session_state[last_key] = tick_count
            auto_tick_trigger = True
    else:
        # 關閉時清掉紀錄，下次開啟才會從頭計時
        for k in [k for k in list(st.session_state.keys()) if k.startswith("auto_last_")]:
            del st.session_state[k]

    # 抽獎核心動畫
    def run_draw_animation(winner_num):
        machine_placeholder.markdown(render_machine("", "rolling"), unsafe_allow_html=True)
        time.sleep(2.5) 
            
        machine_placeholder.markdown(render_machine(winner_num, "golden"), unsafe_allow_html=True)

        # ✅ 金球出現之後才寫入共用狀態，手機端這時才會看到新號碼
        pool.remove(winner_num)
        drawn.append(winner_num)
        save_game_state(pool, drawn)

        time.sleep(1.0)
        
        machine_placeholder.markdown(render_machine(winner_num, "dropping"), unsafe_allow_html=True)
        time.sleep(0.5)

    if draw_clicked or auto_tick_trigger:
        if not pool:
            st.error(" 全部引き終わったよ！ ")
            if auto_tick_trigger:
                st.session_state["auto_draw"] = False   # 抽完了就自動關閉自動模式
        else:
            winner = random.choice(pool)   # 先決定號碼，但還不存檔
            run_draw_animation(winner)     # 金球出現後才會存檔
            st.rerun()

    # 歷史軌道
    track_html = '<div class="ball-track">'
    for num in reversed(drawn):
        track_html += f'<div class="track-ball">{num}</div>'
    track_html += '</div>'
    st.markdown(track_html, unsafe_allow_html=True)

    st.write("")
    col_set1, col_set2 = st.columns(2)
    with col_set1:
        with st.expander("⚙️ 管理画面"):
            if st.button("⚠️ リセット"):
                # 換一個新的輪次編號 → 所有手機上的舊卡片立刻失效，可以重新填寫
                save_game_state(list(range(1, 100)), [], round_id=uuid.uuid4().hex[:8])
                clear_all_cards()
                clear_presence()   # 連線中的手機約 5 秒內會重新打卡，人數就會回來
                st.session_state["winners_baseline"] = 0
                st.session_state["auto_stopped_by_winner"] = False
                st.rerun()
            
            st.write("---")
            st.subheader("⏱️ 自動サーブ")

            st.toggle("スタート", key="auto_draw")
            st.slider("時間設定", min_value=10, max_value=300, step=10, key="auto_interval")

            if st.session_state["auto_draw"]:
                st.markdown(f"""
                    <div style="color: #00FF00; font-weight: bold; margin-top: 10px;">
                        🤖 自動抽球中... (每 {st.session_state['auto_interval']} 秒抽一顆)
                    </div>
                """, unsafe_allow_html=True)

    with col_set2:
        with st.expander("📱 手機連線說明"):
            st.write("右上角的 QR Code 會自動帶入目前網址。如果掃出來的網址不對（例如在本機測試時），請在下方改成大家手機能連到的網址。")
            st.text_input("QR Code 網址", key="player_url", help="不用自己加 ?mode=player，系統會自動補上")
            if segno is None:
                st.warning("尚未安裝 segno，QR Code 無法產生。請在 requirements.txt 加上一行：segno")

    # 🏆 當選者リスト (預設收合，需要時才點開)
    with st.expander(f"🏆 当選者リスト（現在 {n_winners} 名）"):
        if winners:
            st.dataframe(
                [
                    {
                        "No.": i,
                        "ニックネーム / カード番号": w["nick"],
                        "達成した球": f"{w['ball_no']}個目",
                        "ライン数": w["lines"],
                    }
                    for i, w in enumerate(winners, start=1)
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.write("まだ当選者はいません。")

    # 👥 接続人数チェック (預設收合)：用來跟簽到人數做比對
    # 刻意不做自動更新：定時重跑可能打斷抽球動畫，所以改成按「更新」手動刷新
    # (按鈕在 fragment 裡，只會重跑這一小塊，不會影響抽球)
    _fragment = st.fragment if hasattr(st, "fragment") else (lambda f: f)

    @_fragment
    def presence_panel():
        online, total = get_presence_counts()
        locked = count_locked_cards(round_id)
        c1, c2, c3 = st.columns(3)
        c1.metric("🟢 オンライン中", f"{online} 名")
        c2.metric("🔒 カードロック済み", f"{locked} 名")
        c3.metric("📶 接続した端末（累計）", f"{total} 台")
        st.caption(
            f"直近 {ONLINE_WINDOW} 秒以内に通信があった端末を「オンライン」として数えています。"
            "リセットすると累計は 0 に戻り、接続中の端末は約 5 秒で再カウントされます。"
        )
        st.button("🔄 人数を更新", key="refresh_presence")

    with st.expander("👥 接続人数チェック（サインイン人数との照合用）"):
        presence_panel()
