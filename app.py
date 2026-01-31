import os
import sys

# --- 1. ENVIRONMENT CONFIGURATION ---
# Check if running on Render (Production) or Local
IS_RENDER = os.environ.get('RENDER') == 'True'

# If on Render, apply network patches immediately
if IS_RENDER:
    import eventlet
    eventlet.monkey_patch()

import asyncio
import random
import logging
import json
import threading
from playwright.async_api import async_playwright
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# --- 2. SETTINGS & CREDENTIALS ---
# Load credentials from Environment Variables (Render) or Config (Local)
try:
    import config
    # Fallback to config.py if env vars are missing
    DEFAULT_USER = os.environ.get('INSTAGRAM_USERNAME', getattr(config, 'INSTAGRAM_USERNAME', ''))
    DEFAULT_PASS = os.environ.get('INSTAGRAM_PASSWORD', getattr(config, 'INSTAGRAM_PASSWORD', ''))
except ImportError:
    DEFAULT_USER = os.environ.get('INSTAGRAM_USERNAME', '')
    DEFAULT_PASS = os.environ.get('INSTAGRAM_PASSWORD', '')

# Helper to get the JSON cookies string from Env
SESSION_COOKIES_ENV = os.environ.get('SESSION_COOKIES') 

# Bot Configuration
MAX_DAILY_FOLLOWS = 50
HASHTAGS_TO_SEARCH = ["photography", "nature", "travel", "art", "fitness", "tech"]

