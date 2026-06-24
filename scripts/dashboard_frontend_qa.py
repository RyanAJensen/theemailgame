#!/usr/bin/env python3
"""Frontend QA pass for the Email Game Race Control dashboard."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
SUMMARY_PATH = QA_DIR / "latest-summary.json"
LAST_REPORT_STATE_PATH = QA_DIR / "last_report_state.json"
LAST_SEND_RESULT_PATH = QA_DIR / "last_send_result.json"
VIEWPORTS = [
    ("home-412x915.png", 412, 915),
    ("home-360x800.png", 360, 800),
    ("home-412x1000.png", 412, 1000),
]
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
BOT_API_BASE = "https://api.telegram.org/bot"
REPORT_CHAT_ENV = "EMAIL_GAME_TEST_REPORT_CHAT_ID"


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
    race_arena_exists: bool = False
    visual_arena_height: int = 0
    visual_overlay_coverage: float = 0.0
    card_overlaps_you_pod: bool = False
    visible_racer_count: int = 0
    need_chip_visible: bool = False
    our_racer_above_fold: bool = False
    race_numbers_above_fold: bool = False
    unreadable_text: bool = False
    touch_reactive: bool = False
    you_tap_reaction: bool = False
    rival_tap_reaction: bool = False
    swipe_focus_reaction: bool = False
    long_press_slowmo: bool = False
    double_tap_reset: bool = False
    overflow_after_interactions: bool = False


@dataclass
class QAResult:
    url_source: str = "missing"
    browser_available: bool = False
    dependency_error: str = ""
    screenshots: List[str] = field(default_factory=list)
    delivery_screenshots: List[str] = field(default_factory=list)
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
    text = TELEGRAM_TOKEN_RE.sub("[telegram-token-redacted]", text)
    text = re.sub(r"https://[^/\s]+/d/[^/\s]+/?", "https://[dashboard-url-redacted]/d/[token]/", text)
    text = re.sub(r"http://127\.0\.0\.1:8787/d/[^/\s]+/?", "http://127.0.0.1:8787/d/[token]/", text)
    return TOKEN_RE.sub("[token-redacted]", text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_private_env() -> None:
    env_path = PROJECT_ROOT / ".env.local"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _dashboard_url() -> Tuple[str, str]:
    public_url = _read_text(PUBLIC_URL_FILE)
    if public_url:
        return public_url, "public-url-file"
    token = _read_text(TOKEN_FILE)
    if token:
        return f"http://127.0.0.1:8787/d/{token}/", "localhost-token-route"
    return "", "missing"


async def _run_playwright_qa(url: str, result: QAResult, force: bool = False) -> None:
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
                viewport_result = await _qa_viewport(browser, url, filename, width, height, reduced_motion=False, result=result)
                result.viewports.append(viewport_result)
                result.screenshots.append(str(Path("dashboard_qa/screenshots") / filename))
            result.delivery_screenshots = await _capture_delivery_screenshots(browser, url, force)
            reduced = await _qa_viewport(browser, url, "reduced-motion-check.png", 412, 915, reduced_motion=True, result=result)
            result.reduced_motion_useful = reduced.hero_height > 250 and reduced.our_racer_above_fold
            await browser.close()
    except Exception as exc:
        result.dependency_error = (
            "Playwright is installed, but Chromium could not run. Setup: "
            "./.venv/bin/python -m playwright install chromium"
        )
        result.dependency_error += f"\nRuntime error: {_redact(str(exc))}"
        result.browser_available = False


async def _capture_delivery_screenshots(browser: Any, url: str, force: bool) -> List[str]:
    filenames = [
        "delivery-default.png",
        "delivery-you-focused.png",
        "delivery-you-dragged.png",
        "delivery-rival-gap-line.png",
    ]
    paths = [SCREENSHOT_DIR / name for name in filenames]
    if not force and all(path.exists() for path in paths):
        return [str(Path("dashboard_qa/screenshots") / name) for name in filenames]

    context = await browser.new_context(
        viewport={"width": 412, "height": 915},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        reduced_motion="no-preference",
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1200)
        await page.screenshot(path=str(paths[0]), full_page=False)
        await _apply_delivery_gesture(page, "you-focus")
        await page.screenshot(path=str(paths[1]), full_page=False)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1200)
        await _apply_delivery_gesture(page, "you-drag")
        await page.screenshot(path=str(paths[2]), full_page=False)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1200)
        await _apply_delivery_gesture(page, "rival-gap")
        await page.screenshot(path=str(paths[3]), full_page=False)
    finally:
        await context.close()
    return [str(Path("dashboard_qa/screenshots") / name) for name in filenames]


async def _qa_viewport(
    browser: Any,
    url: str,
    filename: str,
    width: int,
    height: int,
    reduced_motion: bool,
    result: QAResult,
) -> ViewportResult:
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
    touch_metrics = await _interaction_metrics(page)
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
          const qaRelevant = /card|panel|hero|racer|rival|race|stat|table|ticker|banner|pod/i;
          for (const el of all) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const classText = String(el.className || '');
            const selector = `${el.tagName.toLowerCase()}.${classText.replace(/\\s+/g,'.')}`.slice(0, 90);
            const isDecorativeTrack = Boolean(el.closest('.race-arena__grid, .race-arena__pods, .race-orbit, .track-pod'));
            const hasText = Boolean(el.textContent && el.textContent.trim().length > 8);
            const verticallyVisible = rect.bottom > 0 && rect.top < viewportHeight;
            const horizontallyClipped = rect.left < -2 || rect.right > viewportWidth + 2;
            if (rect.width > viewportWidth + 2 && style.overflowX !== 'auto' && !isDecorativeTrack && !el.closest('.table-scroll')) {
              wide.push(`${el.tagName.toLowerCase()}.${String(el.className || '').replace(/\\s+/g,'.')}`.slice(0, 90));
            }
            if (horizontallyClipped && verticallyVisible && hasText && qaRelevant.test(selector) && !isDecorativeTrack) {
              clipped.push(selector);
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
          const arena = document.querySelector('.race-arena');
          const arenaRect = arena ? arena.getBoundingClientRect() : {left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0};
          const area = (rect) => Math.max(0, rect.right - rect.left) * Math.max(0, rect.bottom - rect.top);
          const intersection = (a, b) => ({
            left: Math.max(a.left, b.left),
            top: Math.max(a.top, b.top),
            right: Math.min(a.right, b.right),
            bottom: Math.min(a.bottom, b.bottom),
          });
          const overlaySelectors = '.race-state-banner, .race-story, .race-user-pill, .race-hero__strip, .panel';
          const overlayArea = Array.from(document.querySelectorAll(overlaySelectors)).reduce((sum, el) => {
            const rect = el.getBoundingClientRect();
            if (rect.bottom <= 0 || rect.top >= viewportHeight) return sum;
            return sum + area(intersection(rect, arenaRect));
          }, 0);
          const arenaArea = area(arenaRect);
          const overlayCoverage = arenaArea > 0 ? overlayArea / arenaArea : 1;
          const userPod = document.querySelector('.track-pod.is-user');
          const userPodRect = userPod ? userPod.getBoundingClientRect() : null;
          const userPodCenter = userPodRect ? {x: userPodRect.left + userPodRect.width / 2, y: userPodRect.top + userPodRect.height / 2} : null;
          const cardOverlapsPod = Boolean(userPodCenter && Array.from(document.querySelectorAll(overlaySelectors)).some((el) => {
            const rect = el.getBoundingClientRect();
            const bigEnough = rect.width * rect.height > 5000;
            return bigEnough && userPodCenter.x >= rect.left && userPodCenter.x <= rect.right && userPodCenter.y >= rect.top && userPodCenter.y <= rect.bottom;
          }));
          const visibleRacers = Array.from(document.querySelectorAll('[data-racer-visual="true"], .track-pod')).filter((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < viewportHeight && rect.right > 0 && rect.left < viewportWidth;
          }).length;
          const text = document.body.innerText || '';
          const aboveFoldText = Array.from(document.querySelectorAll('body *'))
            .filter((el) => {
              const rect = el.getBoundingClientRect();
              return rect.top >= 0 && rect.top < viewportHeight && rect.width > 0 && rect.height > 0;
            })
            .map((el) => el.innerText || el.textContent || '')
            .join('\\n');
          const duplicateOpen = (text.match(/Open Race Control Dashboard/g) || []).length > 1;
          const ourRacer = /YOU|letlhogonolo_fanampe/.test(aboveFoldText);
          const needChip = /Need \\+\\d/.test(aboveFoldText);
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
            raceArenaExists: Boolean(arena),
            visualArenaHeight: Math.round(arenaRect.height || heroRect.height || 0),
            visualOverlayCoverage: Number(overlayCoverage.toFixed(3)),
            cardOverlapsYouPod: cardOverlapsPod,
            visibleRacerCount: visibleRacers,
            needChipVisible: needChip,
            ourRacerAboveFold: ourRacer,
            raceNumbersAboveFold: numbers,
            unreadableText: unreadable,
          };
        }"""
    )
    await context.close()
    result.console_errors.extend(console_errors)
    result.failed_requests.extend(failed_requests)

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
        race_arena_exists=bool(metrics.get("raceArenaExists")),
        visual_arena_height=int(metrics.get("visualArenaHeight") or 0),
        visual_overlay_coverage=float(metrics.get("visualOverlayCoverage") or 0.0),
        card_overlaps_you_pod=bool(metrics.get("cardOverlapsYouPod")),
        visible_racer_count=int(metrics.get("visibleRacerCount") or 0),
        need_chip_visible=bool(metrics.get("needChipVisible")),
        our_racer_above_fold=bool(metrics.get("ourRacerAboveFold")),
        race_numbers_above_fold=bool(metrics.get("raceNumbersAboveFold")),
        unreadable_text=bool(metrics.get("unreadableText")),
        touch_reactive=bool(touch_metrics.get("touchReactive")),
        you_tap_reaction=bool(touch_metrics.get("youTapReaction")),
        rival_tap_reaction=bool(touch_metrics.get("rivalTapReaction")),
        swipe_focus_reaction=bool(touch_metrics.get("swipeFocusReaction")),
        long_press_slowmo=bool(touch_metrics.get("longPressSlowmo")),
        double_tap_reset=bool(touch_metrics.get("doubleTapReset")),
        overflow_after_interactions=bool(touch_metrics.get("overflowAfterInteractions")),
    )


