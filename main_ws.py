import asyncio
import json
import base64
import math
import os
import shutil
from urllib.parse import urljoin
import websockets
import aiohttp
import requests
from playwright.async_api import async_playwright
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, BadPassword
from filelock import FileLock, Timeout as LockTimeout

PROFILES_DIR = "/var/www/firefox_profiles"
DJANGO_SERVICE_CREATE_PROFILE_URL = "http://127.0.0.1:6688/api/social-media/profile/"
STREAM_FPS = 3
JPEG_QUALITY = 20

PROXY_HOST = "proxy.ghostvps.com"
PROXY_PASSWORD = "92964b4ea532a8e1"
PROFILE_LOCK_DIR = os.path.join(PROFILES_DIR, ".locks")
PROFILE_LOCK_TIMEOUT = 180

os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(PROFILE_LOCK_DIR, exist_ok=True)

playwright = None



def profile_lock_path(profile_path):
    name = os.path.basename(profile_path.rstrip("/").rstrip("\\"))
    return os.path.join(PROFILE_LOCK_DIR, f"ffprofile-{name}.lock")

def build_auth_headers(token):
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}

def generate_instagrapi_session(playwright_cookies, output_json_path, port, username):
    cookie_dict = {
        c["name"]: c["value"]
        for c in playwright_cookies
        if c["domain"] in [".instagram.com", "instagram.com"]
    }

    if "sessionid" not in cookie_dict:
        print("session id not found!")
        return False, "auth"

    try:
        cl = Client()
        proxy_url = f"{username}:{PROXY_PASSWORD}@{PROXY_HOST}:{port}"
        cl.set_proxy(proxy_url)
        cl.login_by_sessionid(cookie_dict["sessionid"])
        cl.dump_settings(output_json_path)
        print(f"session file created successfully: {output_json_path}")
        return True, None
    except (ChallengeRequired, LoginRequired, BadPassword) as e:
        print(f"instagram rejected the login ({type(e).__name__}): {e}")
        return False, "auth"
    except Exception as e:
        print(f"error in create session id with instagrapi: {e}")
        return False, "error"

async def patch_profile(bot_id, auth_headers, payload):
    """PATCH fields onto a profile in Django. Returns True on 2xx."""
    update_url = f"{DJANGO_SERVICE_CREATE_PROFILE_URL.rstrip('/')}/{bot_id}/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(update_url, headers=auth_headers, json=payload) as response:
                ok = 200 <= int(response.status) < 300
                try:
                    print("patch_profile:", response.status, await response.json())
                except Exception:
                    print("patch_profile:", response.status, await response.text())
                return ok
    except Exception as e:
        print("patch_profile failed:", e)
        return False


async def create_profile(headers):
    try:
        async with (aiohttp.ClientSession() as session):
            async with session.get(urljoin(DJANGO_SERVICE_CREATE_PROFILE_URL, "allocate-port/"),
                                   headers=headers) as response_port:
                if response_port.status == 401 or response_port.status == 403:
                    return False, {"msg": "authentication failed, please log in again", "type": "error"}
                data_port = await response_port.json()

                if "error" in data_port:
                    return False, {"msg": data_port["error"], "type": "error"}
                port = int(data_port["allocated_port"])

            async with session.get(
                    urljoin(DJANGO_SERVICE_CREATE_PROFILE_URL, "max_id/"), headers=headers
            ) as response:
                if response.status == 401 or response.status == 403:
                    return False, {"msg": "authentication failed, please log in again", "type": "error"}
                data = await response.json()
                print(data)
                user_id = int(data["max_id"]) + 1
    except Exception as e:
        print(e)
        return False, {"msg": "could not set a id", "type": "error"}


    profile_name = f"bot-profile-{user_id}"
    profile_path = os.path.join(PROFILES_DIR, profile_name)

    if os.path.exists(profile_path):
        shutil.rmtree(profile_path, ignore_errors=True)
    os.makedirs(profile_path, exist_ok=True)

    return True, [profile_path, profile_name, port]

