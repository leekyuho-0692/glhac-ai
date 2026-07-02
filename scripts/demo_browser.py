"""Headless 브라우저 시연 — 로그인 후 전 화면 스크린샷."""
import os
from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "demo_shots")
os.makedirs(OUT, exist_ok=True)
BASE = "http://127.0.0.1:8800/ui/"

SCREENS = [
    ("application", "03_application"), ("materials", "04_materials"),
    ("documents", "05_documents"), ("sjphEvaluation", "06_sjph"),
    ("audit", "07_audit"), ("invoice", "08_invoice"),
    ("fatwa", "09_fatwa"), ("certificate", "10_certificate"),
    ("assistant", "11_assistant"),
]


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        pg.goto(BASE)
        pg.wait_for_timeout(1500)
        pg.screenshot(path=f"{OUT}/01_login.png")
        print("login shot")
        # 데모 계정 consultant1 클릭 → 로그인
        try:
            pg.click("text=consultant1", timeout=5000)
        except Exception:
            pg.evaluate("quickLogin('consultant1','pw')")
        pg.wait_for_timeout(3000)
        pg.screenshot(path=f"{OUT}/02_dashboard.png", full_page=True)
        print("dashboard shot")
        for sec, name in SCREENS:
            try:
                pg.evaluate("go('%s')" % sec)
                pg.wait_for_timeout(2200)
                pg.screenshot(path=f"{OUT}/{name}.png", full_page=True)
                print(name, "shot")
            except Exception as e:  # noqa: BLE001
                print(name, "FAIL", e)
        b.close()
    print("DONE ->", OUT)


if __name__ == "__main__":
    main()
