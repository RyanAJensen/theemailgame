#!/usr/bin/env python3
"""Frontend QA pass for the Email Game Race Control dashboard."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_LOGS = PROJECT_ROOT / "agent_logs"
PUBLIC_URL_FILE = AGENT_LOGS / "emailgame-dashboard-url.txt"
TOKEN_FILE = AGENT_LOGS / "emailgame-dashboard-token.txt"
QA_DIR = PROJECT_ROOT / "dashboard_qa"
SCREENSHOT_DIR = QA_DIR / "screenshots"
REPORT_PATH = QA_DIR / "report.md"
VIEWPORTS = [
    ("home-412x915.png", 412, 915),
    ("home-360x800.png", 360, 800),
    ("home-412x1000.png", 412, 1000),
]
TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
BOT_API_BASE = "https://api.telegram.org/bot"


@dataclass
class ViewportResult:
    name: str
    width: int
    height: int
    screenshot: str
    loaded_ms: Optional[int] = None
    horizontal_overflow: bool = False
    max_scroll_width: int = 0
    wide_elements: List[str] = field(default_factory=list)
    clipped_elements: List[str] = field(default_factory=list)
    table_overflow: bool = False
    tiny_tap_targets: int = 0
    duplicate_open_card: bool = False
    hero_height: int = 0
    our_racer_above_fold: bool = False
    race_numbers_above_fold: bool = False
    unreadable_text: bool = False


@dataclass
class QAResult:
    url_source: str = "missing"
    browser_available: bool = False
    dependency_error: str = ""
    screenshots: List[str] = field(default_factory=list)
    console_errors: List[str] = field(default_factory=list)
    failed_requests: List[str] = field(default_factory=list)
    viewports: List[ViewportResult] = field(default_factory=list)
    reduced_motion_useful: bool = False
    score: int = 100
    readiness: str = "poor"
    main_issue: str = "Browser QA could not run."
    recommendation: str = "Install Playwright browser support, then rerun dashboard QA."


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _redact(text: str) -> str:
    text = re.sub(r"https://[^/\s]+/d/[^/\s]+/?", "https://[dashboard-url-redacted]/d/[token]/", text)
    text = re.sub(r"http://127\.0\.0\.1:8787/d/[^/\s]+/?", "http://127.0.0.1:8787/d/[token]/", text)
    return TOKEN_RE.sub("[token-redacted]", text)


def _dashboard_url() -> Tuple[str, str]:
    public_url = _read_text(PUBLIC_URL_FILE)
    if public_url:
        return public_url, "public-url-file"
    token = _read_text(TOKEN_FILE)
    if token:
        return f"http://127.0.0.1:8787/d/{token}/", "localhost-token-route"
    return "", "missing"


async def _run_playwright_qa(url: str, result: QAResult) -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        result.dependency_error = (
            "Python Playwright is not installed. Setup: ./.venv/bin/python -m pip install playwright "
            "&& ./.venv/bin/python -m playwright install chromium"
        )
        result.dependency_error += f"\nImport error: {_redact(str(exc))}"
        return

    result.browser_available = True
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for filename, width, height in VIEWPORTS:
                viewport_result = await _qa_viewport(browser, url, filename, width, height, reduced_motion=False)
                result.viewports.append(viewport_result)
                result.screenshots.append(str(Path("dashboard_qa/screenshots") / filename))
            reduced = await _qa_viewport(browser, url, "reduced-motion-check.png", 412, 915, reduced_motion=True)
            result.reduced_motion_useful = reduced.hero_height > 250 and reduced.our_racer_above_fold
            await browser.close()
    except Exception as exc:
        result.dependency_error = (
            "Playwright is installed, but Chromium could not run. Setup: "
            "./.venv/bin/python -m playwright install chromium"
        )
        result.dependency_error += f"\nRuntime error: {_redact(str(exc))}"
        result.browser_available = False


async def _qa_viewport(browser: Any, url: str, filename: str, width: int, height: int, reduced_motion: bool) -> ViewportResult:
    console_errors: List[str] = []
    failed_requests: List[str] = []
    context = await browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce" if reduced_motion else "no-preference",
    )
    page = await context.new_page()
    page.on("console", lambda msg: console_errors.append(_redact(msg.text)) if msg.type == "error" else None)
    page.on("requestfailed", lambda req: failed_requests.append(_redact(req.url)))

    started = time.monotonic()
    await page.goto(url, wait_until="networkidle", timeout=30000)
    loaded_ms = int((time.monotonic() - started) * 1000)
    await page.wait_for_timeout(1200)
    screenshot_path = SCREENSHOT_DIR / filename
    await page.screenshot(path=str(screenshot_path), full_page=False)

    metrics = await page.evaluate(
        """() => {
          const viewportWidth = window.innerWidth;
          const viewportHeight = window.innerHeight;
          const all = Array.from(document.body.querySelectorAll('*'));
          const wide = [];
          const clipped = [];
          const tinyTapTargets = [];
          for (const el of all) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (rect.width > viewportWidth + 2 && style.overflowX !== 'auto') {
              wide.push(`${el.tagName.toLowerCase()}.${String(el.className || '').replace(/\\s+/g,'.')}`.slice(0, 90));
            }
            if ((rect.right < 0 || rect.left > viewportWidth || rect.bottom < 0 || rect.top > viewportHeight) && el.textContent && el.textContent.trim().length > 8) {
              clipped.push(`${el.tagName.toLowerCase()}.${String(el.className || '').replace(/\\s+/g,'.')}`.slice(0, 90));
            }
            const isTap = el.matches('a, button, [role="button"], input, select, textarea');
            if (isTap && rect.width > 0 && rect.height > 0 && (rect.width < 44 || rect.height < 44)) {
              tinyTapTargets.push(el.textContent.trim().slice(0, 40) || el.tagName.toLowerCase());
            }
          }
          const tables = Array.from(document.querySelectorAll('table'));
          const tableOverflow = tables.some((table) => {
            const wrapper = table.closest('.table-scroll');
            return table.scrollWidth > viewportWidth && !wrapper;
          });
          const hero = document.querySelector('.race-hero__canvas-wrap');
          const heroRect = hero ? hero.getBoundingClientRect() : {height: 0};
          const text = document.body.innerText || '';
          const aboveFoldText = Array.from(document.querySelectorAll('body *'))
            .filter((el) => {
              const rect = el.getBoundingClientRect();
              return rect.top >= 0 && rect.top < viewportHeight && rect.width > 0 && rect.height > 0;
            })
            .map((el) => el.innerText || el.textContent || '')
            .join('\\n');
          const duplicateOpen = (text.match(/Open Race Control Dashboard/g) || []).length > 0;
          const ourRacer = /YOU|letlhogonolo_fanampe/.test(aboveFoldText);
          const numbers = /Rank\\s*#?\\d|Score\\s*\\d|Gap to #1|Need \\+\\d/.test(aboveFoldText);
          const unreadable = all.some((el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const fontSize = parseFloat(style.fontSize || '0');
            return rect.width > 0 && rect.height > 0 && fontSize > 0 && fontSize < 10 && (el.textContent || '').trim().length > 4;
          });
          return {
            horizontalOverflow: document.documentElement.scrollWidth > viewportWidth + 2,
            maxScrollWidth: document.documentElement.scrollWidth,
            wideElements: wide.slice(0, 12),
            clippedElements: clipped.slice(0, 12),
            tableOverflow,
            tinyTapTargets: tinyTapTargets.length,
            duplicateOpenCard: duplicateOpen,
            heroHeight: Math.round(heroRect.height || 0),
            ourRacerAboveFold: ourRacer,
            raceNumbersAboveFold: numbers,
            unreadableText: unreadable,
          };
        }"""
    )
    await context.close()

    return ViewportResult(
        name=filename,
        width=width,
        height=height,
        screenshot=str(Path("dashboard_qa/screenshots") / filename),
        loaded_ms=loaded_ms,
        horizontal_overflow=bool(metrics.get("horizontalOverflow")),
        max_scroll_width=int(metrics.get("maxScrollWidth") or 0),
        wide_elements=list(metrics.get("wideElements") or []),
        clipped_elements=list(metrics.get("clippedElements") or []),
        table_overflow=bool(metrics.get("tableOverflow")),
        tiny_tap_targets=int(metrics.get("tinyTapTargets") or 0),
        duplicate_open_card=bool(metrics.get("duplicateOpenCard")),
        hero_height=int(metrics.get("heroHeight") or 0),
        our_racer_above_fold=bool(metrics.get("ourRacerAboveFold")),
        race_numbers_above_fold=bool(metrics.get("raceNumbersAboveFold")),
        unreadable_text=bool(metrics.get("unreadableText")),
    )


def _score(result: QAResult) -> None:
    score = 100
    issues: List[str] = []
    all_viewports = result.viewports
    if not result.browser_available or result.dependency_error:
        result.score = 0
        result.readiness = "poor"
        result.main_issue = "Browser automation is unavailable."
        result.recommendation = "Install Playwright and Chromium browser support, then rerun QA."
        return

    if any(v.horizontal_overflow for v in all_viewports):
        score -= 20
        issues.append("horizontal overflow")
    if any(v.clipped_elements for v in all_viewports):
        score -= 15
        issues.append("clipped hero or cards")
    if any(not v.our_racer_above_fold for v in all_viewports):
        score -= 15
        issues.append("our racer not visible above the fold")
    if any(v.duplicate_open_card for v in all_viewports):
        score -= 10
        issues.append("duplicate dashboard-open card")
    if result.console_errors:
        score -= 10
        issues.append("console errors")
    if any(v.tiny_tap_targets for v in all_viewports):
        score -= 10
        issues.append("tap targets under 44px")
    if any(v.unreadable_text for v in all_viewports):
        score -= 10
        issues.append("unreadable text")
    if any((v.loaded_ms or 0) > 3000 for v in all_viewports):
        score -= 10
        issues.append("local load over 3 seconds")
    if any(v.table_overflow for v in all_viewports):
        score -= 5
        issues.append("table overflow")

    result.score = max(0, score)
    if result.score >= 90:
        result.readiness = "excellent"
    elif result.score >= 75:
        result.readiness = "good"
    elif result.score >= 55:
        result.readiness = "fair"
    else:
        result.readiness = "poor"
    result.main_issue = issues[0] if issues else "No major mobile layout issue detected."
    result.recommendation = _recommendation(issues)


def _recommendation(issues: List[str]) -> str:
    if not issues:
        return "Keep dashboard polish focused on motion clarity and race-state readability."
    if "horizontal overflow" in issues:
        return "Fix mobile overflow first; stack or scroll wide hero/table elements cleanly."
    if "our racer not visible above the fold" in issues:
        return "Move the YOU pod and rank/score story higher in the Android viewport."
    if "tap targets under 44px" in issues:
        return "Increase interactive target sizing to at least 44px."
    return f"Address {issues[0]} before further visual polish."


def _report(result: QAResult) -> str:
    screenshots = result.screenshots or [f"dashboard_qa/screenshots/{name}" for name, _, _ in VIEWPORTS]
    finding_lines = _finding_lines(result)
    setup = ""
    if result.dependency_error:
        setup = f"\n\nDependency note: {_redact(result.dependency_error)}"
    return "\n".join(
        [
            "# Email Game Dashboard Frontend QA",
            "",
            "## Summary",
            f"- QA score: {result.score}",
            f"- Android readiness: {result.readiness}",
            f"- Main issue: {result.main_issue}",
            f"- Recommendation: {result.recommendation}",
            f"- URL source: {result.url_source}",
            "",
            "## Screenshots",
            *[f"- {Path(item).name}" for item in screenshots],
            "",
            "## Findings",
            *finding_lines,
            "",
            "## Recommended Codex changes",
            *_recommended_changes(result),
            setup,
            "",
        ]
    )


def _finding_lines(result: QAResult) -> List[str]:
    if result.dependency_error:
        return [
            "- Hero: not inspected because browser automation is unavailable.",
            "- Race visualization: not inspected because browser automation is unavailable.",
            "- Mobile layout: not inspected because browser automation is unavailable.",
            "- Readability: not inspected because browser automation is unavailable.",
            "- Overflow: not inspected because browser automation is unavailable.",
            "- Performance: not inspected because browser automation is unavailable.",
            "- Accessibility: not inspected because browser automation is unavailable.",
            "- Console/network: not inspected because browser automation is unavailable.",
        ]

    hero_heights = ", ".join(f"{v.name}: {v.hero_height}px" for v in result.viewports)
    overflow = [v.name for v in result.viewports if v.horizontal_overflow]
    tiny = sum(v.tiny_tap_targets for v in result.viewports)
    slow = [f"{v.name}: {v.loaded_ms}ms" for v in result.viewports if (v.loaded_ms or 0) > 3000]
    return [
        f"- Hero: heights {hero_heights}; race numbers above fold: {all(v.race_numbers_above_fold for v in result.viewports)}.",
        f"- Race visualization: our racer above fold: {all(v.our_racer_above_fold for v in result.viewports)}; reduced motion useful: {result.reduced_motion_useful}.",
        f"- Mobile layout: {'horizontal overflow in ' + ', '.join(overflow) if overflow else 'no horizontal overflow detected'}.",
        f"- Readability: {'unreadable small text detected' if any(v.unreadable_text for v in result.viewports) else 'no tiny readable-text issue detected'}.",
        f"- Overflow: table overflow: {any(v.table_overflow for v in result.viewports)}; wide elements: {sum(len(v.wide_elements) for v in result.viewports)}.",
        f"- Performance: {'; '.join(slow) if slow else 'all local loads under 3 seconds'}.",
        f"- Accessibility: tap targets under 44px: {tiny}.",
        f"- Console/network: console errors {len(result.console_errors)}; failed requests {len(result.failed_requests)}.",
    ]


def _recommended_changes(result: QAResult) -> List[str]:
    if result.dependency_error:
        return [
            "1. Install Playwright browser support in the venv or run QA where Playwright Chromium is available.",
            "2. Rerun `./.venv/bin/python scripts/dashboard_frontend_qa.py` after any dashboard UI change.",
            "3. Use `dashboard_qa/report.md` as the checklist for the next Codex polish pass.",
        ]
    changes = [
        "1. Keep the YOU pod and rank/score/gap story visible in the first Android viewport.",
        "2. Fix any overflow or clipped elements reported above before adding more visual effects.",
        "3. Re-run this QA script after dashboard CSS or hero changes.",
    ]
    if result.score >= 90:
        changes[0] = "1. Preserve the current mobile layout; use future polish for visual clarity rather than structure."
    return changes


def _write_report(result: QAResult) -> None:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(_report(result), encoding="utf-8")


def _telegram_send(result: QAResult) -> Tuple[bool, str]:
    token = os.getenv("EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("EMAIL_GAME_TEST_REPORT_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "tester bot token or report chat id missing"
    text = "\n".join(
        [
            "Email Game Dashboard Frontend QA",
            f"Frontend QA score: {result.score}",
            f"Main issue: {result.main_issue}",
            "Screenshot paths: dashboard_qa/screenshots/",
            f"Recommended next fix: {result.recommendation}",
        ]
    )
    data = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(f"{BOT_API_BASE}{token}/sendMessage", data=data)
    try:
        with urlopen(request, timeout=20) as response:
            parsed = json.loads(response.read().decode("utf-8", "replace"))
            return bool(parsed.get("ok")), ""
    except Exception as exc:
        return False, _redact(str(exc))


async def _run_once(args: argparse.Namespace) -> QAResult:
    url, source = _dashboard_url()
    result = QAResult(url_source=source)
    if not url:
        result.dependency_error = "Dashboard URL and token are missing; cannot open protected dashboard."
        result.score = 0
        _write_report(result)
        return result
    await _run_playwright_qa(url, result)
    _score(result)
    _write_report(result)
    if args.telegram_report:
        sent, error = _telegram_send(result)
        marker = "yes" if sent else f"no ({error})"
        print(f"telegram_report_sent={marker}")
    return result


async def _watch(args: argparse.Namespace) -> None:
    while True:
        result = await _run_once(args)
        print(f"qa_score={result.score} report={REPORT_PATH}")
        await asyncio.sleep(60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Email Game dashboard frontend QA.")
    parser.add_argument("--watch", action="store_true", help="Rerun every 60 seconds and update dashboard_qa/report.md.")
    parser.add_argument("--telegram-report", action="store_true", help="Send a short tester-bot summary when credentials are available.")
    args = parser.parse_args()
    if args.watch:
        asyncio.run(_watch(args))
        return 0
    result = asyncio.run(_run_once(args))
    print(f"qa_score={result.score}")
    print(f"android_readiness={result.readiness}")
    print(f"browser_available={'yes' if result.browser_available else 'no'}")
    print(f"screenshots_captured={'yes' if result.screenshots else 'no'}")
    print(f"report_path={REPORT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"main_issue={result.main_issue}")
    return 0 if result.browser_available or result.dependency_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