# --- 3. LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==========================================
# 4. INSTAGRAM BOT LOGIC
# ==========================================
class InstagramBot:
    def __init__(self, user_data, socketio=None):
        self.username = user_data.get('username')
        self.password = user_data.get('password')
        self.socketio = socketio 
        
        # Dynamic Target
        self.target_follows = int(user_data.get('target_follows', MAX_DAILY_FOLLOWS))
        self.followed_today_count = 0
        
        # Local Cookie File Path (Used on Local, or as temp on Render)
        if IS_RENDER:
             self.cookie_file = f"/tmp/cookies_{self.username}.json"
        else:
             self.cookie_file = f"cookies_{self.username}.json"

        self.browser = None
        self.context = None
        self.page = None

    def web_log(self, msg, level="info"):
        """Logs to console and sends real-time updates to UI"""
        formatted_msg = f"[{self.username}] {msg}"
        print(formatted_msg) # Shows in Render Logs
        if self.socketio:
            self.socketio.emit('log_update', {'msg': formatted_msg})

    async def start(self, playwright):
        self.web_log(f"🚀 Launching Browser (Render Mode: {IS_RENDER})...")
        
        # --- SMART LAUNCH ARGUMENTS ---
        launch_args = ["--disable-notifications", "--start-maximized"]
        
        if IS_RENDER:
            # Render-Specific Optimization
            launch_args.extend([
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process"
            ])
            headless_mode = True
        else:
            # Local Mode: Show browser so you can watch
            headless_mode = False 

        self.browser = await playwright.chromium.launch(
            headless=headless_mode,
            args=launch_args
        )
        
        self.context = await self.browser.new_context(
            no_viewport=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        )
        
        # --- HYBRID COOKIE LOADING ---
        cookies_loaded = False
        
        # 1. RENDER: Try loading from Environment Variable JSON
        if IS_RENDER and SESSION_COOKIES_ENV:
            try:
                cookies = json.loads(SESSION_COOKIES_ENV)
                await self.context.add_cookies(cookies)
                self.web_log("🍪 SUCCESS: Loaded cookies from Render Environment!")
                cookies_loaded = True
            except Exception as e:
                self.web_log(f"⚠️ ENV COOKIE ERROR: {e}", "warn")

        # 2. LOCAL: Try loading from local file
        if not cookies_loaded and os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, 'r') as f:
                    cookies = json.load(f)
                    await self.context.add_cookies(cookies)
                self.web_log("🍪 SUCCESS: Loaded cookies from local file.")
            except Exception as e:
                self.web_log(f"⚠️ FILE COOKIE ERROR: {e}", "warn")

        # --- RESOURCE BLOCKER (Only on Render for Speed) ---
        if IS_RENDER:
            await self.context.route("**/*", lambda route: route.abort() 
                if route.request.resource_type in ["image", "media", "font"] 
                else route.continue_())

        self.page = await self.context.new_page()
        return True

    async def login(self):
        self.web_log("🌍 Navigating to Instagram...")
        try:
            await self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            
            # --- 20-ATTEMPT HOME VERIFICATION LOOP (RESTORED) ---
            # Checks every 5 seconds for a total of 100 seconds
            for attempt in range(1, 21):
                self.web_log(f"⏳ Verifying session (Attempt {attempt}/20)...")
                await asyncio.sleep(5)
                
                # Check for indicators of a logged-in session
                if await self.page.locator('svg[aria-label="Home"]').is_visible() or \
                   await self.page.get_by_text("Search").is_visible() or \
                   await self.page.get_by_text("Not Now").is_visible():
                    self.web_log("✅ LOGIN SUCCESS: Session is valid.")
                    return True
            
            # --- FALLBACK: MANUAL LOGIN ---
            self.web_log("⚠️ Cookies expired or missing. Attempting password login...")
            await self.page.goto("https://www.instagram.com/accounts/login/")
            await asyncio.sleep(3)
            
            if not self.username or not self.password:
                self.web_log("❌ FAIL: No Username/Password provided for fallback login.")
                return False

            await self.page.fill('input[name="username"]', self.username)
            await self.page.fill('input[name="password"]', self.password)
            await self.page.click('button[type="submit"]')
            
            await self.page.wait_for_selector('svg[aria-label="Home"]', timeout=40000)
            
            # Save new cookies to file (Persists on Local, Temporary on Render)
            cookies = await self.context.cookies()
            with open(self.cookie_file, 'w') as f:
                json.dump(cookies, f)
            
            self.web_log("✅ Manual Login Successful.")
            return True
            
        except Exception as e:
            self.web_log(f"❌ Login failed: {str(e)[:50]}", "warn")
            return False

    async def search_hashtag(self, hashtag):
        self.web_log(f"🔎 Searching: #{hashtag}")
        try:
            await self.page.goto(f"https://www.instagram.com/explore/tags/{hashtag}/", wait_until="domcontentloaded")
            await self.page.wait_for_selector('div._aagu', timeout=30000)
            await self.page.mouse.wheel(0, 2000)
            await asyncio.sleep(2)
            
            links = await self.page.locator('a:has(div._aagu)').evaluate_all(
                "els => els.map(el => el.getAttribute('href'))"
            )
            unique_urls = [f"https://www.instagram.com{l}" for l in list(dict.fromkeys(links)) if "/p/" in l]
            return unique_urls[:10]
        except Exception as e:
            self.web_log(f"⚠️ Search failed: {str(e)[:30]}", "warn")
            return []

    async def process_post(self, post_url):
        self.web_log(f"📸 Processing: {post_url.split('/')[-2]}")
        try:
            await self.page.goto(post_url, wait_until="domcontentloaded")
            await self.page.wait_for_selector('div._aagu', timeout=20000)
            await asyncio.sleep(random.uniform(2, 4))

            # --- 1. OPEN PROFILE ---
            username_selector = 'span._ap3a._aaco._aacw._aacx._aad7._aade'
            user_trigger = self.page.locator(username_selector).last

            if await user_trigger.is_visible():
                target_user = await user_trigger.inner_text()
                await user_trigger.click()
                
                try:
                    await self.page.wait_for_selector('header', timeout=8000)
                except: pass 

                # --- 2. OPEN FOLLOWERS MODAL ---
                try:
                    followers_btn = self.page.locator(f'a[href="/{target_user}/followers/"]').first
                    await followers_btn.click(force=True)
                    await self.page.wait_for_selector('div[role="dialog"], div._aano', timeout=10000)
                    await asyncio.sleep(3)
                except:
                    self.web_log(f"🔒 Could not open followers list.")
                    return

                # --- 3. FOLLOW LOOP ---
                self.web_log("🏃 Analyzing followers...")
                follow_selector = 'div[role="dialog"] button >> text="Follow"'
                scroll_container = self.page.locator('div._aano').first

                while self.followed_today_count < self.target_follows:
                    follow_buttons = self.page.locator(follow_selector)
                    count = await follow_buttons.count()

                    if count == 0:
                        try: await scroll_container.evaluate('el => el.scrollTop += 650')
                        except: await self.page.mouse.wheel(0, 650)
                        await asyncio.sleep(2)
                        continue

                    processed_batch = 0
                    for i in range(count):
                        if self.followed_today_count >= self.target_follows: break
                        if processed_batch >= 4: break

                        btn = follow_buttons.nth(i)
                        try:
                            await btn.scroll_into_view_if_needed(timeout=3000)
                            await asyncio.sleep(1)
                            await btn.click(force=True, timeout=5000)
                            
                            self.followed_today_count += 1
                            self.web_log(f"✅ Followed ({self.followed_today_count}/{self.target_follows})")
                            
                            # Sleep logic: Fast on Local, Fast on Render
                            await asyncio.sleep(random.uniform(2, 5))
                            processed_batch += 1
                        except: continue

                    try: await scroll_container.evaluate('el => el.scrollTop += 850')
                    except: await self.page.mouse.wheel(0, 850)
                    await asyncio.sleep(3)

                await self.page.keyboard.press("Escape")

        except Exception as e:
            self.web_log(f"⚠️ Post Error: {str(e)[:30]}", "warn")

    async def close(self):
        if self.browser: 
            await self.browser.close()
            self.web_log("🔒 Browser closed.")


