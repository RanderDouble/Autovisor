"""诊断: 检查伦巴舞模块下的侧边栏项目"""
import asyncio, json
from playwright.async_api import async_playwright, TimeoutError

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            with open('res/cookies.json') as f:
                await context.add_cookies(json.load(f))
        except: pass
        page = await context.new_page()
        page.set_default_timeout(10000)

        url = 'https://ai-smart-course-student-pro.zhihuishu.com/learnPage/2031308668068024320/1889941814669086720/189466?catalogActiveTab=personal'
        await page.goto(url, wait_until='domcontentloaded')
        await page.wait_for_timeout(3000)

        # Expand all collapses (same as working_loop does)
        for loop in range(5):
            count = await page.evaluate("""() => {
                const items = document.querySelectorAll('.el-collapse-item:not(.is-active) .el-collapse-item__header');
                items.forEach(h => h.click());
                return items.length;
            }""")
            print(f'Expand round {loop}: {count} collapsed items')
            await page.wait_for_timeout(300)
            if count == 0:
                break

        # Get ALL sidebar items with depth info
        data = await page.evaluate("""() => {
            const items = document.querySelectorAll('.section-item-collapse-info');
            return [...items].map((el, idx) => {
                // Find nesting depth by counting ancestor collapse items
                let depth = 0;
                let parent = el.parentElement;
                while (parent) {
                    if (parent.classList.contains('el-collapse-item')) depth++;
                    parent = parent.parentElement;
                }
                return {
                    idx,
                    title: el.querySelector('.section-item-collapse-title')?.textContent?.trim(),
                    progress: el.querySelector('.collapse-info-progress')?.textContent?.trim(),
                    depth,
                    isActive: el.classList.contains('active')
                };
            });
        }""")

        print(f'\nTotal items: {len(data)}')

        # Show items in 伦巴 module area
        in_rhumba = False
        for item in data:
            title = item['title'] or ''
            if '伦巴' in title or in_rhumba:
                in_rhumba = True
                print(f'  [{item["idx"]}] depth={item["depth"]} active={item["isActive"]} | {title} - {item["progress"]}')
            if '双人套路' in title:
                break  # stop after finding the target

        await browser.close()

asyncio.run(main())