async def stream(ws, page):
    delay = 1 / STREAM_FPS

    try:
        while True:
            img = await page.screenshot(
                type="jpeg",
                quality=JPEG_QUALITY,
                scale="css"
            )

            await ws.send(base64.b64encode(img).decode())

            await asyncio.sleep(delay)

    except websockets.ConnectionClosed:
        print("client disconnected")

    except asyncio.CancelledError:
        print("stream cancelled")

    except Exception as e:
        print("stream error:", e)

async def get_page(path, username, port):
    lock = FileLock(
        profile_lock_path(path),
        timeout=PROFILE_LOCK_TIMEOUT,
        thread_local=False,
    )
    await asyncio.to_thread(lock.acquire)

    try:
        browser = await playwright.firefox.launch_persistent_context(
            user_data_dir=path,
            headless=False,
            proxy={
                "server": f"http://{PROXY_HOST}:{port}",
                "username": username,
                "password": PROXY_PASSWORD
            },
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            firefox_user_prefs={
                "intl.accept_languages": "en-US,en",
                "intl.locale.requested": "en-US",
                "javascript.use_us_english_locale": True,
                "media.peerconnection.enabled": False,
                "geo.enabled": False,
                "toolkit.telemetry.enabled": False,
                "network.predictor.enabled": False,
                "browser.startup.homepage_override.mstone": "ignore",
                "startup.homepage_welcome_url": "about:blank",
                "startup.homepage_welcome_url.additional": "",
                "browser.usedOnWindows10": True,
                "toolkit.telemetry.reportingpolicy.firstRun": False,
            }
        )

        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 720})
        await page.goto("about:blank")
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)
    except Exception:
        release_lock(lock)
        raise

    return browser, page, lock


def release_lock(lock):
    if not lock:
        return
    try:
        lock.release()
    except Exception as e:
        print("lock release failed:", e)


async def clean_up(ws, stream_task, browser, page, lock=None):
    if stream_task:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Must not propagate: an exception here used to skip the lock
            # release entirely.
            print("stream task error during cleanup:", e)

    if page:
        try:
            await page.close()
        except:
            pass

    if browser:
        try:
            await browser.close()
        except:
            pass

    # Release AFTER the browser is closed — Firefox only lets go of the
    # profile directory at that point. Outside `if browser` so the lock is
    # returned even when the browser failed to start.
    release_lock(lock)

    try:
        await ws.close()
    except:
        pass

