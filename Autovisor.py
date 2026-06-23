# encoding=utf-8
import asyncio
import os
import time
import traceback
import sys
from playwright.async_api import async_playwright, Playwright, Page, BrowserContext
from playwright.async_api import TimeoutError
from playwright._impl._errors import TargetClosedError
from modules.logger import Logger
from modules.configs import Config
from modules.progress import get_course_progress, show_course_progress
from modules.support import show_donate
from modules.utils import optimize_page, get_lesson_name, get_filtered_class, get_video_attr, hide_window, \
     save_cookies, load_cookies, clear_cookies, get_runtime_path, IS_WINDOWS
from modules.slider import slider_verify
from modules.tasks import video_optimize, play_video, skip_questions, wait_for_verify, task_monitor
from modules import installer
from modules.banner import print_banner

# 获取全局事件循环
event_loop_verify = asyncio.Event()
event_loop_answer = asyncio.Event()
COOKIE_PATH = get_runtime_path("res", "cookies.json")


async def wait_for_interruption(event_loop: asyncio.Event) -> float:
    event_loop.clear()
    wait_start = time.time()
    await event_loop.wait()
    return time.time() - wait_start


def cal_time_period(start_time: float, paused_time: float) -> float:
    return max(0.0, time.time() - start_time - paused_time)

async def init_page(p: Playwright, cookies) -> tuple[Page, BrowserContext]:
    driver = config.driver
    if driver == "edge":
        driver = "msedge"
    logger.info(f"正在启动{config.driver}浏览器...")
    launch_args = {
        "headless": False,
        "executable_path": config.exe_path if config.exe_path else None,
        "args": [
            f'--window-size={1600},{900}',
            '--window-position=100,100',
        ],
    }
    if IS_WINDOWS or driver != "msedge":
        launch_args["channel"] = driver
    else:
        logger.info("非Windows系统, 使用内置Chromium浏览器.")
    try:
        browser = await p.chromium.launch(**launch_args)
    except TargetClosedError as e:
        logger.log_exception("首次启动浏览器失败,准备重试.", e)
        logger.info("检测到浏览器首次启动失败,正在重试...")
        await asyncio.sleep(1)
        browser = await p.chromium.launch(**launch_args)
    context = await browser.new_context()
    # 加载 Cookies
    if cookies:
        await context.add_cookies(cookies)
        logger.info("已加载 Cookies!")
    else:
        logger.info("未找到 Cookies,将跳转至登录页.")
    page = await context.new_page()
    logger.debug(f"{config.driver}浏览器启动完成.")
    #抹去特征
    with open('res/stealth.min.js', 'r') as f:
        js = f.read()
    await page.add_init_script(js)
    logger.debug("stealth.js执行完成.")
    page.set_default_timeout(24 * 3600 * 1000)

    return page, context

async def auto_login(context: BrowserContext, page: Page, modules=None):
    cookie_saved = False

    async def request_handler(request):
        nonlocal cookie_saved
        if cookie_saved:
            return
        if "https://www.zhihuishu.com" in request.url:
            cookies = await context.cookies()
            save_cookies(cookies, COOKIE_PATH)
            logger.info(f"已保存登录凭证到: {COOKIE_PATH},下次可免密登录.")
            cookie_saved = True

    await page.goto(config.login_url, wait_until="commit")
    if "login" not in page.url:
        logger.info("检测到已登录,跳过登录步骤.")
        return
    await page.wait_for_selector(".wall-main", state='attached')  # 等待登陆界面加载
    page.on('request', request_handler)
    if config.username and config.password:
        await page.wait_for_selector("#lUsername", state="attached")
        await page.wait_for_selector("#lPassword", state="attached")
        await page.locator('#lUsername').fill(config.username)
        await page.locator('#lPassword').fill(config.password)
        await page.wait_for_selector(".wall-sub-btn", state="attached")
        await page.wait_for_timeout(500)
        await page.locator(".wall-sub-btn").first.click()
    if config.enableAutoCaptcha and modules:
        await slider_verify(page, modules)
    # 等待登录完成: 弹窗消失 或 页面已跳转(扫码登录)
    try:
        await page.wait_for_selector(".wall-main", state='hidden', timeout=120000)
    except TimeoutError:
        if "login" not in page.url:
            logger.info("检测到页面已跳转,登录成功.")
            return
        raise


