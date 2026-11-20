"""Fixture-only browser acceptance.

Requires a dedicated test broker with fake Instabox providers. Never point this
at a real deployment: this test creates a connection/grant and deletes them.
Run: CB_TEST_FIXTURE=1 CB_TEST_ADMIN_TOKEN=... .venv/bin/python tests/test_browser.py
"""
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:18765").rstrip("/")
TOKEN = os.environ.get("CB_TEST_ADMIN_TOKEN", "")


def run():
    if os.environ.get("CB_TEST_FIXTURE") != "1" or not TOKEN:
        raise SystemExit("Requires CB_TEST_FIXTURE=1 and CB_TEST_ADMIN_TOKEN for a fixture server.")
    if urlparse(BASE_URL).hostname not in {"127.0.0.1", "localhost", "::1"} or urlparse(BASE_URL).port != 18765:
        raise SystemExit("Fixture browser tests only allow a loopback BASE_URL.")
    results = Path("test-results")
    results.mkdir(exist_ok=True)
    errors = []
    connection_label = "浏览器测试连接-" + uuid.uuid4().hex[:8]
    client_label = "browser-fixture-ai-" + uuid.uuid4().hex[:8]
    malicious_label = '<img src=x onerror="window.fixtureXssExecuted=true">'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.add_init_script("window.fixtureXssExecuted = false")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        response = page.goto(BASE_URL + "/")
        assert response and response.ok
        expect(page.get_by_role("heading", name="管理员登录")).to_be_visible()
        page.evaluate("document.fonts.ready")
        assert page.evaluate("Array.from(document.fonts).some((font) => font.family === 'Manrope' && font.status === 'loaded')"), "Local Manrope font did not load"
        page.screenshot(path=str(results / "login-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 375, "height": 812})
        page.screenshot(path=str(results / "login-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1280, "height": 720})
        page.get_by_label("管理令牌").fill(TOKEN)
        page.get_by_role("button", name="登录", exact=True).click()
        expect(page.get_by_role("heading", name="连接目录", exact=True)).to_be_visible()
        expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
        admin_response = context.request.get(BASE_URL + "/admin")
        assert "default-src" in admin_response.headers.get("content-security-policy", ""), "CSP missing"
        assert admin_response.headers.get("x-content-type-options") == "nosniff"
        expect(page.locator(".provider-icon")).to_have_count(6)
        assert page.locator(".provider-icon").evaluate_all("(images) => images.every(i => i.complete && i.naturalWidth > 0)")
        for name in ("飞书", "Instagram"):
            expect(page.get_by_role("article").filter(has=page.get_by_role("heading", name=name, exact=True))).to_contain_text("暂不支持")
        search = page.get_by_role("searchbox", name="搜索服务")
        search.fill("gmail")
        expect(page.get_by_role("article").filter(has=page.get_by_role("heading", name="Gmail", exact=True))).to_be_visible()
        expect(page.get_by_role("article").filter(has=page.get_by_role("heading", name="SSH", exact=True))).to_have_count(0)
        search.fill("no-such-provider-fixture")
        expect(page.locator("article.provider-card")).to_have_count(0)
        search.fill("")
        for category, visible, absent in (("邮箱", "Gmail", "SSH"), ("代码托管", "GitHub", "Gmail"), ("远程主机", "SSH", "Gmail")):
            page.get_by_role("button", name=category, exact=True).click()
            expect(page.get_by_role("article").filter(has=page.get_by_role("heading", name=visible, exact=True))).to_be_visible()
            expect(page.get_by_role("article").filter(has=page.get_by_role("heading", name=absent, exact=True))).to_have_count(0)
        page.get_by_role("button", name="全部", exact=True).click()
        for name in ("飞书", "Instagram"):
            unavailable = page.get_by_role("article").filter(has=page.get_by_role("heading", name=name, exact=True))
            expect(unavailable.get_by_role("button", name="连接", exact=True)).to_have_count(0)
        page.screenshot(path=str(results / "catalog-desktop.png"), full_page=True)

        for anchor, title in (("connections", "我的连接"), ("grants", "AI 授权"), ("audit", "操作记录"), ("catalog", "连接目录")):
            page.locator(f"nav a[href='#{anchor}']").click()
            expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
            expect(page.locator("#content")).to_have_attribute("aria-busy", "false")

        card = page.get_by_role("article").filter(has=page.get_by_role("heading", name="Instabox", exact=True))
        card.get_by_role("button", name="连接", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        assert dialog.evaluate("(d) => d.contains(document.activeElement)"), "Dialog must receive focus"
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(card.get_by_role("button", name="连接", exact=True)).to_be_focused()
        card.get_by_role("button", name="连接", exact=True).click()
        popup = page.get_by_role("dialog")
        popup.get_by_label("连接名称").fill(connection_label)
        popup.get_by_label("允许访问的机器 ID（每行一个）").fill("fixture-machine")
        popup.get_by_label("读取密钥", exact=True).fill("ibk_fixture_read_not_a_real_key")
        popup.get_by_role("button", name="创建连接").click()
        expect(popup).not_to_be_visible()
        expect(page.get_by_role("heading", name="我的连接", exact=True)).to_be_visible()
        connected = page.get_by_role("article").filter(has=page.get_by_role("heading", name=connection_label, exact=True))
        connected.get_by_role("button", name="测试连接").click()
        expect(popup).to_contain_text("连接测试通过。")
        popup.get_by_role("button", name="关闭窗口").click()

        # A second connection to the same provider must update the catalog without reload.
        page.locator("nav a[href='#catalog']").click()
        card.get_by_role("button", name="连接", exact=True).click()
        popup.get_by_label("连接名称").fill(malicious_label)
        popup.get_by_label("允许访问的机器 ID（每行一个）").fill("fixture-machine")
        popup.get_by_label("读取密钥", exact=True).fill("ibk_fixture_read_not_a_real_key")
        popup.get_by_role("button", name="创建连接").click()
        expect(popup).not_to_be_visible()
        malicious = page.get_by_role("article").filter(has=page.get_by_role("heading", name=malicious_label, exact=True))
        expect(malicious).to_be_visible()
        expect(malicious.locator("img[src='x']")).to_have_count(0)
        assert not page.evaluate("window.fixtureXssExecuted"), "Connection label executed HTML"
        connection_search = page.get_by_role("searchbox", name="搜索连接")
        connection_search.fill(connection_label)
        expect(connected).to_be_visible()
        expect(malicious).to_have_count(0)
        connection_search.fill("")
        page.locator("nav a[href='#catalog']").click()
        card.get_by_role("button", name="选择连接并测试", exact=True).click()
        expect(popup.get_by_role("button", name=connection_label, exact=True)).to_be_visible()
        expect(popup.get_by_role("button", name=malicious_label, exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        page.locator("nav a[href='#grants']").click()
        page.get_by_role("button", name="新建授权").click()
        popup.get_by_label("AI 客户端名称").fill(client_label)
        popup.get_by_label("连接", exact=True).select_option(label=connection_label)
        before_grants = context.request.get(BASE_URL + "/admin/api/v1/grants").json()
        popup.get_by_role("button", name="签发令牌").click()
        expect(popup).to_contain_text("至少选择")
        assert context.request.get(BASE_URL + "/admin/api/v1/grants").json() == before_grants, "Empty scope created a grant"
        popup.locator("input[name='resource']").first.check()
        popup.locator("input[name='tool']").first.check()
        popup.get_by_label("有效期").select_option("900")
        popup.get_by_role("button", name="签发令牌").click()
        expect(popup.get_by_role("heading", name="网关令牌已签发")).to_be_visible()
        gateway_token = popup.get_by_label("网关令牌", exact=True).input_value()
        assert gateway_token and gateway_token != TOKEN
        popup.get_by_role("button", name="复制令牌").click()
        expect(popup.get_by_role("status")).to_have_text("已复制。")
        assert page.evaluate("navigator.clipboard.readText()") == gateway_token
        assert not page.evaluate("(token) => JSON.stringify({...localStorage,...sessionStorage}).includes(token)", gateway_token)
        popup.get_by_role("button", name="关闭窗口").click()
        assert gateway_token not in page.content()
        grants = context.request.get(BASE_URL + "/admin/api/v1/grants").json()
        assert gateway_token not in str(grants), "Grant GET must not return token"
        grant = page.get_by_role("article").filter(has=page.get_by_role("heading", name=client_label, exact=True))
        grant_search = page.get_by_role("searchbox", name="搜索授权")
        grant_search.fill(client_label)
        expect(grant).to_be_visible()
        grant_search.fill("missing-grant-fixture")
        expect(grant).to_have_count(0)
        grant_search.fill("")
        grant.get_by_role("button", name="撤销授权").click()
        popup.get_by_role("button", name="确认", exact=True).click()
        expect(popup).not_to_be_visible()
        expect(grant).to_contain_text("已撤销")
        page.locator("nav a[href='#connections']").click()
        connected.get_by_role("button", name="删除连接").click()
        popup.get_by_role("button", name="确认", exact=True).click()
        expect(popup).not_to_be_visible()
        expect(connected).to_have_count(0)

        malicious.get_by_role("button", name="删除连接").click()
        popup.get_by_role("button", name="确认", exact=True).click()
        expect(popup).not_to_be_visible()
        expect(malicious).to_have_count(0)

        expect(page.locator("#notice")).to_be_hidden(timeout=7000)
        page.emulate_media(reduced_motion="reduce")
        for width in (375, 768, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            for anchor in ("catalog", "connections", "grants", "audit"):
                page.locator(f"nav a[href='#{anchor}']").click()
                expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{anchor} overflows at {width}"
            page.screenshot(path=str(results / f"audit-{width}.png"), full_page=True)
        page.set_viewport_size({"width": 375, "height": 812})
        for anchor in ("catalog", "connections", "grants", "audit"):
            page.locator(f"nav a[href='#{anchor}']").click()
            expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), anchor + " overflows"
            page.screenshot(path=str(results / f"{anchor}-mobile.png"), full_page=True)
        assert not errors, "Browser errors: " + "; ".join(errors)
        context.close()
        browser.close()
    print("PASS fixture browser flows, token lifecycle, responsive layout and console")


if __name__ == "__main__":
    run()
