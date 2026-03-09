import re
import time
from typing import Optional

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage._elements.chromium_element import ChromiumElement

TARGET_URL = (
    "https://onlinelibrary.wiley.com/action/oidcCallback"
    "?idpCode=connect"
    "&error=login_required"
    "&error_description=Login+required"
    "&state=Dps2IO0LOrpSUAYYguc7KjWtugvQmVzeIKLq%2B62WBXzFLZcVur3g726JZ4r%2F02kSoR4HnQzH5UKrKme58LUWHhW5DtJ2%2Bm4kYa%2Bkum0p5ip%2F1bsZ2%2FjOU%2FJeE5O8GuzqLb5KKLvAJM%2FyNdHDL3tGjC7kvFRRIbPq6J6y69KPfA193T9h5JyFwxQz%2BlDL5GKjxG1ETW7e3e8XOLsO65xNT%2FqeYPtDiBOF"
)
MAX_RETRIES = 5
HEADLESS = False


class CloudflareByPasser:
    """自动处理常见 Cloudflare 验证页。"""

    EMPTY_PAGE_REGEX = re.compile(
        r"<html><head></head><body></body></html>|ERR_CONNECTION_RESET|ERR_PROXY_CONNECTION_FAILED"
    )

    def __init__(self, driver: ChromiumPage, max_retries: int = 5, log: bool = True):
        self.driver = driver
        self.max_retries = max_retries
        self.log = log

    def _log(self, message: str) -> None:
        if self.log:
            print(f"[CloudflareByPasser] {message}")

    def _locate_verification_button(self) -> Optional[ChromiumElement]:
        try:
            for input_element in self.driver.eles("tag:input"):
                attrs = input_element.attrs
                if "turnstile" not in attrs.get("name", "").lower():
                    continue
                if attrs.get("type") != "hidden":
                    continue

                parent = input_element.parent()
                if not parent or not parent.shadow_root:
                    continue

                shadow_child = parent.shadow_root.child()
                if not shadow_child:
                    continue

                body = shadow_child("tag:body")
                if not body or not body.shadow_root:
                    continue

                button = body.shadow_root("tag:input")
                if button:
                    return button
        except Exception as exc:
            self._log(f"定位验证按钮失败: {exc}")
        return None

    def _click_verification_button(self) -> bool:
        button = self._locate_verification_button()
        if not button:
            self._log("未找到验证按钮。")
            return False

        try:
            self._log("找到验证按钮，开始点击。")
            button.click()
            return True
        except Exception as exc:
            self._log(f"点击验证按钮失败: {exc}")
            return False

    def _is_connection_reset(self) -> bool:
        return bool(self.EMPTY_PAGE_REGEX.search(self.driver.html or ""))

    def is_bypassed(self) -> bool:
        try:
            return "just a moment" not in self.driver.title.lower()
        except Exception as exc:
            self._log(f"读取页面标题失败: {exc}")
            return False

    def bypass(self) -> bool:
        time.sleep(8)
        if self._is_connection_reset():
            self._log("检测到连接异常，终止。")
            return False

        for attempt in range(1, self.max_retries + 1):
            if self.is_bypassed():
                return True

            self._log(f"第 {attempt} 次尝试处理验证页。")
            self._click_verification_button()
            time.sleep(2)

            if self._is_connection_reset():
                self._log("检测到连接异常，终止。")
                return False

        return self.is_bypassed()


def build_browser() -> ChromiumPage:
    options = ChromiumOptions().auto_port()
    if HEADLESS:
        options.headless(True)
    return ChromiumPage(addr_or_opts=options)


def extract_cookies(page: ChromiumPage) -> dict[str, str]:
    cookies = {}
    for cookie in page.cookies():
        name = cookie.get("name")
        value = cookie.get("value")
        if name:
            cookies[name] = value or ""
    return cookies


def main() -> None:
    page = build_browser()
    try:
        print(f"opening: {TARGET_URL}")
        page.get(TARGET_URL)
        bypassed = CloudflareByPasser(page, max_retries=MAX_RETRIES).bypass()
        cookies = extract_cookies(page)

        print("\n===== result =====")
        print(f"bypassed: {bypassed}")
        print(f"title: {page.title}")
        print(f"url: {page.url}")
        print(f"user_agent: {page.user_agent}")
        print(f"cookies: {cookies}")
        print("\n===== html preview =====")
        print((page.html or "")[:2000])
    finally:
        page.quit()


if __name__ == "__main__":
    main()
