# Frontend Agent Framework Research

## Decision

Use option C: continue with the local Playwright QA agent.

Papzin has starred several strong browser automation projects, but none are a better base for this dashboard loop than the current local script. The dashboard task is narrow: protected URL access, Android screenshots, layout assertions, overflow detection, race-story checks, and a tester-bot report. A focused Playwright script does that with fewer moving parts, no new secrets, no cloud/browser-agent runtime, and no live Email Game agent changes.

## Candidate Scoring

| repo | purpose | why relevant | language/runtime | mobile viewport support | screenshot support | visual QA support | Android/mobile friendliness | install weight | ARM64/Oracle VM compatibility | maintenance status | license | risk level | recommendation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `browser-use/browser-harness` | Thin CDP harness for AI agents controlling Chrome | Browser automation, screenshots via Pillow/CDP, Python runtime | Python 3.11+, CDP Chrome, `cdp-use`, `pillow`, `websockets` | Possible through CDP viewport/emulation, not dashboard-QA-specific | Supported through browser harness helpers | Not built for deterministic visual regression or mobile layout assertions | Medium; requires attaching or launching Chrome and writing task helpers | Medium | Likely workable if Chrome/CDP works on ARM64, but not verified | Updated 2026-06-24; active/recent metadata | MIT | Medium | Do not integrate now; useful inspiration for future CDP fallback only. Score: 70 |
| `vercel-labs/agent-browser` | Native browser automation CLI for agents | CLI can open pages, evaluate JS, capture screenshots, and inspect accessibility snapshots | Rust binary via npm/Homebrew/Cargo; Node 24+ for source | Likely possible through Chrome/automation flags, but not first-class in README | Strong screenshot command support | Low; provides primitives, not dashboard QA findings | Medium; needs extra scripting around CLI | Medium to high due Chrome install and Node 24/Rust if source build | Unknown for this Oracle ARM VM without installing browser binary | Updated 2026-06-24; active/recent metadata | Apache-2.0 | Medium | Keep as future CLI alternative, not current base. Score: 70 |
| `jo-inc/camofox-browser` | Anti-detection browser server and OpenClaw plugin | Playwright-compatible browser API with screenshots and snapshots | Node >=22, Camoufox/Firefox binary, REST server | Possible, but focused on anti-bot browsing rather than dashboard QA | Supported | Low to medium; traces/screenshots but not visual QA scoring | Medium; server runtime plus browser binary | High; downloads Camoufox and may need API key for some features | Unclear; ARM support appears via Docker/build paths but not worth risk | Updated 2026-06-24; active/recent metadata | MIT | High | Do not integrate; anti-detection and OpenClaw focus is unnecessary risk. Score: 45 |
| `lightpanda-io/browser` | Lightweight headless browser built in Zig | CDP server, fast screenshots/DOM dumps, ARM64 binaries | Zig browser binary; CDP/Puppeteer compatible | Possible through CDP clients | Possible through CDP clients | Low; browser engine, not QA framework | Medium; fast but beta browser coverage | Medium; binary install required | Linux aarch64 build advertised | Updated 2026-06-24; beta | AGPL-3.0 | High | Do not integrate now; beta coverage and AGPL make it poor fit. Score: 50 |

## Scoring Notes

- Mobile viewport support: +20 when explicit or easy with browser primitives.
- Screenshot/browser automation: +20 when explicit.
- Simple integration: +15 when it can fit the existing Python/Node stack without a service.
- Active maintenance: +15 for recent metadata updates.
- Visual QA/screenshot diffing: +10 when the project has native support.
- Low dependency risk: +10 when no large binary, cloud, or service is required.
- Works on current Python/Node: +10 when compatible with already-used runtime.
- Heavy cloud/SaaS, unclear license, required secrets, or autonomous browser runtime risks subtract per objective.

## Implementation Choice

The local Playwright QA agent is safer and more directly aligned:

- It already opens the protected dashboard URL without exposing tokens.
- It runs fixed Android viewports: `412x915`, `360x800`, and `412x1000`.
- It captures deterministic screenshots and writes `dashboard_qa/report.md`.
- It checks overflow, clipped cards, table wrappers, race numbers, our racer above the fold, console errors, and failed requests.
- It can report via the tester bot without using the main Email Game bot token.

The current upgrade path is to keep this script as the canonical QA loop and add focused checks/reporting instead of adopting a broader autonomous browser framework.
