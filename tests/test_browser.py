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
    if urlparse(BASE_URL).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Fixture browser tests only allow a loopback BASE_URL.")
    results = Path("test-results")
    results.mkdir(exist_ok=True)
    errors = []
    connection_label = "浏览器测试连接-" + uuid.uuid4().hex[:8]
    client_label = "browser-fixture-ai-" + uuid.uuid4().hex[:8]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        response = page.goto(BASE_URL + "/")
        assert response and response.ok
        expect(page.get_by_role("heading", name="管理员登录")).to_be_visible()
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
        page.screenshot(path=str(results / "catalog-desktop.png"), full_page=True)

        for anchor, title in (("connections", "我的连接"), ("grants", "AI 授权"), ("audit", "操作记录"), ("catalog", "连接目录")):
            page.locator(f"nav a[href='#{anchor}']").click()
            expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
            expect(page.locator("#content")).to_have_attribute("aria-busy", "false")

        card = page.get_by_role("article").filter(has=page.get_by_role("heading", name="Instabox", exact=True))
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

        page.locator("nav a[href='#grants']").click()
        page.get_by_role("button", name="新建授权").click()
        popup.get_by_label("AI 客户端名称").fill(client_label)
        popup.get_by_label("连接", exact=True).select_option(label=connection_label)
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
        grant.get_by_role("button", name="撤销授权").click()
        popup.get_by_role("button", name="确认", exact=True).click()
        expect(popup).not_to_be_visible()
        expect(grant).to_contain_text("已撤销")
        page.locator("nav a[href='#connections']").click()
        connected.get_by_role("button", name="删除连接").click()
        popup.get_by_role("button", name="确认", exact=True).click()
        expect(popup).not_to_be_visible()
        expect(connected).to_have_count(0)

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
