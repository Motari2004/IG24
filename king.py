import asyncio
import random
import logging
import sys
import os
import json
from playwright.async_api import async_playwright
import config

# --- Logging Setup ---
logging.basicConfig(
    level=config.LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class InstagramBot:
    def __init__(self, user_data, socketio=None):
        self.username = user_data['username']
        self.password = user_data['password']
        self.socketio = socketio 
        self.cookie_file = f"cookies_{self.username}.json"
        self.followed_today_count = 0
        self.session_batch_count = 0 
        self.browser = None
        self.context = None
        self.page = None
        self.logger = logging.getLogger("bot")

    def web_log(self, msg, level="info"):
        """Logs to console and sends to index.html if socketio is provided"""
        formatted_msg = f"[{self.username}] {msg}"
        if level == "info": self.logger.info(formatted_msg)
        else: self.logger.warning(formatted_msg)
        
        if self.socketio:
            self.socketio.emit('log_update', {'msg': formatted_msg})

    async def start(self, playwright):
        self.web_log("🚀 Launching browser...")
        self.browser = await playwright.chromium.launch(
            headless=config.HEADLESS_MODE,
            args=["--disable-notifications", "--start-maximized"]
        )
        self.context = await self.browser.new_context(no_viewport=True)
        
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, 'r') as f:
                    cookies = json.load(f)
                    await self.context.add_cookies(cookies)
                self.web_log("🍪 Cookies loaded from session file.")
            except Exception as e:
                self.web_log(f"⚠️ Cookie load failed: {e}", "warn")

        self.page = await self.context.new_page()
        return True

    async def login(self):
        self.web_log("🌍 Navigating to Instagram...")
        try:
            await self.page.goto("https://www.instagram.com/", wait_until="commit")
            
            # --- 20-ATTEMPT HOME VERIFICATION LOOP ---
            for attempt in range(1, 21):
                self.web_log(f"⏳ Verifying session (Attempt {attempt}/20)...")
                await asyncio.sleep(5)
                
                if await self.page.locator('svg[aria-label="Home"]').is_visible():
                    self.web_log("✅ Home detected! Session is active.")
                    return True
            
            # Fallback to manual login
            self.web_log("🔑 Session expired. Moving to login form...")
            await self.page.goto("https://www.instagram.com/accounts/login/")
            await asyncio.sleep(3)
            
            await self.page.fill('input[name="username"]', self.username)
            await self.page.fill('input[name="password"]', self.password)
            await self.page.click('button[type="submit"]')
            
            # Wait for login to finish
            await self.page.wait_for_selector('svg[aria-label="Home"]', timeout=40000)
            
            cookies = await self.context.cookies()
            with open(self.cookie_file, 'w') as f:
                json.dump(cookies, f)
            return True
            
        except Exception as e:
            self.web_log(f"❌ Login process failed: {str(e)[:50]}", "warn")
            return False

    async def search_hashtag(self, hashtag):
        self.web_log(f"🔎 Searching: #{hashtag}")
        try:
            await self.page.goto(f"https://www.instagram.com/explore/tags/{hashtag}/", wait_until="domcontentloaded")
            await self.page.wait_for_selector('div._aagu', timeout=30000)
            
            # Scroll to load unique posts
            await self.page.mouse.wheel(0, 2000)
            await asyncio.sleep(3)
            
            links = await self.page.locator('a:has(div._aagu)').evaluate_all(
                "els => els.map(el => el.getAttribute('href'))"
            )
            unique_urls = [f"https://www.instagram.com{l}" for l in list(dict.fromkeys(links)) if "/p/" in l]
            return unique_urls[:12]
        except Exception as e:
            self.web_log(f"⚠️ Search failed for #{hashtag}: {e}", "warn")
            return []

    async def process_post(self, post_url):
        self.web_log(f"📸 Processing: {post_url.split('/')[-2]}")
        try:
            await self.page.goto(post_url, wait_until="domcontentloaded")
            await self.page.wait_for_selector('div._aagu', timeout=30000)
            await asyncio.sleep(random.uniform(2, 4))

            # --- 1. OPEN PROFILE & WAIT FOR HEADER REQUESTS ---
            username_selector = 'span._ap3a._aaco._aacw._aacx._aad7._aade'
            user_trigger = self.page.locator(username_selector).last

            if await user_trigger.is_visible():
                target_user = await user_trigger.inner_text()
                await user_trigger.click()
                


