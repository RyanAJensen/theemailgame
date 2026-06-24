# Email Game Dashboard Frontend QA

`scripts/dashboard_frontend_qa.py` runs a mobile frontend QA pass against the protected Email Game Race Control dashboard.

## How To Run

Run one QA pass:

```bash
./.venv/bin/python scripts/dashboard_frontend_qa.py
```

Or use the helper:

```bash
bash scripts/start_dashboard_qa.sh
```

Watch mode reruns every 60 seconds and rewrites the report:

```bash
./.venv/bin/python scripts/dashboard_frontend_qa.py --watch
```

Optional Telegram summary:

```bash
./.venv/bin/python scripts/dashboard_frontend_qa.py --send-report
```

`--telegram-report` is kept as a backwards-compatible alias. The Telegram summary uses the tester bot credentials only when `EMAIL_GAME_TEST_TELEGRAM_BOT_TOKEN` and `EMAIL_GAME_TEST_REPORT_CHAT_ID` are already present.

## Browser Setup

The QA agent uses Python Playwright when available. If Playwright or Chromium is missing, the script writes a report explaining the missing dependency instead of installing large browser packages automatically.

Suggested setup when approved:

```bash
./.venv/bin/python -m pip install playwright
./.venv/bin/python -m playwright install chromium
```

## Screenshots

Screenshots are saved under:

```text
dashboard_qa/screenshots/
```

The default Android viewports are:

- `412x915`: Samsung A16-ish viewport
- `360x800`: compact Android viewport
- `412x1000`: tall Android viewport

Expected files:

- `dashboard_qa/screenshots/home-412x915.png`
- `dashboard_qa/screenshots/home-360x800.png`
- `dashboard_qa/screenshots/home-412x1000.png`

## Report

The markdown report is written to:

```text
dashboard_qa/report.md
```

Machine-readable QA state is written to:

```text
dashboard_qa/latest-summary.json
dashboard_qa/last_report_state.json
```

`latest-summary.json` stores the current QA result. `last_report_state.json` is updated only after a tester-bot report is sent, and stores the last git commit, screenshot hashes, summary, and sent timestamp.

Codex should use this report after dashboard UI changes to decide the next frontend polish task. The report scores Android readiness and checks:

- console errors
- failed network requests
- horizontal overflow
- elements wider than the viewport
- clipped text and cards
- table overflow
- tap targets under 44px
- duplicate dashboard-open cards
- hero height
- whether the `YOU` racer is visible above the fold
- whether rank, score, and gap numbers are visible above the fold
- local load time under 3 seconds
- reduced-motion usefulness

## Security Notes

- The script reads `agent_logs/emailgame-dashboard-url.txt` first.
- If the public URL is unavailable, it falls back to `http://127.0.0.1:8787/d/<token>/` using `agent_logs/emailgame-dashboard-token.txt`.
- Tokens and protected URLs are redacted from reports and console output.
- The dashboard remains read-only.
- The QA agent does not restart the Email Game agent, monitor, or dashboard.
- The tester-bot report sends screenshots and a dashboard button, but captions avoid printing the protected dashboard token.
- Do not commit screenshots unless Papzin explicitly asks for them.
