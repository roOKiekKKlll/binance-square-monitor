"""
诊断脚本 v5

修复：用 requestfinished 事件拦截（这时 post_data 齐全），
    并持续滚动页面主动触发多次请求，增加拦截成功率。
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright


SQUARE_URL = "https://www.binance.com/en/square"
FEED_KEYWORD = "/pgc/feed/feed-recommend/list"


async def main():
    out_dir = Path("debug_responses_v5")
    out_dir.mkdir(exist_ok=True)

    captured_requests = []  # [(url, method, post_data, headers)]

    print("=> 启动浏览器...")
    async with async_playwright() as p:
        user_data = Path(__file__).parent / "user_data"
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=False,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 用 requestfinished 事件：请求已完全发出，数据齐全
        async def on_req_finished(req):
            if FEED_KEYWORD in req.url and req.method == "POST":
                try:
                    body = req.post_data or ""
                    headers = await req.all_headers()
                    captured_requests.append({
                        "url": req.url,
                        "method": req.method,
                        "body": body,
                        "headers": dict(headers),
                    })
                    print(f"   ✅ 拦截到请求 #{len(captured_requests)}")
                except Exception as e:
                    print(f"   拦截时出错: {e}")

        page.on("requestfinished", lambda r: asyncio.create_task(on_req_finished(r)))

        print(f"=> 打开 {SQUARE_URL}")
        await page.goto(SQUARE_URL, wait_until="domcontentloaded", timeout=60000)
        print("   等 6 秒让前端初始化...")
        await page.wait_for_timeout(6000)

        # 如果还没拦到，滚动触发更多
        if not captured_requests:
            print("   首屏没拦到，开始滚动触发懒加载...")
            for i in range(6):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(2500)
                if captured_requests:
                    break

        if not captured_requests:
            print("\n!! 滚动后仍未拦到 feed 请求")
            print("!! 可能是网络延迟/广场页面未正确加载")
            print("!! 手动检查：浏览器里现在能看到帖子列表吗？")
            await page.wait_for_timeout(20000)  # 给 20 秒观察
            if not captured_requests:
                await context.close()
                return

        # 用第一次拦到的请求作为基准
        baseline = captured_requests[0]
        print(f"\n✅ 使用基准请求（共拦到 {len(captured_requests)} 次，取第 1 次）:")
        print(f"   URL: {baseline['url']}")
        print(f"   Body: {baseline['body'][:500]}")

        with open(out_dir / "baseline_request.json", "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)

        try:
            base_body_json = json.loads(baseline["body"]) if baseline["body"] else {}
        except Exception:
            base_body_json = {}
        print(f"   Body 字段: {list(base_body_json.keys())}")

        # 清理 headers（去掉 JS fetch 不允许的伪字段）
        bad_prefixes = (":",)
        bad_keys = {"cookie", "host", "content-length", "accept-encoding",
                    "connection", "transfer-encoding"}
        js_headers = {}
        for k, v in baseline["headers"].items():
            if k.startswith(bad_prefixes):
                continue
            if k.lower() in bad_keys:
                continue
            js_headers[k] = v

        async def try_variant(label: str, modifications: dict):
            new_body = dict(base_body_json)
            new_body.update(modifications)

            js = f"""
                async () => {{
                    try {{
                        const resp = await fetch({json.dumps(baseline['url'])}, {{
                            method: 'POST',
                            headers: {json.dumps(js_headers)},
                            body: {json.dumps(json.dumps(new_body))},
                            credentials: 'include',
                        }});
                        const text = await resp.text();
                        return {{ status: resp.status, body: text }};
                    }} catch (e) {{
                        return {{ error: e.toString() }};
                    }}
                }}
            """
            try:
                result = await page.evaluate(js)
            except Exception as e:
                print(f"\n[{label}] evaluate 失败: {e}")
                return None

            if "error" in result:
                print(f"\n[{label}] fetch 失败: {result['error']}")
                return None
            if result.get("status") != 200:
                print(f"\n[{label}] HTTP {result.get('status')}")
                return None

            try:
                data = json.loads(result["body"])
            except Exception:
                print(f"\n[{label}] 响应非 JSON")
                return None

            vos = (data.get("data") or {}).get("vos") or []
            print(f"\n[{label}]  body={json.dumps(modifications, ensure_ascii=False) if modifications else '(无修改)'}")
            print(f"    帖子数={len(vos)}, code={data.get('code')}")
            if not vos:
                return {"label": label, "min_age_minutes": None, "post_count": 0}

            now = datetime.now().timestamp()
            ages = []
            for post in vos:
                ts = post.get("date") or post.get("createTime") or 0
                if isinstance(ts, (int, float)) and ts > 0:
                    if ts > 1e11:
                        ts /= 1000
                    ages.append((now - ts) / 60)

            for j, post in enumerate(vos[:3]):
                ts = post.get("date") or post.get("createTime") or 0
                if isinstance(ts, (int, float)) and ts > 0:
                    if ts > 1e11:
                        ts /= 1000
                    dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    dt = "?"
                author = post.get("authorName") or "?"
                preview = (post.get("content") or post.get("title") or "")[:40].replace("\n", " ")
                print(f"      {j+1}. [{dt}] {author}: {preview}")

            if ages:
                ages.sort()
                print(f"    年龄(分钟): 最新={ages[0]:.0f}, 中位数={ages[len(ages)//2]:.0f}, 最老={ages[-1]:.0f}")
                return {"label": label, "min_age_minutes": ages[0], "post_count": len(vos)}
            return {"label": label, "min_age_minutes": None, "post_count": len(vos)}

        print("\n" + "="*70)
        print("第 1 步：原样重放基准请求验证")
        print("="*70)
        base_result = await try_variant("基准(原样)", {})

        if not base_result or not base_result.get("post_count"):
            print("\n!! 基准请求也拿不到帖子。打印 headers 供排查：")
            print(json.dumps(js_headers, ensure_ascii=False, indent=2)[:2000])
            await context.close()
            return

        print("\n" + "="*70)
        print("第 2 步：尝试不同参数")
        print("="*70)

        variants = [
            ("type=latest",       {"type": "latest"}),
            ("sortType=latest",   {"sortType": "latest"}),
            ("orderType=LATEST",  {"orderType": "LATEST"}),
            ("orderType=NEWEST",  {"orderType": "NEWEST"}),
            ("tab=latest",        {"tab": "latest"}),
            ("feedType=LATEST",   {"feedType": "LATEST"}),
            ("scene=latest",      {"scene": "latest"}),
            ("orderBy=time",      {"orderBy": "time"}),
            ("sortBy=time",       {"sortBy": "time"}),
            ("recommendType=1",   {"recommendType": 1}),
            ("recommendType=2",   {"recommendType": 2}),
            ("recommendType=3",   {"recommendType": 3}),
        ]

        all_results = [base_result]
        for label, mods in variants:
            r = await try_variant(label, mods)
            if r:
                all_results.append(r)
            await asyncio.sleep(0.6)

        print("\n" + "="*70)
        print("汇总（按最新帖子年龄升序，小 = 真的拉到新帖）")
        print("="*70)
        ok = [r for r in all_results if r.get("min_age_minutes") is not None]
        ok.sort(key=lambda r: r["min_age_minutes"])
        for r in ok:
            print(f"  最新年龄 {r['min_age_minutes']:>5.0f} 分钟 | 帖子 {r['post_count']:>2} 条 | {r['label']}")

        print(f"\n>>> 把以上输出完整贴给 Claude")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