# --- 1. OPEN PROFILE & WAIT ---
            try:
                self.web_log(f"⏳ Waiting for {target_user} profile data...")
                # Wait for the URL to change to the profile
                await self.page.wait_for_url(f"**/{target_user}/", timeout=10000)
                # Quick check for the header
                await self.page.wait_for_selector('header', timeout=5000)
                self.web_log(f"👤 Profile {target_user} loaded.")
            except Exception:
                self.web_log("⚠️ Profile header slow, attempting immediate click...")


                
# --- 2. OPEN FOLLOWERS MODAL ---
            try:
                # Use a more generic selector for the followers link if the exact href fails
                followers_btn = self.page.locator(f'a[href="/{target_user}/followers/"]').first
                
                await followers_btn.click(force=True)
                
                # wait for the dialog or the specific scroll area
                self.web_log("⏳ Modal triggered, waiting for content to render...")
                await self.page.wait_for_selector('div[role="dialog"], div._aano', timeout=10000)
                
                # Extra heartbeat to let the list items load
                await asyncio.sleep(3)
                self.web_log("👥 Followers list ready.")
            except Exception as e:
                self.web_log(f"🔒 Could not open followers: {str(e)[:30]}")
                return




            # --- 3. FOLLOW LOOP ---
            self.web_log("🏃 Starting follow sequence...")

            # Give Instagram time to actually render the first followers
            await asyncio.sleep(random.uniform(5.5, 9.5))

            # More reliable selector: real <button> elements containing "Follow"
            follow_selector = 'div[role="dialog"] button >> text="Follow"'

            # Try to find the scrollable container (usually . _aano)
            scroll_container = self.page.locator('div._aano').first

            while self.followed_today_count < config.MAX_DAILY_FOLLOWS:
                follow_buttons = self.page.locator(follow_selector)

                count = await follow_buttons.count()

                if count == 0:
                    # No spam log — just scroll and wait silently
                    try:
                        await scroll_container.evaluate('el => el.scrollTop += 650')
                    except:
                        await self.page.mouse.wheel(0, 650)
                    await asyncio.sleep(random.uniform(4.0, 7.0))
                    continue

                # Process a small batch at a time (looks more natural)
                processed_this_batch = 0
                max_per_batch = 4  # adjust if you want more/less aggressive

                for i in range(count):
                    if self.followed_today_count >= config.MAX_DAILY_FOLLOWS:
                        break
                    if processed_this_batch >= max_per_batch:
                        break

                    btn = follow_buttons.nth(i)

                    try:
                        await btn.scroll_into_view_if_needed(timeout=5000)
                        await asyncio.sleep(random.uniform(0.8, 1.5))

                        await btn.click(
                            force=True,
                            delay=random.uniform(90, 190),
                            timeout=10000
                        )

                        self.followed_today_count += 1
                        self.session_batch_count += 1
                        self.web_log(f"✅ Followed ({self.followed_today_count}/{config.MAX_DAILY_FOLLOWS})")

                        await asyncio.sleep(random.uniform(
                            config.MIN_FOLLOW_DELAY,
                            config.MAX_FOLLOW_DELAY
                        ))

                        processed_this_batch += 1

                    except Exception:
                        # Silent fail on single button — continue to next
                        continue

                # Scroll for next set of followers
                try:
                    await scroll_container.evaluate('el => el.scrollTop += 850')
                except:
                    await self.page.mouse.wheel(0, 850)

                await asyncio.sleep(random.uniform(2.0, 4.5))

            # Close the followers modal
            try:
                await self.page.keyboard.press("Escape")
            except:
                # Fallback: try to click outside or close button
                try:
                    await self.page.locator('div[role="dialog"] button[aria-label="Close"]').click(timeout=4000)
                except:
                    pass








        except Exception as e:
            self.web_log(f"⚠️ Skipping post error: {str(e)[:50]}", "warn")

    async def close(self):
        if self.browser: 
            await self.browser.close()
            self.web_log("🔒 Browser closed.")

async def run_worker(user_data, socketio=None):
    async with async_playwright() as playwright:
        bot = InstagramBot(user_data, socketio)
        if await bot.start(playwright):
            try:
                if await bot.login():
                    hashtags = list(config.HASHTAGS_TO_SEARCH)
                    random.shuffle(hashtags)
                    for tag in hashtags:
                        if bot.followed_today_count >= config.MAX_DAILY_FOLLOWS: break
                        urls = await bot.search_hashtag(tag)
                        for url in urls:
                            if bot.followed_today_count >= config.MAX_DAILY_FOLLOWS: break
                            await bot.process_post(url)
            finally:
                await bot.close()

if __name__ == "__main__":
    # Standard entry point
    user_info = {'username': config.INSTAGRAM_USERNAME, 'password': config.INSTAGRAM_PASSWORD}
    asyncio.run(run_worker(user_info))