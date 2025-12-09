import re
import asyncio
import traceback
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, Browser, BrowserContext
from pydantic import BaseModel

# Global state
class AppState:
    playwright = None
    browser = None
    semaphore = None

state = AppState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("LOG: Starting Playwright...")
    try:
        state.playwright = await async_playwright().start()
        # Launch options for Render/Docker
        state.browser = await state.playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080" # Force standard window size
            ],
            ignore_default_args=["--enable-automation"]
        )
        state.semaphore = asyncio.Semaphore(3)
        print("LOG: Playwright started successfully.")
        yield
    except Exception as e:
        print(f"LOG: Failed to start Playwright: {e}")
        raise e
    finally:
        print("LOG: Shutting down Playwright...")
        if state.browser:
            await state.browser.close()
        if state.playwright:
            await state.playwright.stop()
        print("LOG: Playwright shutdown complete.")

app = FastAPI(title="Codolio Scraper API", version="2.2.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UsernameRequest(BaseModel):
    username: str

# ----- CONSTANTS -----
CONTAINER_SELECTOR = "#contest_graph"
PLATFORMS = [
    ("LeetCode", "leetcode_rating"),
    ("CodeChef", "codechef_rating"),
    ("CodeForces", "codeforces_rating"),
    ("GeeksForGeeks", "GeeksForGeeks_rating"),
    ("AtCoder", "AtCoder_rating"),
    ("CodeStudio", "codestudio_rating")
]

X_STEPS = 220
Y_SWEEP_PIXELS = 80
Y_SWEEP_STEP = 12
EVENT_PAUSE = 0.005
CLICK_WAIT_TIMEOUT = 5.0

READ_PANEL_JS = """
(containerSelector) => {
  const root = document.querySelector(containerSelector);
  if (!root) return null;
  const headerBlock = root.querySelector('.flex.gap-10') || root.querySelector('div.flex.flex-col');
  if (!headerBlock) return null;
  const ratingDiv = headerBlock.querySelector('div.flex.flex-col') || headerBlock.children[0];
  const infoDiv   = headerBlock.querySelector('div.w-full') || headerBlock.children[1];
  if (!ratingDiv || !infoDiv) return null;
  let ratingSpan = ratingDiv.querySelector('span:nth-child(2)') || ratingDiv.querySelector('span');
  let ratingText = ratingSpan ? ratingSpan.innerText.trim() : '';
  const ps = infoDiv.querySelectorAll('p');
  let dateText = ps[0] ? ps[0].innerText.trim() : '';
  let contestText = ps[1] ? ps[1].innerText.trim() : '';
  let rankText = ps[2] ? ps[2].innerText.trim() : '';
  const rating = (ratingText || '').replace(/[^0-9]/g,'') || null;
  const rank   = (rankText || '').replace(/[^0-9]/g,'') || null;
  return {
    ratingText: ratingText || null,
    rating: rating ? parseInt(rating) : null,
    date: dateText || null,
    contestName: contestText || null,
    rankText: rankText || null,
    rank: rank ? parseInt(rank) : null
  };
}
"""

READ_TOOLTIPS_JS = """
() => {
  const out = { xaxis: null, tooltip: null };
  const x = document.querySelector('.apexcharts-xaxistooltip-text');
  if (x) out.xaxis = x.innerText.trim();
  const t = document.querySelector('.apexcharts-tooltip');
  if (t) out.tooltip = t.innerText.trim();
  return out;
}
"""

def try_parse_date(dstr):
    if not dstr or not isinstance(dstr, str): return None
    s = dstr.strip().replace("Sept ", "Sep ")
    fmts = ["%d %b %Y", "%d %B %Y", "%d %b, %Y", "%Y-%m-%d"]
    for f in fmts:
        try: return datetime.strptime(s, f).date()
        except: pass
    try:
        parts = s.split()
        if len(parts) == 3:
            day = int(parts[0]); month = parts[1]; year = int(parts[2])
            for f in ("%d %b %Y", "%d %B %Y"):
                try: return datetime.strptime(f"{day} {month} {year}", f).date()
                except: pass
    except: pass
    return None

async def dispatch_event_at(page, cx, cy):
    script = """
    ({cx, cy, containerSelector}) => {
      try {
        const el = document.elementFromPoint(cx, cy); 
        if (el) { 
            el.dispatchEvent(new PointerEvent('pointermove', {bubbles:true, clientX:cx, clientY:cy}));
            el.dispatchEvent(new MouseEvent('mousemove', {bubbles:true, clientX:cx, clientY:cy}));
        }
      } catch(e){}
      return true;
    }
    """
    await page.evaluate(script, {"cx": cx, "cy": cy, "containerSelector": CONTAINER_SELECTOR})

async def synthetic_svg_sweep(page):
    svg = await page.query_selector(f"{CONTAINER_SELECTOR} svg.apexcharts-svg") or await page.query_selector(f"{CONTAINER_SELECTOR} svg")
    if not svg: return []
    box = await svg.bounding_box()
    if not box: return []
    
    left, top, width, height = box["x"], box["y"], box["width"], box["height"]
    pad_x = max(4, width * 0.02)
    start_x = left + pad_x
    end_x = left + width - pad_x
    center_y = int(top + height * 0.5)
    half = Y_SWEEP_PIXELS // 2
    y_positions = [center_y + offset for offset in range(-half, half+1, Y_SWEEP_STEP)]
    
    snapshots = []
    for i in range(X_STEPS):
        t = i / (X_STEPS - 1) if X_STEPS > 1 else 0.5
        x = int(round(start_x + (end_x - start_x) * t))
        for y in y_positions:
            await dispatch_event_at(page, x, y)
            panel = await page.evaluate(READ_PANEL_JS, CONTAINER_SELECTOR)
            if panel and panel.get("contestName"):
                snapshots.append(panel)
                break
            tips = await page.evaluate(READ_TOOLTIPS_JS)
            txt = (tips.get("xaxis") or "") + "\n" + (tips.get("tooltip") or "")
            if txt.strip():
                snapshots.append({"raw_tooltip": txt.strip()})
                break
    return snapshots

def refine_points(raw_points):
    refined_map = {}
    for item in raw_points:
        if not item: continue
        contest = None; date_str = None; rating = None; rank = None
        if "contestName" in item and item.get("contestName"):
            contest = item.get("contestName"); date_str = item.get("date"); rating = item.get("rating"); rank = item.get("rank")
        else:
            raw = item.get("raw_tooltip", "")
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            for ln in lines:
                if "Rank" in ln:
                    try: rank = int("".join(ch for ch in ln if ch.isdigit()))
                    except: pass
                m = re.search(r"(\d{3,5})", ln)
                if m and not rating: rating = int(m.group(1))
                if "Contest" in ln or "contest" in ln: contest = ln
                parts = ln.split()
                if len(parts) >= 3 and parts[-1].isdigit() and len(parts[-1]) == 4:
                    ds = " ".join(parts[-3:])
                    if any(mm in ds for mm in ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec","Sept"]): date_str = ds
        
        parsed = try_parse_date(date_str) if date_str else None
        iso = parsed.isoformat() if parsed else None
        key = (iso if iso else date_str or "", (contest or "").strip())
        score = sum(1 for v in (rating, rank, date_str, contest) if v)
        existing = refined_map.get(key)
        if existing:
            existing_score = sum(1 for v in (existing.get("rating"), existing.get("rank"), existing.get("date"), existing.get("contestName")) if v)
            if score <= existing_score: continue
        
        refined_map[key] = {"rating": int(rating) if rating is not None else None, "date": date_str, "contestName": contest, "rank": int(rank) if rank is not None else None, "_iso": iso}
    
    items = list(refined_map.values())
    items.sort(key=lambda it: (0, it["_iso"]) if it.get("_iso") else (1, it.get("date") or ""), reverse=False)
    for it in items: it.pop("_iso", None)
    return items

async def click_platform_locator(page, platform_text):
    try:
        loc = page.locator(f"text={platform_text}").first
        if await loc.count() > 0 and await loc.is_visible():
            await loc.click(timeout=2000)
            return True
    except: pass
    script = """(platformText) => {
      const nodes = Array.from(document.querySelectorAll('*'));
      for (const n of nodes) {
        if (n.innerText && n.innerText.trim().toLowerCase().includes(platformText.toLowerCase())) {
          n.click(); return true; 
        }
      }
      return false;
    }"""
    try: return await page.evaluate(script, platform_text)
    except: return False

async def wait_for_panel_change(page, old_snapshot, timeout=CLICK_WAIT_TIMEOUT):
    old_date = old_snapshot.get("date") if old_snapshot else None
    old_contest = old_snapshot.get("contestName") if old_snapshot else None
    fn = """(oldDate, oldContest, sel) => { 
        try { 
            const root = document.querySelector(sel); 
            if(!root) return false; 
            const headerBlock = root.querySelector('.flex.gap-10') || root.querySelector('div.flex.flex-col'); 
            if(!headerBlock) return false; 
            const infoDiv = headerBlock.querySelector('div.w-full') || headerBlock.children[1]; 
            if(!infoDiv) return false; 
            const ps = infoDiv.querySelectorAll('p'); 
            const dateText = ps[0] ? ps[0].innerText.trim() : null; 
            const contestText = ps[1] ? ps[1].innerText.trim() : null; 
            if(!oldDate && !oldContest) return !!(dateText || contestText); 
            return (dateText !== oldDate) || (contestText !== oldContest);
        } catch(e){return false;} 
    }"""
    try:
        await page.wait_for_function(fn, arg=(old_date, old_contest, CONTAINER_SELECTOR), timeout=int(timeout*1000))
        return True
    except: return False

async def get_page(context: BrowserContext):
    page = await context.new_page()
    # UNBLOCK STYLESHEETS: Removed "stylesheet" and "font" from blocked list.
    # Block only very heavy things.
    await page.route("**/*", lambda route: route.abort() 
                     if route.request.resource_type in ["image", "media"] 
                     else route.continue_())
    return page

async def scrape_codolio(username: str):
    if not state.browser: raise HTTPException(status_code=500, detail="Browser not initialized")
    print(f"LOG: Scraping Codolio for {username}")
    
    url = f"https://codolio.com/profile/{username}/problemSolving"
    async with state.semaphore:
        context = await state.browser.new_context(
            viewport={"width": 1920, "height": 1080}, # Restored full HD for safety
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await get_page(context)
        
        try:
            print("LOG: Navigating to page...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Better waiting
            print("LOG: Waiting for Total Questions...")
            try:
                # Wait for the label
                await page.wait_for_selector("text=Total Questions", timeout=20000)
                # Wait a bit more for numbers to bloom
                await asyncio.sleep(2.0)
            except:
                print("LOG: Timeout waiting for 'Total Questions' selector")
            
            data = {"basicStats": {}, "problemsSolved": {}, "contestRankings": {}, "heatmap": []}

            # Helper with logging
            async def get_text_xpath(xpath):
                try:
                    loc = page.locator(f"xpath={xpath}").first
                    if await loc.count() > 0:
                        txt = (await loc.inner_text()).strip()
                        # Simple check to see if we got empty string
                        if not txt: return "0"
                        return txt
                except: pass
                return "0"

            # 1. Basic Stats
            data["basicStats"]["total_questions"] = await get_text_xpath("//div[div[contains(text(), 'Total Questions')]]/span[contains(@class, 'text-5xl')]")
            print(f"LOG: Total Questions found: {data['basicStats']['total_questions']}")
            
            # If 0, double check with evaluate (fallback)
            if data["basicStats"]["total_questions"] == "0":
                 # Fallback: maybe the class changed or structure differs slightly
                 print("LOG: Retry finding Total Questions via broader search")
                 val = await page.evaluate("""() => {
                    const el = Array.from(document.querySelectorAll('div')).find(x => x.innerText === 'Total Questions');
                    return el ? el.nextElementSibling?.innerText.trim() : '0';
                 }""")
                 if val: data["basicStats"]["total_questions"] = val

            data["basicStats"]["total_active_days"] = await get_text_xpath("//div[div[contains(text(), 'Total Active Days')]]/span[contains(@class, 'text-5xl')]")
            data["basicStats"]["total_submissions"] = await get_text_xpath("//div[contains(@class, 'flex gap-1 text-center')]/span[contains(text(), 'Submissions')]/following-sibling::span")
            data["basicStats"]["max_streak"] = await get_text_xpath("//span[contains(text(), 'Max.Streak')]/following-sibling::span")
            data["basicStats"]["current_streak"] = await get_text_xpath("//span[contains(text(), 'Current.Streak')]/following-sibling::span")
            data["basicStats"]["total_contests"] = await get_text_xpath("//div[div[contains(text(), 'Total Contests')]]/span[contains(@class, 'text-6xl')]")
            data["basicStats"]["awards"] = await get_text_xpath("//h3[contains(text(), 'Awards')]/following-sibling::span")

            # 2. Problems Solved
            async def get_stat_by_label(label):
                return await page.evaluate(f"""() => {{
                    const labels = Array.from(document.querySelectorAll('div, span, p'));
                    const target = labels.find(el => el.innerText.trim() === '{label}');
                    if (!target) return '0';
                    let p = target.parentElement;
                    for(let i=0; i<4; i++) {{
                        if(!p) break;
                        const num = p.querySelector('span.text-2xl');
                        if(num) return num.innerText.trim();
                        p = p.parentElement;
                    }}
                    return '0';
                }}""")

            data["problemsSolved"]["fundamentals"] = await get_stat_by_label("Fundamentals")
            data["problemsSolved"]["dsa"] = await get_stat_by_label("DSA")

            for level in ["Easy", "Medium", "Hard"]:
                data["problemsSolved"][level.lower()] = await page.evaluate(f"""() => {{
                    const el = Array.from(document.querySelectorAll('div')).find(x => x.innerText === '{level}');
                    return el ? el.nextElementSibling?.innerText.trim() : '0';
                }}""") or "0"

            cp_text = await get_text_xpath("//div[contains(text(), 'Competitive Programming')]/following-sibling::div")
            data["problemsSolved"]["competitive_programming"] = cp_text.split('\n')[0].strip() if cp_text else "0"

            data["problemsSolved"]["codechef"] = await get_text_xpath("//div[div[contains(text(), 'Codechef')]]/span")
            data["problemsSolved"]["codeforces"] = await get_text_xpath("//div[div[contains(text(), 'Codeforces')]]/span")
            data["problemsSolved"]["hackerrank"] = await get_text_xpath("//div[div[contains(text(), 'HackerRank')]]/span")
            data["problemsSolved"]["geeksforgeeks"] = await get_text_xpath("//div[div[contains(text(), 'GFG')]]/span")

            # 3. Contest Rankings
            data["contestRankings"]["total_contests"] = data["basicStats"]["total_contests"]
            
            data["contestRankings"]["leetcode_total_contest"] = await get_text_xpath("//button[div[span[text()='LeetCode']]]/span[last()]")
            data["contestRankings"]["codechef_total_contest"] = await get_text_xpath("//button[div[span[text()='CodeChef']]]/span[last()]")
            data["contestRankings"]["codeforces_total_contest"] = await get_text_xpath("//button[div[span[text()='CodeForces']]]/span[last()]")
            data["contestRankings"]["GeeksForGeeks_total_contest"] = await get_text_xpath("//button[div[span[text()='GeeksForGeeks']]]/span[last()]")
            data["contestRankings"]["AtCoder_total_contest"] = await get_text_xpath("//button[div[span[text()='AtCoder']]]/span[last()]")
            data["contestRankings"]["codestudio_total_contest"] = await get_text_xpath("//button[div[span[text()='CodeStudio']]]/span[last()]")

            # Ratings
            data["contestRankings"]["leetcode_current_rating"] = await get_text_xpath("//div[div[text()='LEETCODE']]//h3")
            data["contestRankings"]["leetcode_max-rating"] = await page.evaluate("""() => {
                const el = Array.from(document.querySelectorAll('div')).find(x => x.innerText === 'LEETCODE');
                if(!el) return '0';
                const maxSpan = Array.from(el.parentElement.querySelectorAll('span')).find(s => s.innerText.includes('max :'));
                return maxSpan ? maxSpan.innerText.replace('max :', '').replace('(', '').replace(')', '').trim() : '0';
            }""")
            
            def get_rating_snippet(pname):
                return f"""() => {{
                    const el = Array.from(document.querySelectorAll('div')).find(x => x.innerText === '{pname}');
                    if(!el) return '0';
                    const maxSpan = Array.from(el.parentElement.querySelectorAll('span')).find(s => s.innerText.includes('max :'));
                    return maxSpan ? maxSpan.innerText.replace('max :', '').replace('(', '').replace(')', '').trim() : '0';
                }}"""

            data["contestRankings"]["codechef_current_rating"] = await get_text_xpath("//div[div[text()='CODECHEF']]//h3")
            data["contestRankings"]["codechef_max-rating"] = await page.evaluate(get_rating_snippet('CODECHEF'))
            data["contestRankings"]["codeforces_current_rating"] = await get_text_xpath("//div[div[text()='CODEFORCES']]//h3")
            data["contestRankings"]["codeforces_max-rating"] = await page.evaluate(get_rating_snippet('CODEFORCES'))
            data["contestRankings"]["GeeksForGeeks_current_rating"] = await get_text_xpath("//div[div[text()='GEEKSFORGEEKS']]//h3")
            data["contestRankings"]["GeeksForGeeks_max-rating"] = await page.evaluate(get_rating_snippet('GEEKSFORGEEKS'))
            data["contestRankings"]["AtCoder_current_rating"] = await get_text_xpath("//div[div[text()='ATCODER']]//h3")
            data["contestRankings"]["AtCoder_max-rating"] = await page.evaluate(get_rating_snippet('ATCODER'))
            data["contestRankings"]["codestudio_current_rating"] = await get_text_xpath("//div[div[text()='CODESTUDIO']]//h3")
            data["contestRankings"]["codestudio_max-rating"] = await page.evaluate(get_rating_snippet('CODESTUDIO'))

            # Heatmap
            try:
                data["heatmap"] = await page.eval_on_selector_all(
                    "svg.react-calendar-heatmap rect",
                    """(rects) => rects.map(r => {
                        const tooltip = r.getAttribute("data-tooltip-content") || "";
                        const match = tooltip.match(/(\\d+)\\s+submissions\\s+on\\s+(\\d{2}\\/\\d{2}\\/\\d{4})/i);
                        if (match) {
                            return { date: match[2], submissions: parseInt(match[1], 10), colorClass: r.getAttribute("class") || "" };
                        }
                        return null;
                    }).filter(x => x !== null)"""
                )
            except: data["heatmap"] = []

            # 4. Detailed Contest History
            for platform_name, key in PLATFORMS:
                clicked = await click_platform_locator(page, platform_name)
                if clicked:
                    old_panel = await page.evaluate(READ_PANEL_JS, CONTAINER_SELECTOR)
                    await wait_for_panel_change(page, old_panel)
                    snapshots = await synthetic_svg_sweep(page)
                    refined = refine_points(snapshots)
                    data["contestRankings"][key] = refined
                else:
                    data["contestRankings"][key] = []

        except Exception as e:
            traceback.print_exc()
            await context.close()
            raise HTTPException(status_code=500, detail=f"Error extracting data: {str(e)}")
        
        await context.close()
        return data


@app.get("/")
async def root():
    return {"message": "Codolio Scraper API", "status": "active", "concurrency_limit": 3}

@app.get("/health")
async def health_check():
    # Don't check browser instance here to avoid race conditions or overhead.
    # Just return healthy.
    return {"status": "healthy"}

@app.get("/codolio/{username}")
async def get_profile(username: str):
    if not username.strip(): raise HTTPException(status_code=400, detail="Username required")
    return {"success": True, "username": username, "data": await scrape_codolio(username.strip())}

@app.post("/codolio")
async def post_profile(request: UsernameRequest):
    if not request.username.strip(): raise HTTPException(status_code=400, detail="Username required")
    return {"success": True, "username": request.username, "data": await scrape_codolio(request.username.strip())}

async def scrape_generic_profile(username: str, platform: str):
    if not state.browser: raise HTTPException(status_code=500, detail="Browser not initialized")
    url = f"https://codolio.com/profile/{username}/problemSolving/{platform}"
    async with state.semaphore:
        context = await state.browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await get_page(context)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try: await page.wait_for_selector("text=Total Questions", timeout=20000)
            except: pass 
            await asyncio.sleep(2.0) 

            data = {"basicStats": {}, "problemsSolved": {}, "heatmap": []}
            async def get_text_xpath(xpath):
                try: 
                    loc = page.locator(f"xpath={xpath}").first
                    if await loc.count() > 0: return (await loc.inner_text()).strip()
                except: pass
                return "0"
            
            data["basicStats"]["total_questions"] = await get_text_xpath("//div[div[contains(text(), 'Total Questions')]]/span[contains(@class, 'text-5xl')]")
            data["basicStats"]["total_active_days"] = await get_text_xpath("//div[div[contains(text(), 'Total Active Days')]]/span[contains(@class, 'text-5xl')]")
            
            try:
                data["heatmap"] = await page.eval_on_selector_all(
                    "svg.react-calendar-heatmap rect",
                    """(rects) => rects.map(r => {
                        const tooltip = r.getAttribute("data-tooltip-content") || "";
                        const match = tooltip.match(/(\\d+)\\s+submissions\\s+on\\s+(\\d{2}\\/\\d{2}\\/\\d{4})/i);
                        return match ? { date: match[2], submissions: parseInt(match[1], 10) } : null;
                    }).filter(x => x !== null)"""
                )
            except: data["heatmap"] = []
            
            data["problemsSolved"]["total_solved"] = await get_text_xpath("//div[contains(@class, 'absolute inset-0')]/span[contains(@class, 'text-2xl')]")
            for level in ["Easy", "Medium", "Hard"]:
                data["problemsSolved"][level.lower()] = await get_text_xpath(f"//div[div[contains(text(), '{level}')]]/span")

        except Exception as e:
            traceback.print_exc()
            await context.close()
            raise HTTPException(status_code=500, detail=f"Error extracting data: {str(e)}")
        
        await context.close()
        return data

@app.get("/tuf/{username}")
async def get_tuf_profile(username: str):
    return {"success": True, "username": username, "data": await scrape_generic_profile(username.strip(), "tuf")}
@app.get("/codestudio/{username}")
async def get_codestudio_profile(username: str):
    return {"success": True, "username": username, "data": await scrape_generic_profile(username.strip(), "codestudio")}
@app.get("/interviewbit/{username}")
async def get_interviewbit_profile(username: str):
    return {"success": True, "username": username, "data": await scrape_generic_profile(username.strip(), "interviewbit")}
@app.get("/geeksforgeeks/{username}")
async def get_geeksforgeeks_profile(username: str):
    return {"success": True, "username": username, "data": await scrape_generic_profile(username.strip(), "geeksforgeeks")}

async def scrape_contest_platform(username: str, platform: str):
    if not state.browser: raise HTTPException(status_code=500, detail="Browser not initialized")
    url = f"https://codolio.com/profile/{username}/problemSolving/{platform}"
    
    async with state.semaphore:
        context = await state.browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await get_page(context)
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try: await page.wait_for_selector("text=Total Questions", timeout=20000)
            except: pass 
            await asyncio.sleep(2.0)

            data = {"basicStats": {}, "problemsSolved": {}, "contestRankings": {}, "heatmap": []}
            async def get_text_xpath(xpath):
                try: 
                    loc = page.locator(f"xpath={xpath}").first
                    if await loc.count() > 0: return (await loc.inner_text()).strip()
                except: pass
                return "0"
            
            data["basicStats"]["total_questions"] = await get_text_xpath("//div[div[contains(text(), 'Total Questions')]]/span[contains(@class, 'text-5xl')]")
            data["basicStats"]["total_active_days"] = await get_text_xpath("//div[div[contains(text(), 'Total Active Days')]]/span[contains(@class, 'text-5xl')]")
            
            try:
                data["heatmap"] = await page.eval_on_selector_all(
                    "svg.react-calendar-heatmap rect",
                    """(rects) => rects.map(r => {
                        const tooltip = r.getAttribute("data-tooltip-content") || "";
                        const match = tooltip.match(/(\\d+)\\s+submissions\\s+on\\s+(\\d{2}\\/\\d{2}\\/\\d{4})/i);
                        return match ? { date: match[2], submissions: parseInt(match[1], 10) } : null;
                    }).filter(x => x !== null)"""
                )
            except: data["heatmap"] = []
            
            data["problemsSolved"]["total_solved"] = await get_text_xpath("//div[contains(@class, 'absolute inset-0')]/span[contains(@class, 'text-2xl')]")
            for level in ["Easy", "Medium", "Hard"]:
                data["problemsSolved"][level.lower()] = await get_text_xpath(f"//div[div[contains(text(), '{level}')]]/span")
            
            rating = await get_text_xpath("//div[span[contains(text(), 'Rating')]]/span[contains(@class, 'text-base')]")
            if rating == "0": rating = await get_text_xpath("//h3[contains(@class, 'text-4xl font-bold')]")
            data["contestRankings"]["rating"] = rating
            
            content = await page.content()
            max_rating_match = re.search(r'\(max\s*:\s*(\d+)\)', content)
            data["contestRankings"]["maxRating"] = max_rating_match.group(1) if max_rating_match else "0"
            data["contestRankings"]["total_contests"] = await get_text_xpath("//div[h3[contains(text(), 'Total Contests')]]/span")
            
            if platform == "codeforces":
                contest_level = await get_text_xpath("//h2[contains(@class, 'text-3xl')]")
                if contest_level and contest_level != "0":
                    data["contestRankings"]["contestLevel"] = contest_level
        except Exception as e:
            traceback.print_exc()
            await context.close()
            raise HTTPException(status_code=500, detail=f"Error extracting data: {str(e)}")
        
        await context.close()
        return data

@app.get("/leetcode/{username}")
async def get_leetcode_profile(username: str):
    return {"success": True, "username": username, "data": await scrape_contest_platform(username.strip(), "leetcode")}