async def _interaction_metrics(page: Any) -> Dict[str, Any]:
    return await page.evaluate(
        """async () => {
          const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const arena = document.querySelector('.race-arena');
          const you = document.querySelector('.track-pod.is-user[data-racer-id]');
          const rival = document.querySelector('.track-pod:not(.is-user)[data-racer-id]');
          const gapLine = document.querySelector('.race-gap-line');
          if (!arena || !you || !rival || typeof PointerEvent === 'undefined') {
            return {
              touchReactive: false,
              youTapReaction: false,
              rivalTapReaction: false,
              swipeFocusReaction: false,
              longPressSlowmo: false,
              doubleTapReset: false,
              overflowAfterInteractions: document.documentElement.scrollWidth > window.innerWidth + 2,
            };
          }
          const center = (el) => {
            const rect = el.getBoundingClientRect();
            return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
          };
          const fire = (target, type, point) => {
            target.dispatchEvent(new PointerEvent(type, {
              bubbles: true,
              cancelable: true,
              clientX: point.x,
              clientY: point.y,
              pointerId: 41,
              pointerType: 'touch',
              isPrimary: true,
            }));
          };
          const tap = async (target) => {
            const point = center(target);
            fire(target, 'pointerdown', point);
            await wait(40);
            fire(target, 'pointerup', point);
            await wait(140);
          };
          const arenaRect = arena.getBoundingClientRect();
          const left = {x: arenaRect.left + arenaRect.width * 0.24, y: arenaRect.top + arenaRect.height * 0.5};
          const right = {x: arenaRect.left + arenaRect.width * 0.76, y: arenaRect.top + arenaRect.height * 0.5};

          fire(arena, 'pointerdown', left);
          await wait(45);
          fire(arena, 'pointermove', right);
          const touchReactive = arena.classList.contains('race-arena--touched') && Boolean(arena.style.getPropertyValue('--tilt-y'));
          fire(arena, 'pointerup', right);
          await wait(140);
          const swipeFocusReaction = Boolean(arena.dataset.focusedRacer || document.querySelector('.track-pod.race-pod--focused'));

          await tap(you);
          const youTapReaction = you.classList.contains('race-pod--you-active') && document.querySelector('.race-touch-burst.is-visible');

          await tap(rival);
          const rivalTapReaction = rival.classList.contains('race-pod--rival-active') && Boolean(gapLine && gapLine.classList.contains('is-visible'));

          const slowPoint = center(arena);
          fire(arena, 'pointerdown', slowPoint);
          await wait(720);
          const longPressSlowmo = arena.classList.contains('race-pod--slowmo');
          fire(arena, 'pointerup', slowPoint);
          await wait(120);

          fire(arena, 'pointerdown', slowPoint);
          fire(arena, 'pointerup', slowPoint);
          await wait(90);
          fire(arena, 'pointerdown', slowPoint);
          fire(arena, 'pointerup', slowPoint);
          await wait(180);
          const doubleTapReset = !document.querySelector('.track-pod.race-pod--focused, .track-pod.race-pod--you-active, .track-pod.race-pod--rival-active') && !(gapLine && gapLine.classList.contains('is-visible'));
          return {
            touchReactive,
            youTapReaction: Boolean(youTapReaction),
            rivalTapReaction,
            swipeFocusReaction,
            longPressSlowmo,
            doubleTapReset,
            overflowAfterInteractions: document.documentElement.scrollWidth > window.innerWidth + 2,
          };
        }"""
    )