async def add_handler(ws):
    browser = None
    page = None
    profile_lock = None
    stream_task = None
    platform = None
    port = None
    profile_path = None
    country = None
    proxy_username = None
    auth_headers = {}

    try:
        async for msg in ws:
            data = json.loads(msg)

            # 🟢 init
            if data["type"] == "init":
                country = data["country"]
                proxy_username = f"b8f3fab422699782c1f0__cr.{country};sessttl.120"
                auth_headers = build_auth_headers(data.get("token"))
                if not auth_headers:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "missing auth token"
                    }))
                    continue
                created, data = await create_profile(auth_headers)
                if not created:
                    await ws.send(json.dumps(data))
                else:
                    profile_path, profile_name, port = data
                    browser, page, profile_lock = await get_page(
                        profile_path, proxy_username, port
                    )

                    await ws.send(json.dumps({
                        "type": "size",
                        "w": 1280,
                        "h": 720
                    }))

                    if stream_task is None:
                        stream_task = asyncio.create_task(stream(ws, page))

            elif data["type"] == "reset":
                if page:
                    try:
                        await page.close()
                    except:
                        pass
                    page = None

                if browser:
                    try:
                        await browser.close()
                    except:
                        pass
                    browser = None

                if stream_task:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                    stream_task = None


                await ws.send(json.dumps({
                    "type": "reset_success",
                    "msg": "لطفا دوباره کشور را انتخاب کنید."
                }))

            # 🌍 goto
            elif data["type"] == "goto":
                print('page:', page)
                url = None
                platform = data["platform"].lower()
                if platform == "instagram":
                    url = "https://www.instagram.com/"
                if platform == 'twitter':
                    url = "https://x.com/"
                if url:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                else:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "platform is not available"
                    }))

            # 🖱 click
            elif data["type"] == "click":
                await page.mouse.click(data["x"], data["y"])

            # ⌨ type
            elif data["type"] == "type":
                if data["text"] == "Backspace":
                    await page.keyboard.press("Backspace")
                elif data["text"] == "Enter":
                    await page.keyboard.press("Enter")
                else:
                    await page.keyboard.type(data["text"])

            elif data["type"] == "end":
                url = None
                username = data["username"]
                max_actions_per_hour = data["max_actions_per_hour"]
                session_data_str = None
                if platform == "instagram":
                    url = f"https://www.instagram.com/{username}/"
                if platform == 'twitter':
                    url = f"https://x.com/{username}/"
                if platform == "instagram" and page:
                    try:
                        print("Cookie extractions")
                        playwright_cookies = await page.context.cookies()

                        profile_name = os.path.basename(profile_path.strip(r"\/"))
                        session_file_name = f"session_{profile_name}.json"
                        temp_session_path = os.path.join(PROFILES_DIR, session_file_name)

                        is_session_created, failure_kind = await asyncio.to_thread(
                            generate_instagrapi_session,
                            playwright_cookies,
                            temp_session_path,
                            port,
                            proxy_username
                        )
                        if is_session_created and os.path.exists(temp_session_path):
                            with open(temp_session_path, 'r', encoding='utf-8') as f:
                                session_data_str = f.read()
                            os.remove(temp_session_path)
                        else:
                            print('Session creation failed or file does not exist.')
                            if failure_kind == "auth":
                                # New profile never actually got logged in.
                                await ws.send(json.dumps({
                                    "type": "error",
                                    "msg": "Instagram rejected this login (challenge or "
                                           "incomplete sign-in). Please log in fully and "
                                           "complete any verification before finishing."
                                }))

                    except Exception as e:
                        print(f"error in Cookie extraction: {e}")
                if profile_path and platform and url and session_data_str:
                    max_actions_count = max_actions_per_hour["follow"] + max_actions_per_hour["comment"] + max_actions_per_hour["like"] + max_actions_per_hour["direct"]
                    pause_time = math.ceil(60 / max_actions_count)
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                                DJANGO_SERVICE_CREATE_PROFILE_URL,
                                headers=auth_headers,
                                json={
                                    "firefox_profile_path": profile_path,
                                    "firefox_json_path": session_data_str,
                                    "social_media": platform,
                                    "max_actions_per_hour": max_actions_per_hour,
                                    "username": username,
                                    "pause_time": pause_time,
                                    "proxy": {
                                        "host": "proxy.ghostvps.com",
                                        "port": port,
                                        "username": f"b8f3fab422699782c1f0__cr.{country};sessttl.120",
                                        "password": "92964b4ea532a8e1",
                                    },
                                    "url": url
                                }
                        ) as response:
                            print("status:", response.status)
                            response_body = await response.text()
                            print("body:", response_body)
                            if 200 <= int(response.status) < 300:
                                await ws.send(json.dumps({"status": "profile is created"}))
                            else:
                                print(response)

                                await ws.send(json.dumps({"status": "profile is not created"}))
                                profile_state = False

                            try:
                                print(await response.json())
                            except:
                                print(await response.text())

                await clean_up(ws, stream_task, browser, page, profile_lock)
    except websockets.ConnectionClosed:
        print("client disconnected")

    finally:
        await clean_up(ws, stream_task, browser, page, profile_lock)


