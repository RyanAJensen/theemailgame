#!/usr/bin/env python3
"""Read-only local dashboard for The Email Game race control."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv
import uvicorn

try:
    from emailgame_budget import EmailGameBudget
except ModuleNotFoundError:  # pragma: no cover - module-style imports in tests
    from scripts.emailgame_budget import EmailGameBudget

try:
    from emailgame_coach import EmailGameCoach
except ModuleNotFoundError:  # pragma: no cover - module-style imports in tests
    from scripts.emailgame_coach import EmailGameCoach


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_LOGS = PROJECT_ROOT / "agent_logs"
TOKEN_FILE = AGENT_LOGS / "emailgame-dashboard-token.txt"
URL_FILE = AGENT_LOGS / "emailgame-dashboard-url.txt"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

load_dotenv(PROJECT_ROOT / ".env.local")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_token_file() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = _read_text(TOKEN_FILE)
    if not token:
        token = secrets.token_urlsafe(24).rstrip("=")
        TOKEN_FILE.write_text(f"{token}\n", encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def _load_public_url() -> str:
    return _read_text(URL_FILE)


def _tmux_session_running(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except Exception:
        return None


def _sorted_rounds(match: Any) -> List[Dict[str, Any]]:
    rounds = getattr(match, "rounds", {}) or {}
    items: List[Dict[str, Any]] = []
    for round_id, metrics in rounds.items():
        requests_sent = len(getattr(metrics, "requests_sent", []) or [])
        request_targets = getattr(metrics, "request_targets", None)
        sent_count = max(requests_sent, request_targets or 0)
        items.append(
            {
                "round_id": str(round_id),
                "requests_sent": sent_count,
                "signed_replies_received": len(getattr(metrics, "signed_replies_received", []) or []),
                "signatures_submitted": len(getattr(metrics, "signatures_submitted", []) or []),
                "action_reminders": int(getattr(metrics, "action_reminders", 0) or 0),
                "parser_fallbacks": int(getattr(metrics, "parser_fallbacks", 0) or 0),
                "stale_skips": int(getattr(metrics, "stale_skips", 0) or 0),
                "unauthorized_skips": int(getattr(metrics, "unauthorized_skips", 0) or 0),
                "missing_signer_skips": int(getattr(metrics, "missing_signer_skips", 0) or 0),
            }
        )
    return sorted(items, key=lambda item: (0, int(item["round_id"])) if item["round_id"].isdigit() else (1, item["round_id"]))


def _sparkline_svg(points: List[int], width: int = 420, height: int = 120) -> str:
    if len(points) < 2:
        return (
            f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Leaderboard chart'>"
            "<rect width='100%' height='100%' rx='16' fill='rgba(255,255,255,0.04)'/>"
            "<text x='24' y='66' fill='rgba(255,255,255,0.6)' font-size='18'>Not enough history yet</text>"
            "</svg>"
        )
    min_value = min(points)
    max_value = max(points)
    span = max(max_value - min_value, 1)
    margin = 12
    usable_width = width - margin * 2
    usable_height = height - margin * 2
    step = usable_width / (len(points) - 1)
    coords: List[str] = []
    for index, value in enumerate(points):
        x = margin + index * step
        y = margin + (max_value - value) * usable_height / span
        coords.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(coords)
    latest = points[-1]
    first = points[0]
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Leaderboard score trend'>"
        f"<defs><linearGradient id='trend-fill' x1='0' x2='0' y1='0' y2='1'>"
        "<stop offset='0%' stop-color='rgba(90,198,255,0.45)'/>"
        "<stop offset='100%' stop-color='rgba(90,198,255,0.05)'/>"
        "</linearGradient></defs>"
        f"<rect width='100%' height='100%' rx='16' fill='rgba(255,255,255,0.04)'/>"
        f"<polyline fill='none' stroke='#5ac6ff' stroke-width='3' points='{polyline}' stroke-linecap='round' stroke-linejoin='round'/>"
        f"<text x='20' y='28' fill='rgba(255,255,255,0.75)' font-size='15'>score trend</text>"
        f"<text x='20' y='{height - 16}' fill='rgba(255,255,255,0.55)' font-size='12'>start {first}</text>"
        f"<text x='{width - 104}' y='{height - 16}' fill='rgba(255,255,255,0.55)' font-size='12'>latest {latest}</text>"
        "</svg>"
    )


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _build_race_state(summary: Dict[str, Any]) -> Dict[str, Any]:
    leaderboard = summary.get("leaderboard") if isinstance(summary.get("leaderboard"), dict) else {}
    status = summary.get("status") if isinstance(summary.get("status"), dict) else {}
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    top5 = leaderboard.get("top5") if isinstance(leaderboard.get("top5"), list) else []
    leader_score = None
    if top5 and isinstance(top5[0], dict):
        leader_score = _safe_int(top5[0].get("elo"))

    racers: List[Dict[str, Any]] = []
    for item in top5[:5]:
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id") or "")
        score = _safe_int(item.get("elo"))
        racers.append(
            {
                "agent_id": agent_id,
                "score": score,
                "rank": _safe_int(item.get("rank")),
                "is_user": agent_id == "letlhogonolo_fanampe",
                "gap_to_leader": (leader_score - score) if leader_score is not None and score is not None else None,
            }
        )

    user_racer = next((item for item in racers if item["is_user"]), racers[0] if racers else None)
    if user_racer is None:
        user_racer = {"agent_id": "letlhogonolo_fanampe", "score": None, "rank": None, "is_user": True, "gap_to_leader": None}

    score_delta = _first_non_none(
        metrics.get("score_delta_15m"),
        metrics.get("score_delta_30m"),
        metrics.get("score_delta_60m"),
    )

    return {
        "phase": str(status.get("phase") or "waiting"),
        "log_stale": bool(status.get("log_stale")),
        "rank": leaderboard.get("rank"),
        "score": leaderboard.get("score"),
        "gap_to_one": leaderboard.get("gap_to_one"),
        "gap_to_four": leaderboard.get("gap_to_four"),
        "score_delta": score_delta,
        "rank_change": summary.get("coach", {}).get("rank_delta") if isinstance(summary.get("coach"), dict) else None,
        "top_competitors": racers,
        "user": user_racer,
        "leader_score": leader_score,
        "callout": _callout_text(score_delta, summary),
        "public_url": _load_public_url(),
    }


def _callout_text(score_delta: Any, summary: Dict[str, Any]) -> str:
    leaderboard = summary.get("leaderboard") if isinstance(summary.get("leaderboard"), dict) else {}
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    if isinstance(summary.get("coach"), dict):
        rank_change = summary["coach"].get("rank_delta")
    else:
        rank_change = None
    rank = leaderboard.get("rank")
    if isinstance(rank_change, int) and rank_change < 0:
        return f"rank up • now #{rank}"
    if isinstance(score_delta, int) and score_delta > 0:
        return f"+{score_delta} surge"
    if isinstance(score_delta, int) and score_delta < 0:
        return f"{score_delta} slip"
    if str(summary.get("status", {}).get("phase") if isinstance(summary.get("status"), dict) else "").lower() in {"waiting", "between matches"}:
        return "idle hover"
    recent_reminders = metrics.get("recent_reminders")
    if isinstance(recent_reminders, int) and recent_reminders > 0:
        return "hold position"
    return "race live"


def _race_hero_html(race: Dict[str, Any]) -> str:
    top_competitors = race.get("top_competitors") if isinstance(race.get("top_competitors"), list) else []
    rows = []
    for item in top_competitors:
        if not isinstance(item, dict):
            continue
        classes = ["racers-list__item"]
        if item.get("is_user"):
            classes.append("is-user")
        score = item.get("score")
        gap = item.get("gap_to_leader")
        rows.append(
            "<li class='" + " ".join(classes) + "' "
            f"data-agent='{html_escape(str(item.get('agent_id') or ''), quote=True)}' "
            f"data-rank='{html_escape(str(item.get('rank') or ''), quote=True)}' "
            f"data-gap='{html_escape(str(gap if gap is not None else ''), quote=True)}' "
            f"data-score='{html_escape(str(score if score is not None else ''), quote=True)}'>"
            f"<span class='racers-list__rank'>#{html_escape(str(item.get('rank') or ''), quote=False)}</span>"
            f"<span class='racers-list__name'>{html_escape(str(item.get('agent_id') or ''), quote=False)}</span>"
            f"<span class='racers-list__score'>{html_escape(str(score if score is not None else 'n/a'), quote=False)}</span>"
            "</li>"
        )

    user_rank = race.get("rank")
    user_score = race.get("score")
    gap_to_one = race.get("gap_to_one")
    callout = race.get("callout") or "race live"
    phase = race.get("phase") or "waiting"
    return f"""
    <section class="race-hero card">
      <div class="race-hero__canvas-wrap">
        <div class="race-hero__canvas" id="race-canvas" aria-hidden="true"></div>
        <div class="race-hero__overlay">
          <div class="race-hero__title">
            <span class="race-hero__kicker">Email Race Control</span>
            <h1>3D Email Pod Race</h1>
            <p>Live leaderboard-driven pods loop the track. The highlighted racer is you.</p>
          </div>
          <div class="race-badges">
            <span class="chip">Rank #{html_escape(str(user_rank or 'n/a'), quote=False)}</span>
            <span class="chip">Score {html_escape(str(user_score if user_score is not None else 'n/a'), quote=False)}</span>
            <span class="chip">Gap to #1 {html_escape(str(gap_to_one if gap_to_one is not None else 'n/a'), quote=False)}</span>
            <span class="chip race-callout" id="race-callout">{html_escape(str(callout), quote=False)}</span>
          </div>
          <div class="race-user-pill">
            <span class="race-user-pill__label">YOU</span>
            <span class="race-user-pill__name">letlhogonolo_fanampe</span>
            <span class="race-user-pill__phase">{html_escape(str(phase), quote=False)}</span>
          </div>
        </div>
      </div>
      <div class="race-hero__legend">
        <div class="race-legend__header">
          <div>
            <div class="label">Visible racers</div>
            <h2>Top of the board</h2>
          </div>
          <div class="race-legend__state">{html_escape(str(phase), quote=False)}</div>
        </div>
        <ul class="racers-list" id="racers-list">
          {''.join(rows) if rows else '<li class="racers-list__item"><span class="racers-list__rank">#1</span><span class="racers-list__name">Waiting for leaderboard data</span><span class="racers-list__score">n/a</span></li>'}
        </ul>
        <div class="race-legend__note">
          {('Idle hover active' if str(phase).lower() in {'waiting', 'between matches'} else 'Camera drift and surge motion stay active')}
        </div>
      </div>
    </section>
    """


def _dashboard_script(race: Dict[str, Any]) -> str:
    dashboard_state_json = json.dumps(race, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return f"""
    <script id="dashboard-state" type="application/json">{dashboard_state_json}</script>
    <script>
    (async function() {{
      const stateElement = document.getElementById('dashboard-state');
      let state = stateElement ? JSON.parse(stateElement.textContent || '{{}}') : {{}};
      const canvasHost = document.getElementById('race-canvas');
      const calloutEl = document.getElementById('race-callout');
      const racersList = document.getElementById('racers-list');
      const tokenFromPath = (() => {{
        const parts = window.location.pathname.split('/').filter(Boolean);
        if (parts[0] === 'd' && parts[1]) {{
          return parts[1];
        }}
        const queryToken = new URLSearchParams(window.location.search).get('token');
        return queryToken || '';
      }})();
      const apiBase = tokenFromPath ? `/d/${{encodeURIComponent(tokenFromPath)}}` : '';
      const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const slowDevice = (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4) || (navigator.connection && navigator.connection.saveData);
      const fallbackMode = prefersReducedMotion || slowDevice || !window.WebGLRenderingContext;
      let scene;
      let camera;
      let renderer;
      let frameId = 0;
      let racerMeshes = [];
      let anchor = 0;
      let boostUntil = 0;
      let focusAt = performance.now();
      let lastUpdateAt = 0;
      let previousRank = state.rank;
      let previousScore = state.score;
      const racerCount = 5;

      const setCallout = (text, pulse) => {{
        if (!calloutEl) return;
        calloutEl.textContent = text;
        calloutEl.classList.toggle('is-pulse', Boolean(pulse));
      }};

      const renderFallback = () => {{
        if (!canvasHost) return;
        canvasHost.innerHTML = `
          <div class="race-fallback">
            <div class="race-fallback__ring"></div>
            <div class="race-fallback__pods">
              <div class="pod pod--user">YOU</div>
              <div class="pod">#2</div>
              <div class="pod">#3</div>
              <div class="pod">#4</div>
              <div class="pod">#5</div>
            </div>
          </div>`;
      }};

      const syncList = (next) => {{
        if (!racersList) return;
        const items = Array.from(racersList.querySelectorAll('.racers-list__item'));
        const lookup = new Map((next.top_competitors || []).map((item) => [String(item.agent_id || ''), item]));
        items.forEach((row) => {{
          const agent = row.getAttribute('data-agent') || '';
          const data = lookup.get(agent);
          if (!data) return;
          row.setAttribute('data-rank', String(data.rank ?? ''));
          row.setAttribute('data-gap', String(data.gap_to_leader ?? ''));
          row.querySelector('.racers-list__rank').textContent = `#${{data.rank ?? 'n/a'}}`;
          row.querySelector('.racers-list__score').textContent = String(data.score ?? 'n/a');
        }});
      }};

      const syncOverlay = (next) => {{
        const callout = next.callout || 'race live';
        setCallout(callout, typeof next.score_delta === 'number' && next.score_delta > 0);
        syncList(next);
        const userName = document.querySelector('.race-user-pill__name');
        const userPhase = document.querySelector('.race-user-pill__phase');
        if (userName) userName.textContent = 'letlhogonolo_fanampe';
        if (userPhase) userPhase.textContent = next.phase || 'waiting';
      }};

      const updateState = (next) => {{
        state = next || state;
        syncOverlay(state);
        syncTargets();
        const now = performance.now();
        if (typeof state.rank === 'number' && typeof previousRank === 'number' && state.rank < previousRank) {{
          boostUntil = now + 1400;
        }}
        if (typeof state.score === 'number') {{
          previousScore = state.score;
        }}
        if (typeof state.rank === 'number') {{
          previousRank = state.rank;
        }}
      }};

      const maybeFetch = async () => {{
        try {{
          const response = await fetch(`${{apiBase}}/api/dashboard`, {{ cache: 'no-store', headers: {{ accept: 'application/json' }} }});
          if (!response.ok) return;
          const next = await response.json();
          if (next && next.race) {{
            updateState(next.race);
          }}
        }} catch (error) {{}}
      }};

      if (fallbackMode) {{
        renderFallback();
        syncOverlay(state);
        setInterval(maybeFetch, 25000);
        return;
      }}

      try {{
        const THREE = await import('https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js');
        const width = canvasHost ? canvasHost.clientWidth : window.innerWidth;
        const height = canvasHost ? canvasHost.clientHeight : Math.max(320, window.innerHeight * 0.45);
        scene = new THREE.Scene();
        scene.fog = new THREE.Fog(0x06101d, 12, 34);
        camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 100);
        camera.position.set(0, 8.5, 18);
        renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true, powerPreference: 'high-performance' }});
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
        renderer.setSize(width, height);
        renderer.domElement.style.width = '100%';
        renderer.domElement.style.height = '100%';
        renderer.domElement.style.display = 'block';
        if (canvasHost) {{
          canvasHost.innerHTML = '';
          canvasHost.appendChild(renderer.domElement);
        }}

        const ambient = new THREE.AmbientLight(0x8bb8ff, 1.2);
        const key = new THREE.DirectionalLight(0xffffff, 1.5);
        key.position.set(-6, 10, 8);
        const fill = new THREE.DirectionalLight(0x56ffd4, 0.8);
        fill.position.set(5, 2, -6);
        scene.add(ambient, key, fill);

        const track = new THREE.Mesh(
          new THREE.TorusGeometry(8.2, 0.55, 20, 180),
          new THREE.MeshStandardMaterial({{
            color: 0x0d2238,
            emissive: 0x16314f,
            emissiveIntensity: 0.55,
            metalness: 0.75,
            roughness: 0.2,
          }})
        );
        track.rotation.x = Math.PI / 2;
        scene.add(track);

        const grid = new THREE.GridHelper(26, 26, 0x22405d, 0x1a2636);
        grid.position.y = -2.5;
        scene.add(grid);

        const particles = new THREE.Points(
          new THREE.BufferGeometry(),
          new THREE.PointsMaterial({{ color: 0x5ac6ff, size: 0.08, transparent: true, opacity: 0.72 }})
        );
        const particlePositions = [];
        for (let i = 0; i < 120; i++) {{
          particlePositions.push((Math.random() - 0.5) * 36, Math.random() * 16 - 2, (Math.random() - 0.5) * 36);
        }}
        particles.geometry.setAttribute('position', new THREE.Float32BufferAttribute(particlePositions, 3));
        scene.add(particles);

        const colors = [0x7dffb2, 0x66d9ff, 0xffd166, 0xb58cff, 0xff7d9c];
        const makeRacer = (item, index) => {{
          const group = new THREE.Group();
          const body = new THREE.Mesh(
            new THREE.CapsuleGeometry(0.56, 0.92, 4, 10),
            new THREE.MeshStandardMaterial({{
              color: item.is_user ? 0x143450 : 0x102238,
              emissive: item.is_user ? 0x66d9ff : colors[index % colors.length],
              emissiveIntensity: item.is_user ? 1.7 : 0.8,
              metalness: 0.86,
              roughness: 0.15,
            }})
          );
          const fin = new THREE.Mesh(
            new THREE.RingGeometry(0.22, 0.48, 18),
            new THREE.MeshStandardMaterial({{
              color: item.is_user ? 0xe8fbff : 0x9eb7d8,
              emissive: item.is_user ? 0x5ac6ff : 0x33506f,
              side: THREE.DoubleSide,
              transparent: true,
              opacity: 0.95,
            }})
          );
          fin.rotation.x = Math.PI / 2;
          fin.position.y = 0.95;
          const glow = new THREE.PointLight(item.is_user ? 0x66d9ff : colors[index % colors.length], item.is_user ? 2.2 : 1.2, 7);
          glow.position.set(0, 0.4, 0);
          const marker = new THREE.Mesh(
            new THREE.BoxGeometry(0.34, 0.12, 0.22),
            new THREE.MeshStandardMaterial({{
              color: item.is_user ? 0xffffff : 0x182533,
              emissive: item.is_user ? 0x66d9ff : 0x000000,
              emissiveIntensity: item.is_user ? 1.5 : 0.0,
            }})
          );
          marker.position.set(0, 1.36, 0);
          if (item.is_user) {{
            const ring = new THREE.Mesh(
              new THREE.TorusGeometry(1.0, 0.08, 8, 24),
              new THREE.MeshBasicMaterial({{ color: 0x9be6ff, transparent: true, opacity: 0.8 }})
            );
            ring.rotation.x = Math.PI / 2;
            ring.position.y = -0.92;
            group.add(ring);
          }}
          group.add(body, fin, glow, marker);
          group.userData = {{
            item,
            index,
            angle: (index / Math.max(racerCount, 1)) * Math.PI * 2,
            targetAngle: (index / Math.max(racerCount, 1)) * Math.PI * 2,
            radius: 8.3 + Math.max(0, Math.min(4.5, (item.gap_to_leader || 0) * 0.02)),
            targetRadius: 8.3 + Math.max(0, Math.min(4.5, (item.gap_to_leader || 0) * 0.02)),
            surge: 0,
            pulse: item.is_user ? 1.0 : 0.0,
          }};
          scene.add(group);
          return group;
        }};

        racerMeshes = (state.top_competitors || []).slice(0, racerCount).map(makeRacer);

        const resize = () => {{
          const w = canvasHost ? canvasHost.clientWidth : window.innerWidth;
          const h = canvasHost ? canvasHost.clientHeight : Math.max(320, window.innerHeight * 0.45);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
          renderer.setSize(w, h, false);
        }};
        if (window.ResizeObserver && canvasHost) {{
          new ResizeObserver(resize).observe(canvasHost);
        }}
        window.addEventListener('resize', resize, {{ passive: true }});

        const syncTargets = () => {{
          const racers = state.top_competitors || [];
          racerMeshes.forEach((mesh, index) => {{
            const item = racers[index] || mesh.userData.item;
            mesh.userData.item = item;
            mesh.userData.index = index;
            const baseAngle = (index / Math.max(racerCount, 1)) * Math.PI * 2;
            mesh.userData.targetAngle = baseAngle + (index * 0.12);
            mesh.userData.targetRadius = 8.3 + Math.max(0, Math.min(4.5, (item.gap_to_leader || 0) * 0.02));
            if (item.is_user) {{
              mesh.userData.surge = Math.max(mesh.userData.surge, 0.35);
            }}
          }});
        }};

        syncTargets();
        syncOverlay(state);

        const clock = new THREE.Clock();
        const animate = () => {{
          frameId = requestAnimationFrame(animate);
          const elapsed = clock.getElapsedTime();
          const delta = clock.getDelta();
          const isIdle = String(state.phase || '').toLowerCase() === 'between matches' || String(state.phase || '').toLowerCase() === 'waiting';
          anchor += delta * (isIdle ? 0.08 : 0.18);
          const drift = Math.sin(elapsed * 0.45) * 0.04;
          const bob = Math.sin(elapsed * 1.2) * (isIdle ? 0.16 : 0.08);

          racerMeshes.forEach((mesh, index) => {{
            mesh.userData.angle += delta * (isIdle ? 0.08 : 0.18) + drift * 0.01;
            mesh.userData.radius += (mesh.userData.targetRadius - mesh.userData.radius) * Math.min(1, delta * 2.2);
            const surgeBoost = mesh.userData.item && mesh.userData.item.is_user ? mesh.userData.surge : 0;
            const angle = mesh.userData.angle + index * 0.08 + surgeBoost * 0.2;
            mesh.position.set(
              Math.cos(angle) * mesh.userData.radius,
              Math.sin(elapsed * (mesh.userData.item && mesh.userData.item.is_user ? 2.0 : 1.2) + index) * (isIdle ? 0.42 : 0.18) + bob,
              Math.sin(angle) * mesh.userData.radius
            );
            mesh.rotation.y = -angle + Math.PI / 2;
            mesh.rotation.x = Math.sin(elapsed + index) * 0.04;
            mesh.scale.setScalar(1 + (mesh.userData.item && mesh.userData.item.is_user ? 0.08 : 0.0) + Math.max(0, mesh.userData.surge) * 0.18);
            mesh.userData.surge = Math.max(0, mesh.userData.surge - delta * 0.42);
            if (mesh.userData.item && mesh.userData.item.is_user) {{
              mesh.children.forEach((child) => {{
                if (child.material && child.material.opacity !== undefined) {{
                  child.material.opacity = 0.8 + Math.sin(elapsed * 4) * 0.08;
                }}
              }});
            }}
          }});

          camera.position.x = Math.cos(elapsed * 0.16 + 0.7) * 18;
          camera.position.z = Math.sin(elapsed * 0.16 + 0.7) * 18;
          camera.position.y = 8.4 + Math.sin(elapsed * 0.22) * 0.7;
          if (performance.now() - focusAt > 9000) {{
            focusAt = performance.now();
          }}
          const focusMesh = racerMeshes.find((mesh) => mesh.userData.item && mesh.userData.item.is_user) || racerMeshes[0];
          if (focusMesh) {{
            const desired = focusMesh.position.clone();
            camera.lookAt(desired);
          }}

          if (performance.now() < boostUntil) {{
            const userMesh = racerMeshes.find((mesh) => mesh.userData.item && mesh.userData.item.is_user);
            if (userMesh) {{
              userMesh.userData.surge = Math.max(userMesh.userData.surge, 1.0);
            }}
          }}

          if (performance.now() - lastUpdateAt > 18000) {{
            lastUpdateAt = performance.now();
            maybeFetch();
          }}

          renderer.render(scene, camera);
        }};
        animate();
      }} catch (error) {{
        renderFallback();
      }}

      const initialScore = state.score;
      const initialRank = state.rank;
      setInterval(async () => {{
        try {{
          const response = await fetch(`${{apiBase}}/api/dashboard`, {{ cache: 'no-store', headers: {{ accept: 'application/json' }} }});
          if (!response.ok) return;
          const next = await response.json();
          if (!next || !next.race) return;
          const nextState = next.race;
          const scoreChanged = typeof nextState.score === 'number' && nextState.score !== previousScore;
          const rankChanged = typeof nextState.rank === 'number' && nextState.rank !== previousRank;
          updateState(nextState);
          if (scoreChanged || rankChanged) {{
            boostUntil = performance.now() + 1500;
          }}
        }} catch (error) {{}}
      }}, 20000);
    }})();
    </script>
    """


def _dashboard_token_matches(candidate: str) -> bool:
    secret = _ensure_token_file()
    return bool(candidate and candidate == secret)


def _access_denied() -> PlainTextResponse:
    return PlainTextResponse("Dashboard link invalid or expired.", status_code=403)


def _request_token(request: Request, path_token: Optional[str] = None) -> str:
    return path_token or request.query_params.get("token", "")


def _summarize() -> Dict[str, Any]:
    coach = EmailGameCoach()
    coach_analysis = coach.analyze(persist=False)
    budget = EmailGameBudget()
    budget_analysis = budget.analyze(persist=False)

    leaderboard_state = _read_json(AGENT_LOGS / "emailgame-leaderboard-state.json")
    leaderboard_snapshot = leaderboard_state.get("last_snapshot")
    leaderboard_snapshot = leaderboard_snapshot if isinstance(leaderboard_snapshot, dict) else {}

    monitor_state = _read_json(AGENT_LOGS / "emailgame-monitor-state.json")
    phase = str(monitor_state.get("phase") or "waiting")
    last_event = str(monitor_state.get("last_event") or "")
    connected_at = str(monitor_state.get("connected_sent_at") or "")
    observed_lines = monitor_state.get("observed_lines") if isinstance(monitor_state.get("observed_lines"), list) else []
    latest_observed = observed_lines[-1] if observed_lines else {}
    latest_observed_text = str(latest_observed.get("text") or "") if isinstance(latest_observed, dict) else ""

    match = coach_analysis.matches[-1] if coach_analysis.matches else None
    latest_match = None
    if match is not None:
        latest_match = {
            "index": match.index,
            "started_at": match.started_at.isoformat() if match.started_at else None,
            "ended": bool(match.ended),
            "rounds": _sorted_rounds(match),
            "total_requests_sent": match.total_requests_sent(),
            "total_signed_replies": match.total_signed_replies(),
            "total_submissions": match.total_submissions(),
            "total_reminders": match.total_reminders(),
        }

    leaderboard_history = [
        {
            "fetched_at": str(item.get("fetched_at") or ""),
            "rank": _safe_int(item.get("rank")),
            "score": _safe_int(item.get("score")),
        }
        for item in coach_analysis.state.get("leaderboard_history", [])
        if isinstance(item, dict)
    ]
    score_points = [item["score"] for item in leaderboard_history if item["score"] is not None]

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": {
            "agent_running": _tmux_session_running("emailgame"),
            "monitor_running": _tmux_session_running("emailgame-monitor"),
            "dashboard_running": _tmux_session_running("emailgame-dashboard"),
            "phase": phase,
            "last_event": last_event,
            "connected_at": connected_at,
            "latest_observed": latest_observed_text,
            "log_stale": bool(coach_analysis.latest_log_stale),
        },
        "leaderboard": {
            "rank": coach_analysis.rank,
            "score": coach_analysis.score,
            "gap_to_four": coach_analysis.gap_to_four,
            "gap_to_one": coach_analysis.gap_to_one,
            "fetched_at": str(leaderboard_snapshot.get("fetched_at") or ""),
            "top5": leaderboard_snapshot.get("top5") if isinstance(leaderboard_snapshot.get("top5"), list) else [],
            "history": leaderboard_history[-12:],
            "chart": {
                "points": score_points[-12:],
            },
        },
        "coach": {
            "recommendation_title": coach_analysis.recommendation_title,
            "recommendation_reason": coach_analysis.recommendation_reason,
            "recommendation_goal": coach_analysis.recommendation_goal,
            "recommendation_evidence": coach_analysis.recommendation_evidence,
            "weaknesses": coach_analysis.weaknesses,
            "log_stale": bool(coach_analysis.latest_log_stale),
            "rank_delta": coach_analysis.rank_delta,
        },
        "metrics": {
            "score_delta_15m": coach_analysis.deltas.get(15),
            "score_delta_30m": coach_analysis.deltas.get(30),
            "score_delta_60m": coach_analysis.deltas.get(60),
            "recent_reminders": sum(match.total_reminders() for match in coach_analysis.matches[-3:]),
            "recent_submissions": sum(match.total_submissions() for match in coach_analysis.matches[-3:]),
            "recent_signed_replies": sum(match.total_signed_replies() for match in coach_analysis.matches[-3:]),
            "matches_parsed": len(coach_analysis.matches),
            "budget_usd": budget_analysis.budget_usd,
            "calls_15m": budget_analysis.calls_15m,
            "calls_30m": budget_analysis.calls_30m,
            "calls_60m": budget_analysis.calls_60m,
            "total_calls": budget_analysis.total_calls,
            "token_tracking_available": budget_analysis.token_tracking_available,
        },
        "latest_match": latest_match,
    }
    summary["race"] = _build_race_state(summary)
    return summary


def _html_page(summary: Dict[str, Any], public_url: str) -> str:
    status = summary["status"]
    leaderboard = summary["leaderboard"]
    coach = summary["coach"]
    metrics = summary["metrics"]
    race = summary["race"]
    latest_match = summary["latest_match"]
    top5 = leaderboard.get("top5") or []
    chart_points = leaderboard.get("chart", {}).get("points") or []
    chart_html = _sparkline_svg([int(value) for value in chart_points if isinstance(value, int) or isinstance(value, float)])
    last_status = status.get("phase", "waiting")
    race_hero_html = _race_hero_html(race)
    race_script_html = _dashboard_script(race)
    button_html = (
        f"<a class='button' href='{html_escape(public_url, quote=True)}' target='_blank' rel='noreferrer'>Open Race Control Dashboard</a>"
        if public_url
        else "<span class='button muted'>Dashboard tunnel not active</span>"
    )
    rows = []
    for item in top5:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{html_escape(str(item.get('rank') or ''), quote=False)}</td>"
            f"<td>{html_escape(str(item.get('agent_id') or ''), quote=False)}</td>"
            f"<td>{html_escape(str(item.get('elo') or ''), quote=False)}</td>"
            "</tr>"
        )
    if latest_match:
        round_rows = []
        for round_info in latest_match.get("rounds", []):
            round_rows.append(
                "<tr>"
                f"<td>{html_escape(str(round_info.get('round_id') or ''), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('requests_sent') or 0), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('signed_replies_received') or 0), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('signatures_submitted') or 0), quote=False)}</td>"
                f"<td>{html_escape(str(round_info.get('action_reminders') or 0), quote=False)}</td>"
                "</tr>"
            )
        round_table = "".join(round_rows)
    else:
        round_table = ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Email Game Race Control</title>
    <style>
    :root {{
      color-scheme: dark;
      --bg: #09111f;
      --panel: rgba(11, 18, 30, 0.84);
      --panel-2: rgba(255,255,255,0.04);
      --text: #edf4ff;
      --muted: rgba(237, 244, 255, 0.72);
      --line: rgba(125, 165, 220, 0.24);
      --accent: #66d9ff;
      --accent-2: #7dffb2;
      --warning: #ffd166;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(102, 217, 255, 0.18), transparent 28%),
        radial-gradient(circle at 85% 0%, rgba(125, 255, 178, 0.14), transparent 26%),
        linear-gradient(180deg, #08101d 0%, #0b1321 55%, #08101b 100%);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px 12px 32px;
    }}
    .hero {{
      display: grid;
      gap: 16px;
      grid-template-columns: 1.3fr 0.7fr;
      align-items: start;
      margin-bottom: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.2);
      backdrop-filter: blur(12px);
    }}
    .race-hero {{
      overflow: hidden;
      margin-bottom: 16px;
    }}
    .race-hero__canvas-wrap {{
      position: relative;
      min-height: 420px;
      background:
        radial-gradient(circle at 50% 20%, rgba(102, 217, 255, 0.18), transparent 34%),
        radial-gradient(circle at 70% 0%, rgba(125, 255, 178, 0.1), transparent 26%),
        linear-gradient(180deg, rgba(5, 10, 18, 0.2), rgba(5, 10, 18, 0.7));
    }}
    .race-hero__canvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      min-height: 420px;
    }}
    .race-hero__overlay {{
      position: relative;
      z-index: 2;
      display: grid;
      gap: 16px;
      align-content: start;
      min-height: 420px;
      padding: 20px;
      background:
        linear-gradient(180deg, rgba(7, 13, 24, 0.08), rgba(7, 13, 24, 0.34) 55%, rgba(7, 13, 24, 0.72));
    }}
    .race-hero__title h1 {{
      margin: 6px 0 0;
      font-size: clamp(30px, 6vw, 64px);
      line-height: 0.92;
      letter-spacing: -0.05em;
      max-width: 10ch;
    }}
    .race-hero__title p {{
      margin: 12px 0 0;
      max-width: 58ch;
      color: rgba(237, 244, 255, 0.78);
      line-height: 1.55;
      font-size: 15px;
    }}
    .race-hero__kicker {{
      color: var(--accent-2);
      text-transform: uppercase;
      letter-spacing: 0.18em;
      font-size: 11px;
      font-weight: 800;
    }}
    .race-badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .race-callout {{
      background: rgba(102, 217, 255, 0.12);
      border-color: rgba(102, 217, 255, 0.26);
      color: #dff7ff;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .race-callout.is-pulse {{
      animation: calloutPulse 1.1s ease-out 1;
    }}
    .race-user-pill {{
      display: inline-flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      width: fit-content;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(8, 17, 31, 0.72);
      border: 1px solid rgba(102, 217, 255, 0.24);
      box-shadow: 0 0 0 1px rgba(125, 255, 178, 0.08), 0 18px 36px rgba(0, 0, 0, 0.2);
    }}
    .race-user-pill__label {{
      padding: 4px 8px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #07121f;
      font-weight: 900;
      letter-spacing: 0.1em;
      font-size: 11px;
    }}
    .race-user-pill__name {{
      font-weight: 800;
      letter-spacing: -0.02em;
    }}
    .race-user-pill__phase {{
      color: rgba(237, 244, 255, 0.68);
      font-size: 12px;
    }}
    .race-hero__legend {{
      position: relative;
      z-index: 2;
      padding: 16px 20px 18px;
      border-top: 1px solid rgba(255,255,255,0.08);
      background: rgba(6, 11, 20, 0.78);
    }}
    .race-legend__header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .race-legend__header h2 {{
      margin: 2px 0 0;
      font-size: clamp(18px, 3vw, 26px);
      letter-spacing: -0.03em;
    }}
    .race-legend__state {{
      color: var(--accent-2);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .racers-list {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }}
    .racers-list__item {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.06);
      background: rgba(255,255,255,0.03);
      font-size: 14px;
    }}
    .racers-list__item.is-user {{
      border-color: rgba(102, 217, 255, 0.26);
      background: rgba(102, 217, 255, 0.08);
      box-shadow: 0 0 0 1px rgba(125, 255, 178, 0.08);
    }}
    .racers-list__rank {{
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}
    .racers-list__name {{
      font-weight: 700;
      letter-spacing: -0.02em;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .racers-list__item.is-user .racers-list__name {{
      color: var(--text);
    }}
    .racers-list__score {{
      color: var(--accent-2);
      font-variant-numeric: tabular-nums;
      font-weight: 800;
    }}
    .race-legend__note {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .race-fallback {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 20px;
      background:
        radial-gradient(circle at 50% 50%, rgba(102, 217, 255, 0.18), transparent 36%),
        linear-gradient(180deg, rgba(9, 17, 31, 0.15), rgba(9, 17, 31, 0.72));
    }}
    .race-fallback__ring {{
      position: absolute;
      width: min(78vw, 320px);
      aspect-ratio: 1;
      border-radius: 50%;
      border: 1px solid rgba(102, 217, 255, 0.28);
      box-shadow: 0 0 0 18px rgba(102, 217, 255, 0.04), 0 0 70px rgba(102, 217, 255, 0.12);
      animation: ringPulse 4s ease-in-out infinite;
    }}
    .race-fallback__pods {{
      position: relative;
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 10px;
      max-width: 360px;
    }}
    .pod {{
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.1);
      font-weight: 800;
      letter-spacing: -0.02em;
    }}
    .pod--user {{
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #06111e;
    }}
    @keyframes ringPulse {{
      0%, 100% {{ transform: scale(0.96); opacity: 0.8; }}
      50% {{ transform: scale(1.02); opacity: 1; }}
    }}
    @keyframes calloutPulse {{
      0% {{ transform: translateY(0) scale(1); filter: brightness(1); }}
      40% {{ transform: translateY(-1px) scale(1.04); filter: brightness(1.2); }}
      100% {{ transform: translateY(0) scale(1); filter: brightness(1); }}
    }}
    .hero-main {{
      padding: 24px;
    }}
    .kicker {{
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.15em;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(28px, 5vw, 52px);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }}
    .sub {{
      margin-top: 14px;
      color: var(--muted);
      line-height: 1.5;
      max-width: 65ch;
    }}
    .button-row {{
      margin-top: 18px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 13px 18px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #07121f;
      text-decoration: none;
      font-weight: 800;
      border: none;
      cursor: pointer;
      box-shadow: 0 12px 24px rgba(102, 217, 255, 0.18);
    }}
    .button.muted {{
      background: rgba(255,255,255,0.08);
      color: var(--text);
      box-shadow: none;
      cursor: default;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      padding: 16px;
    }}
    .stat {{
      padding: 16px;
      background: var(--panel-2);
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,0.06);
      min-height: 110px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .value {{
      font-size: clamp(24px, 3.5vw, 36px);
      font-weight: 800;
      letter-spacing: -0.04em;
    }}
    .small {{
      color: var(--muted);
      margin-top: 6px;
      font-size: 13px;
      line-height: 1.4;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-top: 16px;
    }}
    .panel {{
      padding: 18px;
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 20px;
      letter-spacing: -0.02em;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      padding: 10px 8px;
      text-align: left;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      font-size: 14px;
    }}
    th {{
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 11px;
    }}
    .two-col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }}
    .chip {{
      display: inline-flex;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.07);
      color: var(--text);
      border: 1px solid rgba(255,255,255,0.08);
      font-size: 12px;
      margin: 0 6px 6px 0;
    }}
    .good {{ color: var(--accent-2); }}
    .warn {{ color: var(--warning); }}
    .chart svg {{ width: 100%; height: auto; display: block; }}
    ul {{
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
    }}
    .footer {{
      margin-top: 16px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 920px) {{
      .hero, .grid, .two-col, .stat-grid {{ grid-template-columns: 1fr; }}
      .race-hero__canvas-wrap, .race-hero__overlay {{ min-height: 360px; }}
      .race-hero__overlay {{
        padding: 16px;
      }}
      .race-legend__header {{
        flex-direction: column;
      }}
    }}
    @media (max-width: 640px) {{
      .wrap {{
        padding: 10px 10px 24px;
      }}
      .hero {{
        gap: 12px;
      }}
      .hero-main, .panel {{
        padding: 16px;
      }}
      .button {{
        width: 100%;
        justify-content: center;
      }}
      .button-row {{
        align-items: stretch;
      }}
      .race-hero__canvas-wrap, .race-hero__overlay {{
        min-height: 320px;
      }}
      .race-hero__title p {{
        font-size: 14px;
      }}
      .racers-list__item {{
        grid-template-columns: auto 1fr;
      }}
      .racers-list__score {{
        grid-column: 2;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    {race_hero_html}
    <div class="hero">
      <div class="card hero-main">
        <div class="kicker">Email Game Race Control</div>
        <h1>🏁 Open Race Control Dashboard</h1>
        <div class="sub">
          Read-only view for the live Email Game agent, monitor, leaderboard, and coach signals.
          Protected access is required.
        </div>
        <div class="button-row">
          {button_html}
          <span class="chip">Agent { 'running' if status['agent_running'] else 'stopped' }</span>
          <span class="chip">Monitor { 'running' if status['monitor_running'] else 'stopped' }</span>
          <span class="chip">Phase: {html_escape(last_status, quote=False)}</span>
        </div>
      </div>
      <div class="card panel">
        <h2>Quick Status</h2>
        <div class="small"><span class="good">Rank</span> #{leaderboard.get('rank') or 'n/a'}</div>
        <div class="small"><span class="good">Score</span> {leaderboard.get('score') if leaderboard.get('score') is not None else 'n/a'}</div>
        <div class="small"><span class="warn">Gap to #4</span> {leaderboard.get('gap_to_four') if leaderboard.get('gap_to_four') is not None else 'n/a'}</div>
        <div class="small"><span class="warn">Gap to #1</span> {leaderboard.get('gap_to_one') if leaderboard.get('gap_to_one') is not None else 'n/a'}</div>
        <div class="small" style="margin-top: 12px;">Updated: {html_escape(str(summary.get('generated_at') or ''), quote=False)}</div>
      </div>
    </div>

    <div class="stat-grid card">
      <div class="stat">
        <div class="label">Coach</div>
        <div class="value">{html_escape(str(coach.get('recommendation_title') or 'n/a'), quote=False)}</div>
        <div class="small">{html_escape(str(coach.get('recommendation_reason') or ''), quote=False)}</div>
      </div>
      <div class="stat">
        <div class="label">Recent Match</div>
        <div class="value">{html_escape(str(latest_match.get('total_submissions') if latest_match else 0), quote=False)}</div>
        <div class="small">submissions, with {html_escape(str(latest_match.get('total_reminders') if latest_match else 0), quote=False)} reminders</div>
      </div>
      <div class="stat">
        <div class="label">Recent Signals</div>
        <div class="value">{html_escape(str(metrics.get('recent_signed_replies') or 0), quote=False)}</div>
        <div class="small">signed replies in the last 3 parsed matches</div>
      </div>
      <div class="stat">
        <div class="label">LLM Calls</div>
        <div class="value">{html_escape(str(metrics.get('total_calls') or 0), quote=False)}</div>
        <div class="small">{html_escape(str(metrics.get('calls_30m') or 0), quote=False)} in 30m, {html_escape(str(metrics.get('calls_60m') or 0), quote=False)} in 60m</div>
      </div>
    </div>

    <div class="grid">
      <div class="card panel chart">
        <h2>Leaderboard Chart</h2>
        {chart_html}
      </div>
      <div class="card panel">
        <h2>Leaderboard Top 5</h2>
        <table>
          <thead>
            <tr><th>Rank</th><th>Agent</th><th>Elo</th></tr>
          </thead>
          <tbody>
            {''.join(rows) if rows else '<tr><td colspan="3">No leaderboard snapshot yet.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid">
      <div class="card panel">
        <h2>Latest Match Summary</h2>
        {f"<div class='small'>Match #{latest_match.get('index')}</div>" if latest_match else "<div class='small'>No parsed match yet.</div>"}
        <div class="two-col" style="margin-top: 12px;">
          <div>
            <div class="label">Rounds</div>
            <table>
              <thead>
                <tr><th>Round</th><th>Sent</th><th>Replies</th><th>Submitted</th><th>Reminders</th></tr>
              </thead>
              <tbody>
                {round_table if latest_match else '<tr><td colspan="5">No parsed rounds yet.</td></tr>'}
              </tbody>
            </table>
          </div>
          <div>
            <div class="label">Coach Weaknesses</div>
            <ul>
              {''.join(f"<li>{html_escape(str(item), quote=False)}</li>" for item in (coach.get('weaknesses') or [])[:6]) or '<li>No weakness detected from local evidence.</li>'}
            </ul>
            <div class="label" style="margin-top: 14px;">Status Notes</div>
            <div class="small">{html_escape(str(status.get('last_event') or 'No recent event.'), quote=False)}</div>
            <div class="small">{html_escape(str(status.get('latest_observed') or ''), quote=False)}</div>
          </div>
        </div>
      </div>
      <div class="card panel">
        <h2>Metrics</h2>
        <div class="small">Score delta 15m: {metrics.get('score_delta_15m') if metrics.get('score_delta_15m') is not None else 'n/a'}</div>
        <div class="small">Score delta 30m: {metrics.get('score_delta_30m') if metrics.get('score_delta_30m') is not None else 'n/a'}</div>
        <div class="small">Score delta 60m: {metrics.get('score_delta_60m') if metrics.get('score_delta_60m') is not None else 'n/a'}</div>
        <div class="small">Recent reminders: {metrics.get('recent_reminders')}</div>
        <div class="small">Recent submissions: {metrics.get('recent_submissions')}</div>
        <div class="small">Recent signed replies: {metrics.get('recent_signed_replies')}</div>
        <div class="small">Log stale: {'yes' if status.get('log_stale') else 'no'}</div>
        <div class="small">Budget: ${metrics.get('budget_usd')}</div>
        <div class="small">Token tracking available: {'yes' if metrics.get('token_tracking_available') else 'no'}</div>
      </div>
    </div>

    <div class="footer">
      Protected via /d/&lt;token&gt;/ or ?token=&lt;token&gt;. No secrets or raw log dumps are exposed.
    </div>
  </div>
  {race_script_html}
  <script>
    (function() {{
      const parts = window.location.pathname.split('/').filter(Boolean);
      const token = parts[0] === 'd' && parts[1] ? parts[1] : new URLSearchParams(window.location.search).get('token');
      if (!token) return;
      async function refresh() {{
        try {{
          const response = await fetch(`/d/${{encodeURIComponent(token)}}/api/dashboard`, {{
            cache: 'no-store',
            headers: {{ 'accept': 'application/json' }},
          }});
          if (!response.ok) return;
        }} catch (error) {{
        }}
      }}
      setInterval(refresh, 30000);
    }})();
  </script>
</body>
</html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="Email Game Race Control", docs_url=None, redoc_url=None)
    _ensure_token_file()

    def _check(request: Request, path_token: Optional[str] = None) -> Optional[PlainTextResponse]:
        if not _dashboard_token_matches(_request_token(request, path_token)):
            return _access_denied()
        return None

    def _json_response(request: Request, path_token: Optional[str] = None) -> Any:
        denied = _check(request, path_token)
        if denied is not None:
            return denied
        return JSONResponse(_summarize())

    def _html_response(request: Request, path_token: Optional[str] = None) -> Any:
        denied = _check(request, path_token)
        if denied is not None:
            return denied
        token = _request_token(request, path_token)
        return HTMLResponse(_html_page(_summarize(), _load_public_url()))

    @app.get("/")
    def root(request: Request):
        return _html_response(request)

    @app.get("/api/health")
    def api_health(request: Request):
        return _json_response(request)

    @app.get("/api/dashboard")
    def api_dashboard(request: Request):
        return _json_response(request)

    @app.get("/api/status")
    def api_status(request: Request):
        return JSONResponse({"status": _summarize()["status"]}) if _check(request) is None else _access_denied()

    @app.get("/api/leaderboard")
    def api_leaderboard(request: Request):
        return JSONResponse({"leaderboard": _summarize()["leaderboard"]}) if _check(request) is None else _access_denied()

    @app.get("/api/coach")
    def api_coach(request: Request):
        return JSONResponse({"coach": _summarize()["coach"]}) if _check(request) is None else _access_denied()

    @app.get("/api/metrics")
    def api_metrics(request: Request):
        return JSONResponse({"metrics": _summarize()["metrics"]}) if _check(request) is None else _access_denied()

    @app.get("/api/matches/latest")
    def api_latest_match(request: Request):
        return JSONResponse({"latest_match": _summarize()["latest_match"]}) if _check(request) is None else _access_denied()

    @app.get("/d/{token}/")
    def protected_root(token: str, request: Request):
        return _html_response(request, token)

    @app.get("/d/{token}")
    def protected_root_no_slash(token: str, request: Request):
        return _html_response(request, token)

    @app.get("/d/{token}/api/health")
    def protected_api_health(token: str, request: Request):
        return _json_response(request, token)

    @app.get("/d/{token}/api/dashboard")
    def protected_api_dashboard(token: str, request: Request):
        return _json_response(request, token)

    @app.get("/d/{token}/api/status")
    def protected_api_status(token: str, request: Request):
        return JSONResponse({"status": _summarize()["status"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/leaderboard")
    def protected_api_leaderboard(token: str, request: Request):
        return JSONResponse({"leaderboard": _summarize()["leaderboard"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/coach")
    def protected_api_coach(token: str, request: Request):
        return JSONResponse({"coach": _summarize()["coach"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/metrics")
    def protected_api_metrics(token: str, request: Request):
        return JSONResponse({"metrics": _summarize()["metrics"]}) if _check(request, token) is None else _access_denied()

    @app.get("/d/{token}/api/matches/latest")
    def protected_api_latest_match(token: str, request: Request):
        return JSONResponse({"latest_match": _summarize()["latest_match"]}) if _check(request, token) is None else _access_denied()

    return app


app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Email Game read-only dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