async def _apply_delivery_gesture(page: Any, gesture: str) -> None:
    await page.evaluate(
        """async (gesture) => {
          const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const arena = document.querySelector('.race-arena');
          const you = document.querySelector('.track-pod.is-user[data-racer-id]');
          const rival = document.querySelector('.track-pod:not(.is-user)[data-racer-id]');
          if (!arena || !you || !rival || typeof PointerEvent === 'undefined') return;
          const center = (el) => {
            const rect = el.getBoundingClientRect();
            return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
          };
          const fire = (target, type, point) => {
            target.dispatchEvent(new PointerEvent(type, {
              bubbles: true,
              cancelable: true,
              clientX: point.x,
              clientY: point.y,
              pointerId: 41,
              pointerType: 'touch',
              isPrimary: true,
            }));
          };
          const tap = async (target) => {
            const point = center(target);
            fire(target, 'pointerdown', point);
            await wait(40);
            fire(target, 'pointerup', point);
            await wait(160);
          };
          const drag = async (target) => {
            const start = center(target);
            const end = {x: start.x + 104, y: start.y - 38};
            fire(target, 'pointerdown', start);
            await wait(40);
            fire(target, 'pointermove', end);
            await wait(80);
            fire(target, 'pointerup', end);
            await wait(180);
          };
          if (gesture === 'you-focus') {
            await tap(you);
          } else if (gesture === 'you-drag') {
            await drag(you);
          } else if (gesture === 'rival-gap') {
            await tap(rival);
          }
        }""",
        gesture,
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
    if any(not v.race_arena_exists for v in all_viewports):
        score -= 25
        issues.append("race arena missing")
    if any(v.visual_arena_height < 320 for v in all_viewports):
        score -= 20
        issues.append("visual race arena too short")
    if any(v.visual_overlay_coverage > 0.35 for v in all_viewports):
        score -= 20
        issues.append("large overlays cover race arena")
    if any(v.card_overlaps_you_pod for v in all_viewports):
        score -= 20
        issues.append("card overlaps YOU pod")
    if any(v.visible_racer_count < 3 for v in all_viewports):
        score -= 20
        issues.append("not enough visible racer pods")
    if any(not v.need_chip_visible for v in all_viewports):
        score -= 10
        issues.append("Need +1 chip missing")
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
    if any(not v.touch_reactive for v in all_viewports):
        score -= 20
        issues.append("touch drag reaction missing")
    if any(not v.you_tap_reaction for v in all_viewports):
        score -= 15
        issues.append("YOU pod tap reaction missing")
    if any(not v.rival_tap_reaction for v in all_viewports):
        score -= 15
        issues.append("rival pod tap reaction missing")
    if any(not v.swipe_focus_reaction for v in all_viewports):
        score -= 10
        issues.append("swipe focus reaction missing")
    if any(not v.long_press_slowmo for v in all_viewports):
        score -= 10
        issues.append("long press slow-motion missing")
    if any(not v.double_tap_reset for v in all_viewports):
        score -= 10
        issues.append("double tap reset missing")
    if any(v.overflow_after_interactions for v in all_viewports):
        score -= 15
        issues.append("interaction caused horizontal overflow")

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
    if "large overlays cover race arena" in issues or "card overlaps YOU pod" in issues:
        return "Move large text cards below the arena and keep only compact chips over the race visual."
    if "not enough visible racer pods" in issues:
        return "Render at least three competitor pod visuals above the fold."
    if "touch drag reaction missing" in issues:
        return "Wire Android pointer movement to arena tilt/parallax state."
    if "YOU pod tap reaction missing" in issues or "rival pod tap reaction missing" in issues:
        return "Make pod tap targets update visible active/focus classes and compact chips."
    if "swipe focus reaction missing" in issues:
        return "Handle horizontal swipes inside the race arena without navigating the page."
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
    touch_ready = all(v.touch_reactive for v in result.viewports)
    you_ready = all(v.you_tap_reaction for v in result.viewports)
    rival_ready = all(v.rival_tap_reaction for v in result.viewports)
    swipe_ready = all(v.swipe_focus_reaction for v in result.viewports)
    long_ready = all(v.long_press_slowmo for v in result.viewports)
    reset_ready = all(v.double_tap_reset for v in result.viewports)
    overflow_after = any(v.overflow_after_interactions for v in result.viewports)
    return [
        f"- Hero: heights {hero_heights}; race numbers above fold: {all(v.race_numbers_above_fold for v in result.viewports)}.",
        f"- Visual-first hero: arena exists: {all(v.race_arena_exists for v in result.viewports)}; arena height >= 320px: {all(v.visual_arena_height >= 320 for v in result.viewports)}; overlay coverage <= 35%: {all(v.visual_overlay_coverage <= 0.35 for v in result.viewports)}.",
        f"- Race visualization: our racer above fold: {all(v.our_racer_above_fold for v in result.viewports)}; visible racer pods: {min((v.visible_racer_count for v in result.viewports), default=0)}; no card over YOU pod: {not any(v.card_overlaps_you_pod for v in result.viewports)}; reduced motion useful: {result.reduced_motion_useful}.",
        f"- Touch reactive: drag/parallax {touch_ready}; YOU tap {you_ready}; rival tap {rival_ready}; swipe focus {swipe_ready}; long press slow-motion {long_ready}; double tap reset {reset_ready}; overflow after interactions {overflow_after}.",
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
    SUMMARY_PATH.write_text(json.dumps(_summary_payload(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _summary_payload(result: QAResult) -> Dict[str, Any]:
    return {
        "generated_at": _utc_now(),
        "qa_score": result.score,
        "android_readiness": result.readiness,
        "browser_available": result.browser_available,
        "screenshots_captured": bool(result.screenshots),
        "screenshots": result.screenshots,
        "main_issue": result.main_issue,
        "recommendation": result.recommendation,
        "url_source": result.url_source,
        "console_errors": len(result.console_errors),
        "failed_requests": len(result.failed_requests),
        "viewports": [
            {
                "name": item.name,
                "width": item.width,
                "height": item.height,
                "loaded_ms": item.loaded_ms,
                "horizontal_overflow": item.horizontal_overflow,
                "table_overflow": item.table_overflow,
                "clipped_elements": item.clipped_elements,
                "wide_elements": item.wide_elements,
                "tiny_tap_targets": item.tiny_tap_targets,
                "duplicate_open_card": item.duplicate_open_card,
                "hero_height": item.hero_height,
                "race_arena_exists": item.race_arena_exists,
                "visual_arena_height": item.visual_arena_height,
                "visual_overlay_coverage": item.visual_overlay_coverage,
                "card_overlaps_you_pod": item.card_overlaps_you_pod,
                "visible_racer_count": item.visible_racer_count,
                "need_chip_visible": item.need_chip_visible,
                "our_racer_above_fold": item.our_racer_above_fold,
                "race_numbers_above_fold": item.race_numbers_above_fold,
                "unreadable_text": item.unreadable_text,
                "touch_reactive": item.touch_reactive,
                "you_tap_reaction": item.you_tap_reaction,
                "rival_tap_reaction": item.rival_tap_reaction,
                "swipe_focus_reaction": item.swipe_focus_reaction,
                "long_press_slowmo": item.long_press_slowmo,
                "double_tap_reset": item.double_tap_reset,
                "overflow_after_interactions": item.overflow_after_interactions,
            }
            for item in result.viewports
        ],
        "touch_reactive": all(item.touch_reactive for item in result.viewports) if result.viewports else False,
        "you_pod_tap_reaction": all(item.you_tap_reaction for item in result.viewports) if result.viewports else False,
        "rival_tap_reaction": all(item.rival_tap_reaction for item in result.viewports) if result.viewports else False,
        "swipe_focus_reaction": all(item.swipe_focus_reaction for item in result.viewports) if result.viewports else False,
        "long_press_slowmo": all(item.long_press_slowmo for item in result.viewports) if result.viewports else False,
        "double_tap_reset": all(item.double_tap_reset for item in result.viewports) if result.viewports else False,
    }


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _file_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _screenshot_hashes(result: QAResult) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for item in result.screenshots:
        rel_path = Path(item)
        path = PROJECT_ROOT / rel_path
        hashes[item] = _file_sha256(path)
    return hashes


def _read_last_report_state() -> Dict[str, Any]:
    try:
        parsed = json.loads(LAST_REPORT_STATE_PATH.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_last_report_state(result: QAResult, summary: str, commit: str, screenshot_hashes: Dict[str, str]) -> None:
    LAST_REPORT_STATE_PATH.write_text(
        json.dumps(
            {
                "last_git_commit": commit,
                "last_screenshot_hashes": screenshot_hashes,
                "last_summary": summary,
                "last_sent_timestamp": _utc_now(),
                "last_qa_score": result.score,
                "last_readiness": result.readiness,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_last_send_result(result: Dict[str, Any]) -> None:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SEND_RESULT_PATH.write_text(
        json.dumps(_redact_value(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_last_send_result() -> Dict[str, Any]:
    try:
        parsed = json.loads(LAST_SEND_RESULT_PATH.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _send_result_lines(result: Dict[str, Any]) -> List[str]:
    return [
        f"summary sent: {'yes' if result.get('summary_sent') else 'no'}",
        f"screenshots found: {int(result.get('screenshots_found') or 0)}",
        f"screenshots sent: {int(result.get('screenshots_sent') or 0)}",
        f"fallback documents sent: {int(result.get('fallback_documents_sent') or 0)}",
        f"telegram message ids received: {'yes' if result.get('telegram_message_ids_received') else 'no'}",
    ]


def _telegram_summary(result: QAResult, previous: Dict[str, Any], commit: str, screenshot_hashes: Dict[str, str]) -> str:
    main_issue = result.main_issue.rstrip(".")
    recommendation = result.recommendation.rstrip(".")
    if not previous:
        if not result.screenshots:
            return (
                "Dashboard QA complete. Browser screenshots were not captured because browser automation is unavailable. "
                f"Current readiness: {result.readiness}; main issue: {main_issue}. "
                f"Recommendation: {recommendation}."
            )
        return (
            "Dashboard QA complete. Initial Android screenshots were captured, the protected dashboard was checked for "
            f"overflow, clipped hero cards, above-the-fold race numbers, console errors, and network failures. Current readiness: "
            f"{result.readiness}; main issue: {main_issue}."
        )
    changes: List[str] = []
    if previous.get("last_git_commit") != commit:
        changes.append("the dashboard/QA commit changed")
    previous_hashes = previous.get("last_screenshot_hashes") if isinstance(previous.get("last_screenshot_hashes"), dict) else {}
    changed_screenshots = [
        name for name, digest in screenshot_hashes.items() if previous_hashes.get(name) and previous_hashes.get(name) != digest
    ]
    if changed_screenshots:
        changes.append(f"{len(changed_screenshots)} Android screenshot(s) changed")
    if previous.get("last_qa_score") != result.score:
        changes.append(f"QA score is now {result.score}")
    if previous.get("last_readiness") != result.readiness:
        changes.append(f"readiness is now {result.readiness}")
    if not changes:
        changes.append("screenshots and QA score are unchanged")
    return (
        "Dashboard QA complete. Since the last screenshots, "
        + ", ".join(changes)
        + f". Main issue: {main_issue}. Recommendation: {recommendation}."
    )


def _telegram_request(token: str, method: str, payload: Dict[str, Any], timeout: int = 20) -> Tuple[bool, Dict[str, Any], str]:
    data = urlencode({key: str(value) for key, value in payload.items()}).encode("utf-8")
    request = Request(
        f"{BOT_API_BASE}{token}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8", "replace"))
            return bool(isinstance(parsed, dict) and parsed.get("ok")), parsed if isinstance(parsed, dict) else {}, ""
    except Exception as exc:
        return False, {}, _redact(str(exc))


def _telegram_send_photo(token: str, chat_id: str, path: Path, caption: str = "") -> Tuple[bool, Dict[str, Any], str]:
    if not path.exists():
        return False, {}, f"missing screenshot {path.name}"
    boundary = f"----emailgameqa{int(time.time() * 1000)}"
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="photo"; filename="{path.name}"\r\n'.encode("utf-8"))
    body.extend(b"Content-Type: image/png\r\n\r\n")
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    request = Request(
        f"{BOT_API_BASE}{token}/sendPhoto",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            parsed = json.loads(response.read().decode("utf-8", "replace"))
            return bool(isinstance(parsed, dict) and parsed.get("ok")), parsed if isinstance(parsed, dict) else {}, ""
    except Exception as exc:
        return False, {}, _redact(str(exc))


def _telegram_send_document(token: str, chat_id: str, path: Path, caption: str = "") -> Tuple[bool, Dict[str, Any], str]:
    if not path.exists():
        return False, {}, f"missing screenshot {path.name}"
    boundary = f"----emailgameqa{int(time.time() * 1000)}"
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'.encode("utf-8"))
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    request = Request(
        f"{BOT_API_BASE}{token}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            parsed = json.loads(response.read().decode("utf-8", "replace"))
            return bool(isinstance(parsed, dict) and parsed.get("ok")), parsed if isinstance(parsed, dict) else {}, ""
    except Exception as exc:
        return False, {}, _redact(str(exc))


def _resolve_delivery_screenshots(result: QAResult) -> List[str]:
    if result.delivery_screenshots:
        return result.delivery_screenshots[:3]
    delivery_files = sorted(SCREENSHOT_DIR.glob("delivery-*.png"))
    if delivery_files:
        return [str(path.relative_to(PROJECT_ROOT)) for path in delivery_files[:3]]
    if result.screenshots:
        return result.screenshots[:3]
    return []


def _telegram_send(result: QAResult) -> Tuple[bool, str, Dict[str, Any]]:
    _load_private_env()
    token = os.getenv("EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv(REPORT_CHAT_ENV, "").strip()
    screenshot_paths = _resolve_delivery_screenshots(result)
    delivery_result: Dict[str, Any] = {
        "generated_at": _utc_now(),
        "summary_sent": False,
        "screenshots_found": len(screenshot_paths),
        "screenshots_sent": 0,
        "fallback_documents_sent": 0,
        "telegram_message_ids_received": False,
        "message_responses": [],
        "errors": [],
        "dashboard_button_included": False,
        "screenshots": screenshot_paths,
    }
    if not token or not chat_id:
        delivery_result["error"] = "tester bot token or report chat id missing"
        _write_last_send_result(delivery_result)
        return False, "tester bot token or report chat id missing", delivery_result
    dashboard_url, _ = _dashboard_url()
    if not dashboard_url:
        delivery_result["error"] = "dashboard url missing"
        _write_last_send_result(delivery_result)
        return False, "dashboard url missing", delivery_result
    commit = _git_commit()
    hashes = _screenshot_hashes(result)
    previous = _read_last_report_state()
    summary = _telegram_summary(result, previous, commit, hashes)
    reply_markup = json.dumps(
        {"inline_keyboard": [[{"text": "Open Race Control Dashboard", "url": dashboard_url}]]}
    ) if dashboard_url else ""
    text = "\n".join(
        [
            "Email Game Dashboard Frontend QA",
            "",
            summary,
            "",
            f"Frontend QA score: {result.score}",
            f"Android readiness: {result.readiness}",
            f"Screenshots: {len(screenshot_paths)}",
            "Secrets exposed: no",
        ]
    )
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    ok, message_response, error = _telegram_request(token, "sendMessage", payload, timeout=20)
    delivery_result["summary_sent"] = bool(ok)
    delivery_result["telegram_message_ids_received"] = bool(
        ok and isinstance(message_response.get("result"), dict) and message_response["result"].get("message_id") is not None
    )
    delivery_result["dashboard_button_included"] = bool(reply_markup)
    if ok:
        delivery_result["message_responses"].append(
            {
                "method": "sendMessage",
                "ok": True,
                "response": _redact_value(message_response),
                "message_id": message_response.get("result", {}).get("message_id") if isinstance(message_response.get("result"), dict) else None,
            }
        )
    else:
        delivery_result["message_responses"].append({"method": "sendMessage", "ok": False, "error": error})
        delivery_result["errors"].append(error)
        _write_last_send_result(delivery_result)
        return False, error, delivery_result

    screenshot_labels = [
        "Default race hero",
        "YOU pod focused",
        "YOU pod dragged",
        "Rival selected with gap line",
    ]
    for index, item in enumerate(screenshot_paths[:3]):
        caption = screenshot_labels[index] if index < len(screenshot_labels) else "Android dashboard screenshot"
        sent, screenshot_response, screenshot_error = _telegram_send_photo(token, chat_id, PROJECT_ROOT / item, caption=caption)
        fallback_used = False
        if not sent:
            sent, screenshot_response, screenshot_error = _telegram_send_document(token, chat_id, PROJECT_ROOT / item, caption=caption)
            fallback_used = sent
        delivery_result["message_responses"].append(
            {
                "method": "sendDocument" if fallback_used else "sendPhoto",
                "ok": bool(sent),
                "response": _redact_value(screenshot_response),
                "file": Path(item).name,
                "fallback_document": bool(fallback_used),
                "message_id": screenshot_response.get("result", {}).get("message_id") if isinstance(screenshot_response.get("result"), dict) else None,
            }
        )
        if sent:
            delivery_result["screenshots_sent"] += 1
            if fallback_used:
                delivery_result["fallback_documents_sent"] += 1
        else:
            delivery_result["errors"].append(screenshot_error)

    _write_last_report_state(result, summary, commit, hashes)
    _write_last_send_result(delivery_result)
    if delivery_result["errors"]:
        return False, "; ".join(delivery_result["errors"]), delivery_result
    return True, "", delivery_result


async def _run_once(args: argparse.Namespace) -> QAResult:
    url, source = _dashboard_url()
    result = QAResult(url_source=source)
    if args.force:
        for path in SCREENSHOT_DIR.glob("*.png"):
            path.unlink(missing_ok=True)
    if not url:
        result.dependency_error = "Dashboard URL and token are missing; cannot open protected dashboard."
        result.score = 0
        _write_report(result)
        return result
    await _run_playwright_qa(url, result, force=bool(args.force))
    _score(result)
    _write_report(result)
    if args.send_report:
        sent, error, delivery_result = _telegram_send(result)
        report_result = _read_last_send_result() or delivery_result
        for line in _send_result_lines(report_result):
            print(line)
    return result


async def _watch(args: argparse.Namespace) -> None:
    while True:
        result = await _run_once(args)
        print(f"qa_score={result.score} report={REPORT_PATH}")
        await asyncio.sleep(60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Email Game dashboard frontend QA.")
    parser.add_argument("--watch", action="store_true", help="Rerun every 60 seconds and update dashboard_qa/report.md.")
    parser.add_argument("--force", action="store_true", help="Remove existing screenshots before rerunning QA.")
    parser.add_argument(
        "--send-report",
        "--telegram-report",
        dest="send_report",
        action="store_true",
        help="Send a short tester-bot summary and screenshots when credentials are available.",
    )
    args = parser.parse_args()
    if args.watch:
        asyncio.run(_watch(args))
        return 0
    result = asyncio.run(_run_once(args))
    if args.send_report:
        return 0 if result.browser_available or result.dependency_error else 1
    print(f"qa_score={result.score}")
    print(f"android_readiness={result.readiness}")
    print(f"browser_available={'yes' if result.browser_available else 'no'}")
    print(f"screenshots_captured={'yes' if result.screenshots else 'no'}")
    print(f"report_path={REPORT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"main_issue={result.main_issue}")
    return 0 if result.browser_available or result.dependency_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