async def edit_handler(ws):
    browser = None
    page = None
    profile_lock = None
    stream_task = None
    profile_path = None
    platform = None
    port = None
    proxy_username = None
    bot_id = None
    auth_headers = {}
    session_data_str = None

    try:
        async for msg in ws:
            data = json.loads(msg)

            if data["type"] == "init":
                bot_id = str(data["bot_id"])
                auth_headers = build_auth_headers(data.get("token"))
                if not auth_headers:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "missing auth token"
                    }))
                    continue
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                            urljoin(DJANGO_SERVICE_CREATE_PROFILE_URL, bot_id), headers=auth_headers
                    ) as response:
                        bot_info = await response.json()
                        print('bot_info.proxy', bot_info['proxy'])
                profile_path = bot_info['firefox_profile_path']
                proxy_username = bot_info['proxy']['username']
                port = bot_info['proxy']['port']
                platform = bot_info['social_media'].lower()

                await ws.send(json.dumps({
                    "type": "size",
                    "w": 1280,
                    "h": 720
                }))

                try:
                    browser, page, profile_lock = await get_page(
                        profile_path, proxy_username, port
                    )
                except LockTimeout:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "this profile is busy (being refreshed or open elsewhere), please try again shortly"
                    }))
                    break

                if stream_task is None:
                    stream_task = asyncio.create_task(stream(ws, page))

                url = None
                if platform == "instagram":
                    url = "https://www.instagram.com/"
                if url:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                else:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "platform is not available"
                    }))

            # 🖱 click
            elif data["type"] == "click":
                await page.mouse.click(data["x"], data["y"])

            # ⌨ type
            elif data["type"] == "type":
                if data["text"] == "Backspace":
                    await page.keyboard.press("Backspace")
                elif data["text"] == "Enter":
                    await page.keyboard.press("Enter")
                else:
                    await page.keyboard.type(data["text"])

            elif data["type"] == "end":
                failure_kind = None
                if platform == "instagram" and page:
                    try:
                        print("Cookie extractions (Edit Mode)")
                        playwright_cookies = await page.context.cookies()

                        profile_name = os.path.basename(profile_path.strip(r"\/"))
                        session_file_name = f"session_{profile_name}.json"
                        temp_session_path = os.path.join(PROFILES_DIR, session_file_name)

                        is_session_created, failure_kind = await asyncio.to_thread(
                            generate_instagrapi_session,
                            playwright_cookies,
                            temp_session_path,
                            port,
                            proxy_username
                        )

                        if is_session_created and os.path.exists(temp_session_path):
                            with open(temp_session_path, 'r', encoding='utf-8') as f:
                                session_data_str = f.read()
                            os.remove(temp_session_path)
                            print('session_data_str successfully updated for edit.')
                    except Exception as e:
                        failure_kind = "error"
                        print(f"error in Cookie extraction (Edit Mode): {e}")

                # Instagram rejected the login (challenge/checkpoint, dead
                # session, bad credentials). Retrying can't fix it, so flag
                # the profile for manual attention and stop here.
                if failure_kind == "auth" and bot_id:
                    await patch_profile(bot_id, auth_headers, {
                        "status": "profile-is-not-login",
                    })
                    await ws.send(json.dumps({
                        "type": "error",
                        "status": "profile is not logged in",
                        "msg": "Instagram rejected this login (challenge or expired session). "
                               "The profile has been marked as not logged in — please log in "
                               "again and complete any verification Instagram asks for."
                    }))
                    await clean_up(ws, stream_task, browser, page, profile_lock)
                    break

                if session_data_str and bot_id:
                    ok = await patch_profile(bot_id, auth_headers, {
                        "firefox_json_path": session_data_str,
                    })
                    if ok:
                        await ws.send(json.dumps({"status": "profile session updated successfully"}))
                    else:
                        await ws.send(json.dumps({"status": "profile session update failed"}))

                await clean_up(ws, stream_task, browser, page, profile_lock)
    except websockets.ConnectionClosed:
        print("client disconnected")

    finally:
        await clean_up(ws, stream_task, browser, page, profile_lock)
            # stop_proxy(pid)


async def router(ws):
    path = ws.request.path

    if path == "/add":
        await add_handler(ws)

    elif path == "/edit":
        await edit_handler(ws)

    else:
        await ws.close()


async def main():
    global playwright
    playwright = await async_playwright().start()

    server = await websockets.serve(router, "0.0.0.0", 9000)

    print("Remote Playwright Browser running...")

    await server.wait_closed()


asyncio.run(main())