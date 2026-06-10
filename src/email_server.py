"""
Email Simulation Server - Phase 1
Provides REST API for agent communication with message storage and delivery tracking.
Enhanced with request queuing for handling concurrent moderator messages.
"""

import sys
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

# Ensure emoji/log output never crashes on non-UTF-8 consoles (e.g. Windows
# cp1252 when stdout is redirected). Safe no-op where already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn
import asyncio
import os
import secrets
import jwt  # PyJWT – added in requirements.txt
import json
import subprocess
from src.game.config import NUM_AGENTS, PROJECT_ROOT, MAX_CONCURRENT_GAMES
from src.leaderboard import compute_leaderboard, render_leaderboard_html, INITIAL_RATING
from src.agent_stats import compute_agent_report, render_agent_html

# ---------------------------------------------------------------------------
# External dependencies for upcoming deployment steps
# ---------------------------------------------------------------------------

# Redis dependency removed - using in-memory storage instead

JWT_SECRET = os.getenv("JWT_SECRET", "inbox-arena-secret")

# Verbose per-message delivery logging (WebSocket pushes, queue processing) is
# noise for normal operation; show it only when EMAIL_GAME_DEBUG is set. The
# meaningful events (agent connect/disconnect, registration, errors) always print.
DEBUG_LOGS = os.getenv("EMAIL_GAME_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _dbg(msg: str) -> None:
    if DEBUG_LOGS:
        print(msg)


# Requeue (the live ladder) is a COMPETITION-ONLY capability. It is OFF by
# default: a game's agents are NOT re-queued, so every server runs exactly the
# games it can form and then goes idle. Only the host's competition server turns
# it on by setting EMAIL_GAME_COMPETITION=1. This guarantees that an agent can
# only be requeued by virtue of having joined the actual competition — local
# testing can never enter the ladder.
COMPETITION_MODE = os.getenv("EMAIL_GAME_COMPETITION", "").strip().lower() in ("1", "true", "yes", "on")

# Shared secret for server-INTERNAL endpoints. The game runner and scorer call a
# few endpoints from 127.0.0.1 (read all mail, read a game's submissions, clear
# state); players must never reach them. Generated once and exported into the
# environment so spawned game subprocesses inherit it. The host can pin it via
# the EMAIL_GAME_INTERNAL_KEY env var. These protections only engage in
# competition mode, so local dev and the test suite are unaffected.
INTERNAL_KEY = os.environ.setdefault("EMAIL_GAME_INTERNAL_KEY", secrets.token_hex(16))

# Fail closed: a live competition must NOT run on the public default JWT secret.
# With it, anyone who reads the (public) repo can forge a token for any agent_id
# and impersonate them or read their mail, which would defeat the per-agent access
# control. Refuse to start so this can't happen by omission.
if COMPETITION_MODE and os.getenv("JWT_SECRET", "inbox-arena-secret") == "inbox-arena-secret":
    raise RuntimeError(
        "Refusing to start a competition with the default JWT_SECRET (tokens would "
        "be forgeable). Set a strong one, e.g.:\n"
        "  fly secrets set JWT_SECRET=$(openssl rand -hex 32)\n"
        "For a local competition-mode run: EMAIL_GAME_COMPETITION=1 JWT_SECRET=anything python -m src.email_server"
    )

# Fail closed: a competition must run on a PRIVATE alias pool, never the public
# sample shipped in the repo (or the fuzzy rounds could be solved by looking the
# descriptions up). Refuse to start if MESSAGE_ALIAS_POOL_PATH is unset or points
# at the default public pool.
if COMPETITION_MODE:
    _pool = os.getenv("MESSAGE_ALIAS_POOL_PATH", "").strip()
    _default_pool = (PROJECT_ROOT / "data" / "message_alias_pool.json").resolve()
    if not _pool:
        raise RuntimeError(
            "Refusing to start a competition without a private alias pool. Set "
            "MESSAGE_ALIAS_POOL_PATH to a private pool (not the public sample):\n"
            "  fly secrets set MESSAGE_ALIAS_POOL_PATH=/app/data/message_alias_pool.private.json"
        )
    _pool_path = Path(_pool).resolve()
    if not _pool_path.exists():
        raise RuntimeError(f"MESSAGE_ALIAS_POOL_PATH points to a missing file: {_pool_path}")
    if _pool_path == _default_pool:
        raise RuntimeError(
            "MESSAGE_ALIAS_POOL_PATH must not be the public sample pool "
            "(data/message_alias_pool.json). Use a private pool."
        )

# Local-testing convenience: EMAIL_GAME_RESET_LEADERBOARD=1 starts the board
# fresh by counting only games from this launch onward. It stamps the leaderboard
# cutoff (COMPETITION_START_TIME) in LOCAL time, to match session start_time,
# which is naive local-time ISO. It never overrides an explicit
# COMPETITION_START_TIME, so the real competition (which sets a fixed cutoff once)
# is unaffected and its board still survives server restarts. NOT on by default:
# a restart mid-competition must never wipe the standings.
if os.getenv("EMAIL_GAME_RESET_LEADERBOARD", "").strip().lower() in ("1", "true", "yes", "on") \
        and not os.environ.get("COMPETITION_START_TIME", "").strip():
    from datetime import datetime as _dt
    os.environ["COMPETITION_START_TIME"] = _dt.now().isoformat()
    print(f"🧹 Leaderboard reset: counting games from "
          f"{os.environ['COMPETITION_START_TIME']} onward (existing history kept on disk).")

# How long an agent may be fully disconnected before it leaves its current match
# for good. A reconnect within this window resumes the same match (tolerates a
# transient network blip); after it, the agent is removed from the match and the
# queue and can only re-join for future matches.
DISCONNECT_GRACE_SEC = int(os.getenv("EMAIL_GAME_DISCONNECT_GRACE_SEC", "20"))

# Matchmaking window: when agents become available (join or requeue), wait for
# arrivals to quiet down before forming games, so finishers from concurrent
# games pool in the queue first. The deadline resets on each arrival (debounce),
# so a burst of finishers is gathered together; without this, concurrent games
# that end ~simultaneously each re-form their own 4 the instant they requeue and
# the Elo matcher never sees a real pool (the same groups recur). MAX caps the
# wait so a continuous trickle can't starve formation.
MATCHMAKING_WINDOW_SEC = float(os.getenv("EMAIL_GAME_MATCHMAKING_WINDOW_SEC", "4"))
MATCHMAKING_MAX_SEC = float(os.getenv("EMAIL_GAME_MATCHMAKING_MAX_SEC", "20"))


def _current_ratings() -> Dict[str, float]:
    """Map of agent_id -> current Elo, from the leaderboard (cached). Empty on error."""
    try:
        return {e["agent_id"]: e["elo"] for e in compute_leaderboard()}
    except Exception:
        return {}


def _select_matched_group(queue: List[str], k: int, ratings: Dict[str, float]) -> List[str]:
    """Pick k agents for a game using Elo-based matchmaking.

    Anchors on the longest-waiting agent (queue[0]) so no one is ever starved,
    then fills the rest with the agents whose Elo is closest to the anchor's,
    breaking ties by queue position (FIFO). With k or fewer waiting, returns them
    all (no choice to make).
    """
    if len(queue) <= k:
        return list(queue[:k])
    anchor = queue[0]
    anchor_elo = ratings.get(anchor, INITIAL_RATING)
    rest = sorted(
        queue[1:],
        key=lambda a: (abs(ratings.get(a, INITIAL_RATING) - anchor_elo), queue.index(a)),
    )
    return [anchor] + rest[: k - 1]


# Security validation helpers
def _validate_recipient(to_agent: str) -> bool:
    """Validate that the recipient agent exists and is valid."""
    if not to_agent or not isinstance(to_agent, str):
        return False
    
    # Allow moderator as a special recipient
    if to_agent == "moderator":
        return True
    
    # Basic validation: alphanumeric and underscore only, reasonable length
    if not to_agent.replace("_", "").isalnum() or len(to_agent) > 50:
        return False
    
    # TODO: Could add Redis lookup to verify agent is registered
    # For now, accept any valid-format agent ID
    return True

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_token(request: Request, *, allow_header: bool = True) -> str:
    """FastAPI dependency that returns the *agent_id* from a valid Bearer JWT.

    Raises 401 if no token supplied or invalid, 403 if expired.
    """
    token: str | None = None

    # Prefer Authorization header ("Bearer <token>")
    if allow_header:
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

    # Fallback: token in query param
    if token is None:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        agent_id = payload.get("sub")
        if not agent_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=403, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Stash for downstream handlers
    request.state.agent_id = agent_id
    return agent_id


def _require_internal(request: Request) -> None:
    """Gate a server-internal endpoint. In competition mode only the game runner
    and scorer (which send the internal key) may call it; players are rejected."""
    if COMPETITION_MODE and request.headers.get("x-internal-key") != INTERNAL_KEY:
        raise HTTPException(status_code=403, detail="Endpoint not available")


def _require_own_mailbox(request: Request, agent_id: str) -> None:
    """In competition mode, an agent may only read its own mailbox. Internal
    callers (with the key) are exempt; outside competition mode this is a no-op."""
    if not COMPETITION_MODE:
        return
    if request.headers.get("x-internal-key") == INTERNAL_KEY:
        return
    token_agent = _require_token(request)
    if token_agent != agent_id:
        raise HTTPException(status_code=403, detail="You can only access your own mailbox")


class Message(BaseModel):
    """Message model for email simulation"""
    from_agent: str
    to_agent: str
    subject: str
    body: str
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    status: str = "sent"  # sent, delivered, read


class SendMessageRequest(BaseModel):
    """Request model for sending messages - sender derived from JWT token"""
    to: str
    subject: str
    body: str


class BatchSendRequest(BaseModel):
    """Request model for sending multiple messages at once"""
    messages: List[SendMessageRequest]


class EmailServer:
    """Core email server for message storage and routing with request queuing"""
    
    def __init__(self):
        self.messages: List[Dict] = []
        self.message_status: Dict[str, str] = {}
        # Request queue for handling bursts
        self.message_queue: asyncio.Queue = None  # Will be created when needed
        self.queue_processor_task: Optional[asyncio.Task] = None
        self._queue_started = False

        # In-memory storage (replaces Redis)
        self.registered_agents: Dict[str, Dict[str, str]] = {}
        self.waiting_queue: List[str] = []
        self.current_game_in_progress: bool = False  # legacy flag (kept for status)
        # Concurrent games: game_id -> {"agents": [...], "proc": Popen}
        self.active_games: Dict[str, Dict] = {}
        # agent_id -> game_id it is currently playing in (routes its moderator mail)
        self.agent_to_game: Dict[str, str] = {}
        # game_id -> set of agents who left mid-game; their signatures no longer
        # count toward that game (they can't rejoin or affect it after leaving).
        self.departed_from: Dict[str, set] = {}
        self._game_counter: int = 0
        self._matchmaking_scheduled: bool = False
        self._mm_first_at = None      # when the current matchmaking window opened
        self._mm_deadline: float = 0.0  # loop-time at which to form games
        self._queue_lock = asyncio.Lock()

    
    def _ensure_queue_started(self):
        """Ensure the queue processor is started (lazy initialization)"""
        if not self._queue_started:
            try:
                if self.message_queue is None:
                    self.message_queue = asyncio.Queue()
                self.queue_processor_task = asyncio.create_task(self._process_message_queue())
                self._queue_started = True
            except RuntimeError:
                # No event loop running yet, will try again later
                pass
    
    async def _process_message_queue(self):
        """Background task that processes queued messages one by one"""
        while True:
            try:
                # Get next message from queue (blocks if empty)
                message_data, result_future = await self.message_queue.get()
                _dbg(f"📦 Queue processor: Processing message from {message_data['from_agent']} to {message_data['to']}")
                
                # Process the message
                try:
                    message_id = self._store_message_sync(message_data)
                    result_future.set_result(message_id)
                    _dbg(f"✅ Queue processor: Message {message_id} stored and notified")
                except Exception as e:
                    print(f"❌ Queue processor error storing message: {e}")
                    result_future.set_exception(e)
                
                # Small delay to prevent overwhelming WebSocket delivery
                await asyncio.sleep(0.01)  # 10ms between messages
                
            except Exception as e:
                print(f"Queue processor error: {e}")
                await asyncio.sleep(0.1)
    
    async def store_message_queued(self, message_data: Dict) -> str:
        """Store a message via the queue (non-blocking for concurrent requests)"""
        self._ensure_queue_started()
        if not self._queue_started:
            # Fallback to sync if queue not available
            return self._store_message_sync(message_data)
            
        result_future = asyncio.Future()
        await self.message_queue.put((message_data, result_future))
        return await result_future
    
    def _store_message_sync(self, message_data: Dict) -> str:
        """Synchronous message storage (used by queue processor)"""
        message_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        message = {
            "message_id": message_id,
            "from": message_data["from_agent"],
            "to": message_data["to"],
            "subject": message_data["subject"],
            "body": message_data["body"],
            "timestamp": timestamp,
            "status": "sent"
        }

        # Bucket submissions by the sender's current game so concurrent games'
        # scorers each read only their own. Other mail is left untagged.
        if message["to"] == "moderator":
            message["game_id"] = self.agent_to_game.get(message["from"])

        self.messages.append(message)
        self.message_status[message_id] = "sent"
        
        # After storing, attempt real-time delivery via WebSocket
        try:
            # Get the current event loop and schedule the WebSocket notification
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(manager.send_json(message_data["to"], message))
            else:
                print(f"⚠️  No active event loop for WebSocket notification to {message_data['to']}")
        except Exception as e:
            print(f"⚠️  WebSocket notification failed: {e}")
        
        return message_id
    
    def store_message(self, message_data: Dict) -> str:
        """Store a message and return its ID (legacy sync method)"""
        return self._store_message_sync(message_data)
    
    def get_messages_for_agent(self, agent_id: str) -> List[Dict]:
        """Get all messages for a specific agent"""
        return [msg for msg in self.messages if msg["to"] == agent_id]
    
    def get_all_messages(self) -> List[Dict]:
        """Get all messages (for debugging/visualization)"""
        return self.messages.copy()

    def get_submissions(self, game_id: str) -> List[Dict]:
        """Moderator-addressed messages belonging to a specific game."""
        return [m for m in self.messages
                if m["to"] == "moderator" and m.get("game_id") == game_id]
    
    def clear_all_messages(self) -> None:
        """Clear all messages (useful for starting new rounds)"""
        self.messages.clear()
        self.message_status.clear()
        print("📧 All messages cleared from email server")
    
    def clear_all_state(self) -> None:
        """Clear all server state (useful for testing)"""
        self.messages.clear()
        self.message_status.clear()
        self.registered_agents.clear()
        self.waiting_queue.clear()
        self.current_game_in_progress = False
        print("🧹 All server state cleared")
    
    def get_message_status(self, message_id: str) -> str:
        """Get the delivery status of a message"""
        return self.message_status.get(message_id, "unknown")
    
    def mark_delivered(self, message_id: str) -> bool:
        """Mark a message as delivered"""
        if message_id in self.message_status:
            self.message_status[message_id] = "delivered"
            for msg in self.messages:
                if msg["message_id"] == message_id:
                    msg["status"] = "delivered"
                    break
            return True
        return False
    
    def mark_read(self, message_id: str) -> bool:
        """Mark a message as read"""
        if message_id in self.message_status:
            self.message_status[message_id] = "read"
            # Update message in messages list
            for msg in self.messages:
                if msg["message_id"] == message_id:
                    msg["status"] = "read"
                    break
            return True
        return False
    
    # ------------------------------------------------------------------
    # In-memory storage helpers (replaces Redis)
    # ------------------------------------------------------------------

    # ----------------------------
    # Queue helpers
    # ----------------------------

    async def join_queue(self, agent_id: str) -> int:
        """Push *agent_id* to the waiting_queue if not already present.

        Returns the new queue length.  Raises ValueError if the ID is already
        queued.
        """
        async with self._queue_lock:
            # If the agent is already in a running game (e.g. it dropped and
            # reconnected mid-game), don't queue it again — that would let it be
            # assigned to a second game at once. It is re-queued normally when its
            # current game ends (if still connected).
            if agent_id in self.agent_to_game:
                return len(self.waiting_queue)

            # Check for duplicates
            if agent_id in self.waiting_queue:
                raise ValueError("Agent already queued")

            # Add to queue
            self.waiting_queue.append(agent_id)
            queue_len = len(self.waiting_queue)
            
            print(f"📝 Agent {agent_id} joined queue (position {queue_len})")

            # Pool briefly, then form Elo-matched games (see _schedule_matchmaking).
            self._schedule_matchmaking()

            return queue_len

    def _schedule_matchmaking(self) -> None:
        """Debounce game formation until arrivals quiet down, so finishers from
        concurrent games pool together before the Elo matcher groups them. Resets
        the quiet-period deadline on each call (capped by MATCHMAKING_MAX_SEC so a
        continuous trickle can't starve formation). Safe to call under _queue_lock
        (it only updates timing fields and may schedule one waiter task)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # No running loop (not expected in normal operation): form immediately.
            self._launch_ready_games()
            return
        now = loop.time()
        if self._mm_first_at is None:
            self._mm_first_at = now
        self._mm_deadline = min(now + MATCHMAKING_WINDOW_SEC,
                                self._mm_first_at + MATCHMAKING_MAX_SEC)
        if not self._matchmaking_scheduled:
            self._matchmaking_scheduled = True
            asyncio.create_task(self._matchmaking_loop())

    async def _matchmaking_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            delay = self._mm_deadline - loop.time()
            if delay <= 0:
                break
            await asyncio.sleep(delay)
        async with self._queue_lock:
            self._matchmaking_scheduled = False
            self._mm_first_at = None
            self._launch_ready_games()

    def _launch_ready_games(self) -> None:
        """Form & launch all ready Elo-matched games. Assumes _queue_lock is held.

        With MAX_CONCURRENT_GAMES == 0 (default) this is unlimited: it keeps
        launching games while at least NUM_AGENTS agents are waiting.
        """
        ratings = _current_ratings()
        while len(self.waiting_queue) >= NUM_AGENTS:
            if MAX_CONCURRENT_GAMES and len(self.active_games) >= MAX_CONCURRENT_GAMES:
                break
            group = _select_matched_group(self.waiting_queue, NUM_AGENTS, ratings)
            for a in group:
                self.waiting_queue.remove(a)
            self._spawn_game(group)

    def _spawn_game(self, agents: List[str]) -> None:
        """Spawn one game as its own process and start watching it."""
        self._game_counter += 1
        game_id = f"arena_{int(datetime.now().timestamp())}_{self._game_counter}"
        for a in agents:
            self.agent_to_game[a] = game_id
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.game.run_session",
             "--game-id", game_id, "--agents", ",".join(agents),
             "--server", "http://127.0.0.1:8000"],
            cwd=str(PROJECT_ROOT),
        )
        self.active_games[game_id] = {"agents": agents, "proc": proc}
        self.current_game_in_progress = True
        print(f"🎯 Launched game {game_id}: {agents} ({len(self.active_games)} game(s) running)")
        asyncio.create_task(self._watch_game(game_id))

    async def _watch_game(self, game_id: str) -> None:
        """Wait for a game process to exit, then requeue its agents and refill."""
        info = self.active_games.get(game_id)
        if not info:
            return
        proc = info["proc"]
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, proc.wait)
        except Exception as e:
            print(f"⚠️  Error waiting on game {game_id}: {e}")
        print(f"✅ Game {game_id} finished")
        async with self._queue_lock:
            agents = self.active_games.get(game_id, {}).get("agents", [])
            self.active_games.pop(game_id, None)
            self.departed_from.pop(game_id, None)
            for a in agents:
                if self.agent_to_game.get(a) == game_id:
                    del self.agent_to_game[a]
            self.current_game_in_progress = len(self.active_games) > 0

            if not COMPETITION_MODE:
                print("🧪 Not in competition mode (EMAIL_GAME_COMPETITION unset): not re-queuing.")
                return
            connected = set(manager.active.keys())
            for a in agents:
                if a in connected and a not in self.waiting_queue:
                    self.waiting_queue.append(a)
                    print(f"↩️  Re-queued {a} for the next game")
            # Pool with any other finishers before forming the next games.
            self._schedule_matchmaking()

    async def leave_queue(self, agent_id: str) -> bool:
        """Remove agent from waiting_queue if present.
        
        Returns True if agent was removed, False if not in queue.
        """
        async with self._queue_lock:
            if agent_id in self.waiting_queue:
                self.waiting_queue.remove(agent_id)
                print(f"📤 Agent {agent_id} left queue (remaining: {len(self.waiting_queue)})")
                return True
            return False


# Global email server instance
email_server = EmailServer()

# FastAPI app
app = FastAPI(title="The Email Game Email Server", version="1.0.0")

# ---------------------------------------------------------------------------
# Agent registration (Step 0-b of deployment plan)
# ---------------------------------------------------------------------------


class RegisterAgentRequest(BaseModel):
    agent_id: str
    rsa_public_key: str


@app.post("/register_agent", status_code=201)
async def register_agent(request: RegisterAgentRequest):
    """Register a remote agent and return a short-lived JWT."""

    print(f"🔐 Registration request for {request.agent_id}")
    print(f"📋 Currently registered agents: {list(email_server.registered_agents.keys())}")

    # Name-collision protection: an agent_id is locked to the public key that
    # first claimed it. The same key may re-register freely (reconnect / token
    # refresh); a different key is rejected so two players can't share a name.
    existing = email_server.registered_agents.get(request.agent_id)
    if existing is not None and existing.get("rsa_public_key") != request.rsa_public_key:
        print(f"❌ Agent {request.agent_id} name already taken by a different key")
        raise HTTPException(
            status_code=409,
            detail=(f"Agent ID '{request.agent_id}' is already taken by another "
                    f"player. Choose a different agent_id."),
        )

    # New registration, or same player reconnecting → (re)store and issue token
    email_server.registered_agents[request.agent_id] = {
        "rsa_public_key": request.rsa_public_key
    }
    print(f"✅ Agent {request.agent_id} registered successfully")

    # Generate JWT – 30-minute expiry
    payload = {
        "sub": request.agent_id,
        "exp": datetime.utcnow().timestamp() + 1800,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    return {"success": True, "token": token}


@app.get("/")
async def root():
    """Root endpoint - simple dashboard info"""
    return {
        "service": "The Email Game Email Server",
        "status": "running",
        "registered_agents": len(email_server.registered_agents),
        "waiting_queue": len(email_server.waiting_queue),
        "game_in_progress": email_server.current_game_in_progress,
        "leaderboard": "/leaderboard",
        "api_docs": "/docs"
    }

@app.get("/agent_public_key/{agent_id}")
async def get_agent_public_key(agent_id: str):
    """Return the RSA public key submitted by an agent at registration time."""
    agent = email_server.registered_agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not registered")
    return {"agent_id": agent_id, "rsa_public_key": agent["rsa_public_key"]}


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "message_count": len(email_server.messages)}


@app.post("/clear_state")
async def clear_state(request: Request):
    """Clear all server state (for testing). Internal-only during a competition."""
    _require_internal(request)
    email_server.clear_all_state()
    return {"success": True, "message": "Server state cleared"}


@app.get("/session_results")
async def get_session_results(request: Request):
    """Get list of available session result files. Internal-only during a competition."""
    _require_internal(request)
    try:
        results_dir = Path(__file__).resolve().parent.parent / "session_results"
        if not results_dir.exists():
            return {"success": True, "files": []}
        
        session_files = list(results_dir.glob("session_arena_*.json"))
        file_info = []
        
        for file_path in session_files:
            file_info.append({
                "filename": file_path.name,
                "size": file_path.stat().st_size,
                "modified": file_path.stat().st_mtime
            })
        
        # Sort by modification time (newest first)
        file_info.sort(key=lambda x: x["modified"], reverse=True)
        
        return {"success": True, "files": file_info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get session results: {str(e)}")


@app.get("/session_results/{filename}")
async def get_session_result(filename: str, request: Request):
    """Get a specific session result file. Internal-only during a competition."""
    _require_internal(request)
    try:
        results_dir = Path(__file__).resolve().parent.parent / "session_results"
        file_path = results_dir / filename
        
        # Security check - ensure filename is safe
        if not filename.endswith('.json') or '..' in filename or '/' in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Session result not found")
        
        with open(file_path, 'r') as f:
            session_data = json.load(f)

        return {"success": True, "data": session_data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read session result: {str(e)}")


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page():
    """Human-friendly Elo leaderboard across all sessions."""
    try:
        entries = compute_leaderboard()
        live = {
            "players": len(manager.active),            # agents connected right now
            "matches": len(email_server.active_games),  # games currently running
            "in_game": len(email_server.agent_to_game), # players inside a match
            "queued": len(email_server.waiting_queue),  # players waiting to be matched
        }
        return HTMLResponse(render_leaderboard_html(entries, live))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build leaderboard: {str(e)}")


@app.get("/api/leaderboard")
async def leaderboard_api():
    """Machine-readable Elo leaderboard across all sessions."""
    try:
        entries = compute_leaderboard()
        live = {
            "players": len(manager.active),
            "matches": len(email_server.active_games),
            "in_game": len(email_server.agent_to_game),
            "queued": len(email_server.waiting_queue),
        }
        return {"success": True, "leaderboard": entries, "live": live}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build leaderboard: {str(e)}")


@app.get("/agent/{agent_id}", response_class=HTMLResponse)
async def agent_page(agent_id: str):
    """Per-agent stats: attack/defense/collection rates and per-game breakdown."""
    try:
        return HTMLResponse(render_agent_html(compute_agent_report(agent_id)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build agent stats: {str(e)}")


@app.get("/api/agent/{agent_id}")
async def agent_api(agent_id: str):
    """Machine-readable per-agent stats."""
    try:
        return {"success": True, "report": compute_agent_report(agent_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build agent stats: {str(e)}")


@app.post("/send_message")
async def send_message(request: SendMessageRequest, token_agent: str = Depends(_require_token)):
    """Send a message from one agent to another"""
    # Validate recipient
    if not _validate_recipient(request.to):
        raise HTTPException(status_code=400, detail=f"Invalid recipient: {request.to}")
    
    try:
        # Sender is derived from JWT token, not client payload
        message_data = {
            "from_agent": token_agent,
            "to": request.to,
            "subject": request.subject,
            "body": request.body
        }
        
        message_id = email_server.store_message(message_data)
        
        return {
            "success": True,
            "message_id": message_id,
            "status": "sent"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send message: {str(e)}")


@app.post("/send_message_queued")
async def send_message_queued(request: SendMessageRequest, token_agent: str = Depends(_require_token)):
    """Send a message via the queue (better for concurrent requests)"""
    # Validate recipient
    if not _validate_recipient(request.to):
        raise HTTPException(status_code=400, detail=f"Invalid recipient: {request.to}")
    
    try:
        # Sender is derived from JWT token, not client payload
        message_data = {
            "from_agent": token_agent,
            "to": request.to,
            "subject": request.subject,
            "body": request.body
        }
        
        message_id = await email_server.store_message_queued(message_data)
        
        return {
            "success": True,
            "message_id": message_id,
            "status": "queued"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to queue message: {str(e)}")


@app.post("/send_batch")
async def send_batch_messages(
    request: BatchSendRequest,
    token_agent: str = Depends(_require_token),
):
    """Send multiple messages at once (optimized for moderator instructions)"""
    try:
        results = []
        
        # Validate all recipients first
        for msg_request in request.messages:
            if not _validate_recipient(msg_request.to):
                raise HTTPException(status_code=400, detail=f"Invalid recipient in batch: {msg_request.to}")
        
        # Queue all messages concurrently
        tasks = []
        for msg_request in request.messages:
            # Sender is derived from JWT token, not client payload
            message_data = {
                "from_agent": token_agent,
                "to": msg_request.to,
                "subject": msg_request.subject,
                "body": msg_request.body
            }
            task = email_server.store_message_queued(message_data)
            tasks.append(task)
        
        # Wait for all messages to be processed
        message_ids = await asyncio.gather(*tasks)
        
        for i, message_id in enumerate(message_ids):
            results.append({
                "to": request.messages[i].to,
                "message_id": message_id,
                "status": "queued"
            })
        
        return {
            "success": True,
            "messages_sent": len(results),
            "results": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send batch: {str(e)}")


@app.get("/get_messages/{agent_id}")
async def get_messages(agent_id: str, request: Request):
    """Get all messages for a specific agent (your own only, during a competition)."""
    _require_own_mailbox(request, agent_id)
    try:
        messages = email_server.get_messages_for_agent(agent_id)
        
        # Mark messages as delivered when retrieved
        for msg in messages:
            if msg["status"] == "sent":
                email_server.mark_delivered(msg["message_id"])
        
        return {
            "success": True,
            "agent_id": agent_id,
            "messages": messages,
            "count": len(messages)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {str(e)}")


@app.get("/submissions/{game_id}")
async def get_submissions(game_id: str, request: Request):
    """Submissions (moderator-addressed mail) for one game only, plus the set of
    agents who left this game. Internal-only during a competition."""
    _require_internal(request)
    msgs = email_server.get_submissions(game_id)
    departed = sorted(email_server.departed_from.get(game_id, set()))
    return {"success": True, "messages": msgs, "count": len(msgs), "departed": departed}


@app.get("/get_all_messages")
async def get_all_messages(request: Request):
    """Get all messages in the system. Internal-only during a competition (this
    would otherwise let any player read every agent's mail)."""
    _require_internal(request)
    try:
        messages = email_server.get_all_messages()
        return {
            "success": True,
            "messages": messages,
            "count": len(messages)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get all messages: {str(e)}")


@app.put("/mark_read/{message_id}")
async def mark_message_read(message_id: str):
    """Mark a message as read"""
    try:
        success = email_server.mark_read(message_id)
        if success:
            return {
                "success": True,
                "message_id": message_id,
                "status": "read"
            }
        else:
            raise HTTPException(status_code=404, detail="Message not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark message as read: {str(e)}")


@app.get("/message_status/{message_id}")
async def get_message_status(message_id: str):
    """Get the status of a specific message"""
    try:
        status = email_server.get_message_status(message_id)
        if status == "unknown":
            raise HTTPException(status_code=404, detail="Message not found")
        
        return {
            "success": True,
            "message_id": message_id,
            "status": status
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get message status: {str(e)}")




@app.get("/get_sent/{agent_id}")
async def get_sent_messages(agent_id: str, request: Request):
    """Get all messages that a specific agent has sent (your own only, during a competition)."""
    _require_own_mailbox(request, agent_id)
    try:
        sent_messages = [msg for msg in email_server.messages if msg["from"] == agent_id]
        # No status mutation for sent mail – outbox should reflect original state
        return {
            "success": True,
            "agent_id": agent_id,
            "messages": sent_messages,
            "count": len(sent_messages)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get sent messages: {str(e)}")


@app.get("/get_conversation/{agent_id}")
async def get_conversation(agent_id: str, request: Request):
    """All messages involving the agent (your own only, during a competition)."""
    _require_own_mailbox(request, agent_id)
    try:
        # Filter messages where the agent is either sender or recipient
        related = [msg for msg in email_server.messages if msg["from"] == agent_id or msg["to"] == agent_id]

        # Sort by timestamp (ISO strings sort lexicographically in the same order as datetimes)
        related.sort(key=lambda m: m["timestamp"])

        # Mark incoming *unseen* messages as delivered (same rule as inbox endpoint)
        for msg in related:
            if msg["to"] == agent_id and msg["status"] == "sent":
                email_server.mark_delivered(msg["message_id"])

        return {
            "success": True,
            "agent_id": agent_id,
            "messages": related,
            "count": len(related)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get conversation: {str(e)}")


# ---------------------------------------------------------------------------
# Queue endpoint – Step 0-c
# ---------------------------------------------------------------------------


class JoinQueueRequest(BaseModel):
    agent_id: str


@app.post("/join_queue")
async def join_queue(
    payload: JoinQueueRequest,
    token_agent: str = Depends(_require_token),
):
    """Add agent to waiting_queue and return current length."""

    if token_agent != payload.agent_id:
        raise HTTPException(status_code=403, detail="Token/agent mismatch")

    try:
        new_len = await email_server.join_queue(payload.agent_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Agent already queued")

    return {"success": True, "position": new_len}


@app.post("/leave_queue")
async def leave_queue_endpoint(
    token_agent: str = Depends(_require_token),
):
    """Remove agent from waiting queue."""
    removed = await email_server.leave_queue(token_agent)
    return {"success": True, "removed": removed}


# Queue status endpoint will be added after ConnectionManager is instantiated


# ----------------------------
# WebSocket connection manager
# ----------------------------


class ConnectionManager:
    """Keeps track of active WebSocket connections per agent and allows sending push notifications."""

    def __init__(self):
        # agent_id -> set[WebSocket]
        self.active: Dict[str, set] = {}

    async def connect(self, agent_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active.setdefault(agent_id, set()).add(websocket)
        print(f"🔗 WebSocket connected for agent {agent_id} (total: {len(self.active.get(agent_id, set()))} connections)")

    def disconnect(self, agent_id: str, websocket: WebSocket):
        if agent_id in self.active and websocket in self.active[agent_id]:
            self.active[agent_id].remove(websocket)
            if not self.active[agent_id]:
                # clean empty entry
                self.active.pop(agent_id, None)
                # Fully disconnected. Don't leave the match immediately: give a
                # grace window for a transient blip to reconnect and resume the
                # same game. If still gone after the window, leave for good.
                try:
                    asyncio.create_task(self._leave_after_grace(agent_id))
                except RuntimeError:
                    self._finalize_leave(agent_id)

    async def _leave_after_grace(self, agent_id: str):
        await asyncio.sleep(DISCONNECT_GRACE_SEC)
        if agent_id in self.active:
            return  # reconnected within the grace window — stays in its match
        self._finalize_leave(agent_id)

    def _finalize_leave(self, agent_id: str):
        """Remove a gone-for-good agent from its match and the queue.

        After this it cannot be routed back into a still-running game, its
        signatures stop counting in that game, and it only re-joins the queue
        for FUTURE matches when it reconnects. Runs on the event loop, so it
        doesn't race the queue lock.
        """
        if agent_id in self.active:
            return  # came back at the last moment
        left_game = email_server.agent_to_game.pop(agent_id, None)
        if agent_id in email_server.waiting_queue:
            email_server.waiting_queue.remove(agent_id)
        if left_game:
            email_server.departed_from.setdefault(left_game, set()).add(agent_id)
            print(f"👋 {agent_id} left match {left_game}; cannot rejoin or affect it")

    async def send_json(self, agent_id: str, payload: Dict):
        """Send payload to all websockets listening for *agent_id*."""
        if agent_id not in self.active:
            _dbg(f"⚠️  No WebSocket connections for agent {agent_id}")
            return
        
        _dbg(f"📡 Sending WebSocket message to {agent_id} ({len(self.active[agent_id])} connections)")
        dead_connections = []
        sent_count = 0
        
        for ws in list(self.active[agent_id]):
            try:
                await ws.send_json(payload)
                sent_count += 1
            except Exception as e:
                print(f"⚠️  WebSocket send failed: {e}")
                dead_connections.append(ws)
                
        for ws in dead_connections:
            self.disconnect(agent_id, ws)
            
        _dbg(f"✅ WebSocket message sent to {sent_count} connections for {agent_id}")


# Instantiate global connection manager
manager = ConnectionManager()


@app.get("/queue_status")
async def get_queue_status():
    """Get current queue status and connected agents."""
    connected_agents = list(manager.active.keys())
    queue_agents = email_server.waiting_queue.copy()
    
    return {
        "queue_length": len(queue_agents),
        "agents_waiting": queue_agents,
        "connected_agents": connected_agents,
        "game_in_progress": email_server.current_game_in_progress
    }


@app.websocket("/ws/{agent_id}")
async def websocket_endpoint(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint that streams new messages to *agent_id* in real-time."""
    # Expect JWT via query param ?token=... (simpler for browser/agent clients)
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)  # unauthorized
        return

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        sub = payload.get("sub")
        if sub != agent_id:
            await websocket.close(code=4403)  # forbidden
            return
    except jwt.InvalidTokenError:
        await websocket.close(code=4401)
        return

    await manager.connect(agent_id, websocket)
    try:
        while True:
            # Keep the connection alive – we don't expect the agent to send data.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(agent_id, websocket)
        # Remove from queue when disconnecting
        # Note: No JWT needed here - we already authenticated this WebSocket connection
        # and trust the agent_id from the authenticated session
        await email_server.leave_queue(agent_id)
        print(f"🔌 Agent {agent_id} disconnected and removed from queue")


# ---------------------------------------------------------------------------
# Game launching now lives on the EmailServer (process-per-game): see
# _launch_ready_games / _spawn_game / _watch_game. Each game runs in its own
# process for full state isolation, and any number can run concurrently.
# ---------------------------------------------------------------------------

# No startup hooks needed - games start directly from queue


if __name__ == "__main__":
    print("Starting The Email Game Email Server...")
    print("API documentation available at: http://localhost:8000/docs")
    # Quiet HTTP access-log spam unless EMAIL_GAME_DEBUG is set.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info" if DEBUG_LOGS else "warning")