from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PlatformAuth:
    name: str
    home_url: str
    cookie_domain: str


PLATFORMS = {
    "51job": PlatformAuth("51job", "https://we.51job.com/", "51job.com"),
    "liepin": PlatformAuth("liepin", "https://www.liepin.com/", "liepin.com"),
    "boss": PlatformAuth("boss", "https://www.zhipin.com/", "zhipin.com"),
}


def auth_dir(root: Path) -> Path:
    override = os.getenv("JOB_AGENT_AUTH_DIR")
    path = Path(override) if override else root / "data" / "auth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_state_path(root: Path, platform: str) -> Path:
    return auth_dir(root) / f"{platform}_storage_state.json"


def cookie_file_path(root: Path, platform: str) -> Path:
    return auth_dir(root) / f"{platform}_cookies.json"


def login_with_browser(root: Path, platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")

    from playwright.sync_api import sync_playwright

    info = PLATFORMS[platform]
    state_path = storage_state_path(root, platform)
    cookie_path = cookie_file_path(root, platform)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            storage_state=str(state_path) if state_path.exists() else None,
        )
        page = context.new_page()
        page.goto(info.home_url, wait_until="domcontentloaded", timeout=45000)
        print(f"Browser opened for {platform}: {info.home_url}")
        print("Log in manually, complete any platform verification, then return here.")
        input("Press Enter after login is complete...")
        context.storage_state(path=str(state_path))
        cookies = [cookie for cookie in context.cookies() if info.cookie_domain in cookie.get("domain", "")]
        cookie_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
    return state_path


def load_storage_state(root: Path, platform: str) -> str | None:
    path = storage_state_path(root, platform)
    return str(path) if path.exists() else None


def load_cookie_header_from_state(root: Path, platform: str) -> str:
    path = storage_state_path(root, platform)
    if not path.exists():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    domain = PLATFORMS[platform].cookie_domain
    pairs = []
    for cookie in data.get("cookies", []):
        if domain in cookie.get("domain", "") and cookie.get("name") and cookie.get("value"):
            pairs.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(pairs)