async def ensure_login(context: BrowserContext, page: Page, cookies, modules=None):
    if cookies:
        logger.info("正在校验 Cookies 登录状态...")
        await page.goto(config.login_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        if "login" not in page.url:
            logger.info("使用Cookies登录成功!")
            return True
        logger.warn("检测到 Cookies 已失效, 将重新登录.", shift=True)
        clear_cookies(COOKIE_PATH)
        cookies = None

    if not config.username or not config.password:
        logger.info("请手动填写账号密码...")
    logger.info("正在等待登录完成...")
    await auto_login(context, page, modules)
    logger.info("登录成功!")
    return False


async def learning_loop(page: Page, start_time, is_new_version=False, is_hike_class=False):
    paused_time = 0.0
    try:
        cur_time = await get_course_progress(page, is_new_version, is_hike_class)
    except TargetClosedError:
        return paused_time
    while cur_time != "100%":
        try:
            limit_time = config.limitMaxTime
            time_period = cal_time_period(start_time, paused_time) / 60
            if 0 < limit_time <= time_period:
                break
            cur_time = await get_course_progress(page, is_new_version, is_hike_class)
            show_course_progress(desc="完成进度:", cur_time=cur_time)
            await asyncio.sleep(0.5)
        except TargetClosedError:
            return paused_time
        except TimeoutError as e:
            if await page.query_selector(".yidun_modal__title"):
                paused_time += await wait_for_interruption(event_loop_verify)
            elif await page.query_selector(".topic-title"):
                paused_time += await wait_for_interruption(event_loop_answer)
            else:
                logger.debug(f"学习进度轮询未命中: {logger.summarize_exception(e)}")
    return paused_time


async def review_loop(page: Page, start_time, is_hike_class=False, video_progress=False):
    paused_time = 0.0
    total_time = await get_video_attr(page, "duration")
    if total_time is None:
        return paused_time
    try:
        await page.evaluate(config.reset_curtime)  # 重置视频播放时间
    except TargetClosedError:
        return paused_time
    while True:
        try:
            limit_time = config.limitMaxTime
            cur_time = await get_video_attr(page, "currentTime")
            if cur_time is None or cur_time >= total_time:
                break
            time_period = cal_time_period(start_time, paused_time) / 60
            if 0 < limit_time <= time_period:
                break
            if video_progress:
                pct = int(cur_time / total_time * 100) if total_time > 0 else 0
                show_course_progress(desc="完成进度:", cur_time=pct)
            else:
                show_course_progress(desc="完成进度:", cur_time=time_period, limit_time=limit_time)
            await asyncio.sleep(0.5)
        except TargetClosedError:
            return paused_time
        except TimeoutError as e:
            if await page.query_selector(".yidun_modal__title"):
                paused_time += await wait_for_interruption(event_loop_verify)
            elif await page.query_selector(".topic-title"):
                paused_time += await wait_for_interruption(event_loop_answer)
            else:
                logger.debug(f"复习进度轮询未命中: {logger.summarize_exception(e)}")
    return paused_time


async def process_aismart_resources(page: Page, start_time) -> float:
    """处理AI智慧课的必学资源: 逐个点击视频播放完成或打开PPT后关闭"""
    paused_time = 0.0
    processed = set()

    try:
        while True:
            # 每次重新获取资源列表(防止DOM刷新后元素过期)
            sections = await page.locator('.resources-section').all()
            required_section = None
            for s in sections:
                title_el = s.locator('.resources-detail-title')
                if await title_el.count() > 0:
                    title = (await title_el.text_content()).strip()
                    if '必学' in title:
                        required_section = s
                        break
            if not required_section:
                break

            # 找第一个未完成的资源
            resources = await required_section.locator('.basic-info-video-card-container').all()
            target = None
            target_idx = -1
            for i, r in enumerate(resources):
                text = (await r.text_content()).strip()
                if '已完成' not in text and i not in processed:
                    target = r
                    target_idx = i
                    break

            if not target:
                break  # 所有必学资源已完成

            # 点击该资源
            text = (await target.text_content()).strip()
            logger.info(f"处理必学资源 [{target_idx}]: {text[:50]}")
            await target.evaluate("el => el.click()")

            if '.ppt' in text.lower() or '.pptx' in text.lower():
                # PPT资源: 等待抽屉打开后关闭
                await page.wait_for_timeout(2000)
                if await page.locator('.el-drawer').count() > 0:
                    try:
                        await page.locator('.el-drawer__close-btn').click(timeout=2000)
                    except Exception:
                        await page.keyboard.press('Escape')
                    await page.wait_for_timeout(500)
                logger.info("PPT资源已完成.")
                processed.add(target_idx)
            else:
                # 视频资源: 等待加载后播放到结束
                try:
                    await page.wait_for_selector("video", state="attached", timeout=10000)
                    await page.wait_for_timeout(1000)
                except TimeoutError:
                    processed.add(target_idx)
                    continue

                total_time = await get_video_attr(page, "duration")
                if total_time is None or total_time == 0:
                    await page.wait_for_timeout(2000)
                    total_time = await get_video_attr(page, "duration")
                if total_time is None or total_time == 0:
                    processed.add(target_idx)
                    continue

                try:
                    await page.evaluate(config.reset_curtime)
                except Exception:
                    pass

                while True:
                    try:
                        limit_time = config.limitMaxTime
                        cur_time = await get_video_attr(page, "currentTime")
                        if cur_time is None or cur_time >= total_time:
                            break
                        time_period = cal_time_period(start_time, paused_time) / 60
                        if 0 < limit_time <= time_period:
                            break
                        pct = int(cur_time / total_time * 100) if total_time > 0 else 0
                        show_course_progress(desc="完成进度:", cur_time=pct)
                        await asyncio.sleep(0.5)
                    except TargetClosedError:
                        return paused_time
                    except TimeoutError:
                        break
                logger.info("视频资源已完成.")
                processed.add(target_idx)

    except TargetClosedError:
        pass
    except Exception as e:
        logger.debug(f"资源处理异常: {logger.summarize_exception(e)}")
    return paused_time


async def working_loop(page: Page, is_new_version=False, is_hike_class=False, is_aided=False, is_aismart=False):
    # 获取所有课程元素
    if is_aismart:
        await page.wait_for_selector(".section-item-collapse-info", state="attached")
        all_class = await page.locator(".section-item-collapse-info").all()
        learning = True
    elif is_aided or is_hike_class:
        await page.wait_for_selector(".file-item", state="attached")
        to_learn_class = await get_filtered_class(page, is_new_version, is_hike_class, is_aided=is_aided)
        learning = True if len(to_learn_class) > 0 else False
        if learning:
            all_class = to_learn_class
        else:
            all_class = await get_filtered_class(page, is_new_version, is_hike_class, include_all=True, is_aided=is_aided)
    else:
        await page.wait_for_selector(".clearfix.video", state="attached")
        to_learn_class = await get_filtered_class(page, is_new_version, is_hike_class)
        learning = True if len(to_learn_class) > 0 else False
        if learning:
            all_class = to_learn_class
        else:
            all_class = await get_filtered_class(page, is_new_version, is_hike_class, include_all=True)
    start_time = time.time()
    paused_time = 0.0
    cur_index = 0

    while cur_index < len(all_class):
        if is_aismart:
            prog_el = all_class[cur_index].locator('.collapse-info-progress')
            if await prog_el.count() > 0:
                progress = await prog_el.text_content()
                if '/' in (progress or ''):
                    parts = progress.strip().replace('必学 ', '').split('/')
                    if len(parts) == 2 and parts[0] == parts[1]:
                        title_el = all_class[cur_index].locator('.section-item-collapse-title')
                        skip_name = (await title_el.text_content()).strip()
                        logger.info(f"已完成，跳过: {skip_name}")
                        cur_index += 1
                        continue
            else:
                # 没有进度元素的是章节标题(如"伦巴舞手臂动作"), 跳过
                cur_index += 1
                continue
        if is_aismart:
            # 每次点击前循环展开所有嵌套折叠(模块→单元→知识点)
            for _ in range(5):
                count = await page.evaluate("""() => {
                    const items = document.querySelectorAll('.el-collapse-item:not(.is-active) .el-collapse-item__header');
                    items.forEach(h => h.click());
                    return items.length;
                }""")
                if count == 0:
                    break
                await page.wait_for_timeout(300)
            # 用JS点击避免折叠区域不可见的问题
            await all_class[cur_index].evaluate("el => el.click()")
        else:
            await all_class[cur_index].click()
        if is_aismart:
            await page.wait_for_selector("video", state="attached")
        elif is_aided:
            await page.wait_for_selector("video", state="attached")
        elif is_hike_class:
            await page.wait_for_selector(".file-item.active", state="attached")
        else:
            await page.wait_for_selector(".current_play", state="attached")
        await page.wait_for_timeout(1000)
        title = await get_lesson_name(page, is_hike_class, is_aided, is_aismart)
        logger.info(f"正在学习:{title}")
        page.set_default_timeout(10000)
        # 移除视频暂停功能
        await page.wait_for_selector("video", state="attached")
        await page.evaluate(config.remove_pause)
        if is_aismart:
            # 统一处理所有必学资源(视频+PPT)
            paused_time += await process_aismart_resources(page, start_time)
        elif is_aided:
            paused_time += await review_loop(page, start_time, is_hike_class, video_progress=True)
        elif learning:
            paused_time += await learning_loop(page, start_time, is_new_version, is_hike_class)
        else:
            paused_time += await review_loop(page, start_time, is_hike_class)
        cur_index += 1
        reachTimeLimit = await check_time_limit(page, start_time, paused_time, all_class, title, is_hike_class, is_aided)
        if reachTimeLimit:
            return


async def check_time_limit(page: Page, start_time, paused_time, all_class, title, is_hike_class, is_aided=False) -> bool:
    reachTimeLimit = False
    page.set_default_timeout(24 * 3600 * 1000)
    time_period = cal_time_period(start_time, paused_time) / 60
    if 0 < config.limitMaxTime <= time_period:
        logger.info(f"当前课程已达时限:{config.limitMaxTime}min", shift=True)
        logger.info("即将进入下门课程!")
        reachTimeLimit = True
    else:
        class_name = await all_class[-1].get_attribute('class')
        if is_aided:
            logger.info(f"\"{title}\" 已完成!", shift=True)
            logger.info(f"本次课程已学习:{time_period:.1f} min")
        elif is_hike_class:
            if "active" in class_name:
                logger.info("已学完本课程全部内容!", shift=True)
                print("==" * 10)
            else:
                logger.info(f"\"{title}\" 已完成!", shift=True)
                logger.info(f"本次课程已学习:{time_period:.1f} min")
        else:
            if "current_play" in class_name:
                logger.info("已学完本课程全部内容!", shift=True)
                print("==" * 10)
            else:
                logger.info(f"\"{title}\" 已完成!", shift=True)
                logger.info(f"本次课程已学习:{time_period:.1f} min")
    return reachTimeLimit


async def main():
    modules, tasks = [], []
    if config.enableAutoCaptcha:
        print("===== Install Log =====")
        logger.info("正在检查依赖库...")
        modules = installer.start()
        logger.info("所有依赖库安装完成!")
    print("====== Login Log ======")
    async with async_playwright() as p:
        cookies = load_cookies(COOKIE_PATH)
        page, context = await init_page(p, cookies)

        await ensure_login(context, page, cookies, modules)

        # 先启动人机验证协程
        verify_task = asyncio.create_task(wait_for_verify(page, config, event_loop_verify))

        # 启动协程任务
        video_optimize_task = asyncio.create_task(video_optimize(page, config))
        skip_ques_task = asyncio.create_task(skip_questions(page, event_loop_answer))
        play_video_task = asyncio.create_task(play_video(page))
        tasks.extend([verify_task, video_optimize_task, skip_ques_task, play_video_task])

        # 隐藏窗口
        if config.enableHideWindow:
            await hide_window(page)

        # 任务监视器
        monitor_task = asyncio.create_task(task_monitor(tasks))

        # 遍历所有课程,加载网页
        for course_url in config.course_urls:
            print("===== Runtime Log =====")
            is_new_version = "fusioncourseh5" in course_url
            is_hike_class = "hike.zhihuishu.com" in course_url
            is_aided = "wenda.zhihuishu.com" in course_url
            is_aismart = "ai-smart-course-student-pro.zhihuishu.com" in course_url
            logger.info("正在加载播放页...")
            await page.goto(course_url, wait_until="commit")
            await page.wait_for_timeout(1500)
            if is_aided:
                await page.wait_for_selector(".file-item", state="attached")
                await page.evaluate("window.open = url => { window.__hikeUrl = url; return null; }")
                await page.locator(".file-item").first.click()
                await page.wait_for_timeout(1000)
                hike_url = await page.evaluate("window.__hikeUrl")
                if hike_url:
                    await page.goto(hike_url, wait_until="commit")
                    await page.wait_for_timeout(2000)
                    is_hike_class = True
                    logger.info("已进入辅助教学播放页.")
            if is_aismart:
                await page.wait_for_selector(".tab-item", state="attached")
                tabs = await page.locator(".tab-item").all()
                for tab in tabs:
                    text = await tab.text_content()
                    if "个性化学习路径" in text or "学习路径" in text:
                        await tab.click()
                        break
                await page.wait_for_timeout(2000)
                await page.wait_for_selector(".item-content", state="attached")
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                    await page.locator(".item-content").first.click()
                logger.info("已进入AI智慧课播放页.")
            if "login" in page.url:
                logger.warn("播放页跳转到登录页, 当前登录状态已失效, 正在重新登录.", shift=True)
                clear_cookies(COOKIE_PATH)
                await ensure_login(context, page, None, modules)
                logger.info("重新进入播放页...")
                await page.goto(course_url, wait_until="commit")
                await page.wait_for_timeout(1500)
            # 关闭弹窗,优化页面结构
            await optimize_page(page, config, is_new_version, is_hike_class, is_aided, is_aismart)
            logger.info("页面优化完成!")
            # 获取课程标题
            if is_aismart:
                title_el = page.locator(".section-item-header").first
                course_title = await title_el.text_content()
                logger.info(f"当前课程:<<{course_title.strip()}>>，AI智慧课")
            elif is_aided or is_hike_class:
                title_selector = await page.wait_for_selector(".course-name")
                course_title = await title_selector.text_content()
                logger.info(f"当前课程:<<{course_title}>>，{'辅助教学课' if is_aided else '是翻转课哎'}")
            elif not is_new_version:
                title_selector = await page.wait_for_selector(".source-name")
                course_title = await title_selector.text_content()
                logger.info(f"当前课程:<<{course_title}>>")
            # 启动课程主循环
            await working_loop(page, is_new_version=is_new_version, is_hike_class=is_hike_class, is_aided=is_aided, is_aismart=is_aismart)
    print("===== Task Finished =====")
    logger.info("所有课程已学习完毕!")
    show_donate("res/QRcode.jpg", show=config.showDonateCode)
    # 结束所有协程任务
    await asyncio.gather(*tasks, return_exceptions=True) if tasks else None
    await monitor_task


if __name__ == "__main__":
    print_banner()
    logger = Logger()
    try:
        print("====== Init Log ======")
        logger.info("程序启动中...")
        config = Config("configs.ini")
        if not config.course_urls:
            logger.error("未检测到有效网址或不支持此类网页,请检查配置文件!")
            time.sleep(2)
            sys.exit(-1)
        asyncio.run(main())
    except TargetClosedError as e:
        if "BrowserType.launch" in repr(e):
            logger.log_exception("浏览器相关流程异常结束.", e)
            logger.error("浏览器启动失败,请尝试重新启动!")
            logger.info("如果仍然无法启动,请修改配置文件并使用Chrome浏览器")
        else:
            logger.debug(f"浏览器关闭结束运行: {logger.summarize_exception(e)}")
    except Exception as e:
        logger.log_exception("程序运行时出现未处理异常.", e, shift=True)
        if isinstance(e, KeyError):
            logger.error(f"配置文件错误!")
        elif isinstance(e, FileNotFoundError):
            logger.error(f"依赖文件缺失: {e.filename},请重新安装程序!")
        elif isinstance(e, UnicodeDecodeError):
            logger.error("配置文件编码错误,保存时请选择UTF-8或GBK编码!")
        else:
            logger.error("系统出错,请检查后重新启动!")
    finally:
        logger.save()
        input("程序已结束,按Enter退出...")