# ==========================================
# 5. FLASK & THREADING
# ==========================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret-key-2024'

# Auto-switch async mode based on Environment
async_mode = 'eventlet' if IS_RENDER else 'threading'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode)

async def run_worker(user_data, socketio_instance):
    async with async_playwright() as playwright:
        bot = InstagramBot(user_data, socketio_instance)
        if await bot.start(playwright):
            try:
                if await bot.login():
                    hashtags = list(HASHTAGS_TO_SEARCH)
                    random.shuffle(hashtags)
                    
                    for tag in hashtags:
                        if bot.followed_today_count >= bot.target_follows: break
                        urls = await bot.search_hashtag(tag)
                        for url in urls:
                            if bot.followed_today_count >= bot.target_follows: break
                            await bot.process_post(url)
                            
                    bot.web_log(f"🏁 Task Completed. Total Follows: {bot.followed_today_count}")
            except Exception as e:
                bot.web_log(f"❌ Critical Error: {e}")
            finally:
                await bot.close()

def start_background_loop(user_data):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_worker(user_data, socketio))
    loop.close()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('start_bot')
def handle_start(data):
    # Prioritize form data, fallback to Env/Default
    username = data.get('username') or DEFAULT_USER
    password = data.get('password') or DEFAULT_PASS
    
    try:
        target = int(data.get('target_follows', 10))
    except ValueError:
        target = 10
        
    user_data = {
        'username': username,
        'password': password,
        'target_follows': target
    }
    
    emit('log_update', {'msg': f"🚀 Server: Starting bot for {username}..."})
    
    # Run in background
    t = threading.Thread(target=start_background_loop, args=(user_data,))
    t.start()

if __name__ == "__main__":
    # Render assigns a random port to PORT env var. Local defaults to 5000.
    port = int(os.environ.get("PORT", 5000))
    print(f"🌍 Server running on Port {port} (Render Mode: {IS_RENDER})")
    socketio.run(app, host='0.0.0.0', port=port)