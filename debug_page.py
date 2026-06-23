"""诊断脚本: 查看课程页面结构并测试点击行为"""
import asyncio
import json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        try:
            with open("res/cookies.json") as f:
                cookies = json.load(f)
            await context.add_cookies(cookies)
            print("Cookies 已加载")
        except FileNotFoundError:
            print("未找到 cookies")

        page = await context.new_page()

        course_url = "https://wenda.zhihuishu.com/stu/courseInfo/studyResource?courseId=11508496"
        await page.goto(course_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        print(f"当前 URL: {page.url}")
        print(f"页面标题: {await page.title()}")

        # 检查 .file-item 的结构
        items = await page.locator(".file-item").all()
        print(f"\n找到 {len(items)} 个 .file-item")

        if items:
            first = items[0]
            # 获取第一个条目的HTML结构
            html = await first.inner_html()
            print(f"\n第一个条目的 HTML (前500字符):")
            print(html[:500])

            # 点击第一个条目
            print("\n===== 点击第一个 .file-item =====")
            # 监听新页面打开
            async with context.expect_page() as new_page_info:
                await first.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            await new_page.wait_for_timeout(3000)

            print(f"新页面 URL: {new_page.url}")
            print(f"新页面标题: {await new_page.title()}")

            # 检查新页面上的元素
            for sel in ["video", ".videoArea", "iframe", ".course-name", ".source-name", ".studytime-div"]:
                count = await new_page.locator(sel).count()
                print(f"  {'[OK]' if count > 0 else '[--]'} {sel}: {count}个")

            await new_page.screenshot(path="debug_click_result.png")
            print("\n截图已保存: debug_click_result.png")

            await new_page.close()

        print("\n按 Enter 关闭浏览器...")
        input()
        await browser.close()

asyncio.run(main())
