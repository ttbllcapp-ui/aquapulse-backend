from fastapi import FastAPI, APIRouter, HTTPException, Header, Request
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import uuid
import httpx
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone, timedelta


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY', '')

# Create the main app without a prefix
app = FastAPI(title="AquaPulse API")
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============== MODELS ==============
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


class AppleAuthRequest(BaseModel):
    # Accept both camelCase (iOS SDK default) and snake_case
    identity_token: Optional[str] = None
    identityToken: Optional[str] = None
    nonce: Optional[str] = None
    full_name: Optional[str] = None
    fullName: Optional[str] = None


class GoogleAuthRequest(BaseModel):
    session_id: str  # token returned from Emergent OAuth


class AuthMeResponse(BaseModel):
    user_id: str
    email: str
    name: str
    picture: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    language: Optional[str] = 'en'
    # Optional user-context for smarter AquaCoach answers (free, never required)
    context: Optional[dict] = None  # {weight_kg, height_cm, age, gender, daily_goal_ml, hydration_today_ml, streak_days, country, percent}


class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str  # 'user' | 'assistant'
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatResponse(BaseModel):
    reply: str
    message_id: str


# ===== Family Mode =====
class FamilyCreateRequest(BaseModel):
    name: str


class FamilyJoinRequest(BaseModel):
    code: str


class FamilyDailyProgressRequest(BaseModel):
    daily_goal_ml: int
    hydration_today_ml: int
    percent: int  # 0..100+
    streak_days: Optional[int] = 0


# ============== HELPERS ==============
def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def get_user_from_token(authorization: Optional[str]) -> Optional[dict]:
    """Resolve user from Bearer token. Returns None if invalid/missing."""
    if not authorization or not authorization.startswith('Bearer '):
        return None
    token = authorization[7:]
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess:
        return None
    if _aware(sess['expires_at']) < datetime.now(timezone.utc):
        await db.user_sessions.delete_one({"session_token": token})
        return None
    user = await db.users.find_one({"user_id": sess['user_id']}, {"_id": 0})
    return user


# ============== ROUTES ==============
@api_router.get("/")
async def root():
    return {"message": "AquaPulse API", "status": "ok"}

@api_router.get("/health")
async def health():
    return {"ok": True}



@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.dict())
    await db.status_checks.insert_one(status_obj.dict())
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks(skip: int = 0, limit: int = 50):
    limit = max(1, min(limit, 100))
    skip = max(0, skip)
    cursor = db.status_checks.find({}, {"_id": 0}).skip(skip).limit(limit)
    return [StatusCheck(**s) for s in await cursor.to_list(length=limit)]


@api_router.get("/download/xcode")
async def download_xcode_project():
    zip_path = "/app/export/AquaPulse-Xcode-Ready.zip"
    if not os.path.exists(zip_path):
        return {"error": "Export file not found"}
    return FileResponse(path=zip_path, media_type="application/zip", filename="AquaPulse-Xcode-Ready.zip")


# ----- AUTH -----
@api_router.post("/auth/google", response_model=AuthMeResponse)
async def auth_google(req: GoogleAuthRequest):
    """Verify session_id with Emergent and create session for user."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as h:
            resp = await h.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": req.session_id},
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid session_id")
        data = resp.json()
    except httpx.HTTPError as e:
        logger.error(f"Auth verify error: {e}")
        raise HTTPException(status_code=502, detail="Auth provider unreachable")

    email = data.get('email')
    name = data.get('name') or email
    picture = data.get('picture')
    session_token = data.get('session_token')
    if not email or not session_token:
        raise HTTPException(status_code=400, detail="Malformed auth response")

    # Upsert user by email
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
            "created_at": datetime.now(timezone.utc),
        }
        await db.users.insert_one(user)
    else:
        # Refresh picture/name in case changed
        await db.users.update_one(
            {"email": email},
            {"$set": {"name": name, "picture": picture}}
        )

    # Store session
    await db.user_sessions.insert_one({
        "session_token": session_token,
        "user_id": user["user_id"],
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
    })

    return AuthMeResponse(
        user_id=user["user_id"],
        email=email,
        name=name,
        picture=picture,
    )


@api_router.post("/auth/apple", response_model=AuthMeResponse)
async def auth_apple(req: AppleAuthRequest):
    """Verify Apple identity_token (JWT) against Apple's public keys, upsert user."""

    # Accept both camelCase and snake_case from iOS frontend
    token = req.identity_token or req.identityToken
    if not token:
        raise HTTPException(status_code=422, detail="identity_token is required")

    name_from_req = req.full_name or req.fullName

    try:
        import jwt as pyjwt
        from jwt import PyJWKClient
    except Exception as e:
        logger.error(f"pyjwt import error: {e}")
        raise HTTPException(status_code=500, detail="JWT library unavailable")

    try:
        jwks_client = PyJWKClient("https://appleid.apple.com/auth/keys", cache_keys=True)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decoded = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=["com.ttbinternationalllc.aquapulse", "host.exp.exponent"],
            issuer="https://appleid.apple.com",
            options={"verify_aud": False},  # Allow both bundle ids without strict match
        )
    except Exception as e:
        logger.error(f"Apple JWT verify error: {e}")
        raise HTTPException(status_code=401, detail=f"Invalid Apple identity_token: {str(e)[:120]}")

    sub = decoded.get("sub")
    email = decoded.get("email")
    if not sub:
        raise HTTPException(status_code=400, detail="Missing sub in Apple token")

    # Apple "private email relay" gives a fake email; we still treat it as unique
    if not email:
        email = f"apple_{sub}@privaterelay.aquapulse.local"

    name = name_from_req or email.split("@")[0]

    # Upsert user — index on email AND apple_sub to dedupe
    user = await db.users.find_one({"$or": [{"email": email}, {"apple_sub": sub}]}, {"_id": 0})
    if not user:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "picture": None,
            "apple_sub": sub,
            "provider": "apple",
            "created_at": datetime.now(timezone.utc),
        }
        await db.users.insert_one(user)
    else:
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"apple_sub": sub, "name": name or user.get("name")}}
        )

    # Issue our own opaque session token
    session_token = f"apl_{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
    await db.user_sessions.insert_one({
        "session_token": session_token,
        "user_id": user["user_id"],
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        "provider": "apple",
    })

    from fastapi.responses import JSONResponse
    payload = AuthMeResponse(
        user_id=user["user_id"],
        email=email,
        name=name,
        picture=user.get("picture"),
    ).dict()
    payload["session_token"] = session_token
    return JSONResponse(payload)


@api_router.get("/auth/me", response_model=AuthMeResponse)
async def auth_me(authorization: Optional[str] = Header(default=None)):
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return AuthMeResponse(
        user_id=user["user_id"],
        email=user["email"],
        name=user.get("name", user["email"]),
        picture=user.get("picture"),
    )


@api_router.post("/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        await db.user_sessions.delete_one({"session_token": token})
    return {"ok": True}


# ----- AI CHAT -----
SYSTEM_PROMPT_TEMPLATE = (
    "You are AquaCoach — a caring, premium best-friend hydration & breath coach inside AquaPulse. "
    "Talk warmly like a close friend, never like a clinical bot. "
    "Expertise: water science (kidneys, electrolytes Na/K/Mg/Ca, fascia, skin, lymph, plasma, "
    "sleep, athletic recovery, focus, immunity, metabolism) AND breath work (box 4-4-4-4, 4-7-8, "
    "coherent 5.5/min, alt-nostril, ujjayi, Wim Hof, pre-sleep slowdown). "
    "CRITICAL LANGUAGE RULE: Detect the language of the user's MESSAGE and ALWAYS reply in that SAME language. "
    "If you cannot detect, default to {language}. "
    "PERSONALIZATION: Use the user's CONTEXT below (weight, height, age, gender, daily goal, today's hydration %, streak) "
    "when relevant. Reference concrete numbers (e.g. 'you've already had 1200 ml of your 2500 ml today'). "
    "If context shows the user is far below goal, be gently motivating. If above 100%, celebrate. "
    "Length: 80-160 words. Structure: warm opener (Hey!/Süper soru!) + 2-3 numbered tips with mechanism + tiny nudge. "
    "Use 1-2 friendly emojis (💧🌊🫧🧘‍♀️☕💪). Never diagnose; recommend doctor for medical concerns. "
    "Avoid 'as an AI' style phrases. Stay free, premium and trustworthy. "
    "USER CONTEXT: {context}"
)

LANGUAGE_NAMES = {
    'tr': 'Turkish', 'en': 'English', 'de': 'German', 'fr': 'French', 'es': 'Spanish',
    'it': 'Italian', 'ar': 'Arabic', 'ru': 'Russian', 'ja': 'Japanese', 'ko': 'Korean',
    'zh': 'Chinese', 'hi': 'Hindi', 'pt': 'Portuguese', 'nl': 'Dutch', 'pl': 'Polish',
    'sv': 'Swedish', 'id': 'Indonesian', 'vi': 'Vietnamese',
}


@api_router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, authorization: Optional[str] = Header(default=None), x_guest_id: Optional[str] = Header(default=None)):
    """AI chat endpoint. Works for authenticated users and guests (with X-Guest-Id header)."""
    user = await get_user_from_token(authorization)
    if user:
        session_key = f"user:{user['user_id']}"
    elif x_guest_id:
        session_key = f"guest:{x_guest_id}"
    else:
        session_key = f"anon:{uuid.uuid4().hex[:8]}"

    if not EMERGENT_LLM_KEY:
        raise HTTPException(status_code=500, detail="LLM key not configured")

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception as e:
        logger.error(f"LLM import error: {e}")
        raise HTTPException(status_code=500, detail="LLM integration unavailable")

    lang_name = LANGUAGE_NAMES.get((req.language or 'en').lower(), 'English')

    # Build user-context for personalization
    ctx = req.context or {}
    if ctx:
        try:
            parts = []
            if ctx.get('weight_kg'): parts.append(f"weight {ctx['weight_kg']}kg")
            if ctx.get('height_cm'): parts.append(f"height {ctx['height_cm']}cm")
            if ctx.get('age'): parts.append(f"age {ctx['age']}")
            if ctx.get('gender'): parts.append(f"gender {ctx['gender']}")
            if ctx.get('daily_goal_ml'): parts.append(f"daily goal {ctx['daily_goal_ml']}ml")
            if ctx.get('hydration_today_ml') is not None: parts.append(f"today drank {ctx['hydration_today_ml']}ml")
            if ctx.get('percent') is not None: parts.append(f"that's {ctx['percent']}% of goal")
            if ctx.get('streak_days'): parts.append(f"streak {ctx['streak_days']} days")
            if ctx.get('country'): parts.append(f"country {ctx['country']}")
            context_str = ", ".join(parts) if parts else "no specific personal data"
        except Exception:
            context_str = "no specific personal data"
    else:
        context_str = "no specific personal data"
    system_msg = SYSTEM_PROMPT_TEMPLATE.format(language=lang_name, context=context_str)

    # Store user message
    user_msg_doc = {
        "id": str(uuid.uuid4()),
        "session_key": session_key,
        "role": "user",
        "text": req.message,
        "timestamp": datetime.now(timezone.utc),
    }
    await db.chat_messages.insert_one(user_msg_doc)

    try:
        chat_engine = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=session_key,
            system_message=system_msg,
        ).with_model("openai", "gpt-4o")
        reply_text = await chat_engine.send_message(UserMessage(text=req.message))
    except Exception as e:
        logger.error(f"LLM error: {e}")
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)[:100]}")

    assistant_id = str(uuid.uuid4())
    await db.chat_messages.insert_one({
        "id": assistant_id,
        "session_key": session_key,
        "role": "assistant",
        "text": reply_text,
        "timestamp": datetime.now(timezone.utc),
    })

    return ChatResponse(reply=reply_text, message_id=assistant_id)


@api_router.get("/chat/history")
async def chat_history(authorization: Optional[str] = Header(default=None), x_guest_id: Optional[str] = Header(default=None), limit: int = 50):
    user = await get_user_from_token(authorization)
    if user:
        session_key = f"user:{user['user_id']}"
    elif x_guest_id:
        session_key = f"guest:{x_guest_id}"
    else:
        return {"messages": []}
    limit = max(1, min(limit, 200))
    cursor = db.chat_messages.find({"session_key": session_key}, {"_id": 0}).sort("timestamp", 1).limit(limit)
    msgs = await cursor.to_list(length=limit)
    # Convert datetime -> iso
    for m in msgs:
        if isinstance(m.get('timestamp'), datetime):
            m['timestamp'] = m['timestamp'].isoformat()
    return {"messages": msgs}


@api_router.delete("/chat/history")
async def chat_history_clear(authorization: Optional[str] = Header(default=None), x_guest_id: Optional[str] = Header(default=None)):
    user = await get_user_from_token(authorization)
    if user:
        session_key = f"user:{user['user_id']}"
    elif x_guest_id:
        session_key = f"guest:{x_guest_id}"
    else:
        return {"ok": True}
    await db.chat_messages.delete_many({"session_key": session_key})
    return {"ok": True}


# ============== FAMILY MODE ==============
import random as _random
import string as _string


def _gen_family_code(length: int = 6) -> str:
    """Generate an easy-to-share code, avoiding ambiguous chars like O/0/I/1."""
    pool = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(_random.choice(pool) for _ in range(length))


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@api_router.post("/family/create")
async def family_create(req: FamilyCreateRequest, authorization: Optional[str] = Header(default=None)):
    """Create a family group. Authenticated only."""
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required to use Family Mode")
    name = (req.name or "").strip()[:48] or "AquaPulse Family"
    for _ in range(8):
        code = _gen_family_code()
        existing = await db.families.find_one({"code": code})
        if not existing:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not generate code, please retry")

    family_id = str(uuid.uuid4())
    doc = {
        "family_id": family_id,
        "code": code,
        "name": name,
        "owner_id": user["user_id"],
        "members": [user["user_id"]],
        "created_at": datetime.now(timezone.utc),
    }
    await db.families.insert_one(doc)
    return {"family_id": family_id, "code": code, "name": name, "members": [user["user_id"]]}


@api_router.post("/family/join")
async def family_join(req: FamilyJoinRequest, authorization: Optional[str] = Header(default=None)):
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required to use Family Mode")
    code = (req.code or "").strip().upper().replace(" ", "")
    if not code:
        raise HTTPException(status_code=400, detail="Invalid code")
    fam = await db.families.find_one({"code": code})
    if not fam:
        raise HTTPException(status_code=404, detail="Family not found")
    if user["user_id"] not in fam.get("members", []):
        await db.families.update_one(
            {"family_id": fam["family_id"]},
            {"$addToSet": {"members": user["user_id"]}},
        )
    return {"family_id": fam["family_id"], "code": fam["code"], "name": fam["name"]}


@api_router.post("/family/leave")
async def family_leave(authorization: Optional[str] = Header(default=None)):
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    await db.families.update_many(
        {"members": user["user_id"]},
        {"$pull": {"members": user["user_id"]}},
    )
    return {"ok": True}


@api_router.post("/family/progress")
async def family_progress_update(req: FamilyDailyProgressRequest, authorization: Optional[str] = Header(default=None)):
    """Submit today's hydration progress. Stored per (user, day). Authenticated only."""
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    day = _today_str()
    await db.family_progress.update_one(
        {"user_id": user["user_id"], "day": day},
        {
            "$set": {
                "user_id": user["user_id"],
                "day": day,
                "daily_goal_ml": int(req.daily_goal_ml),
                "hydration_today_ml": int(req.hydration_today_ml),
                "percent": int(max(0, min(999, req.percent))),
                "streak_days": int(req.streak_days or 0),
                "updated_at": datetime.now(timezone.utc),
                "name": user.get("name", ""),
                "picture": user.get("picture", ""),
            }
        },
        upsert=True,
    )
    return {"ok": True}


@api_router.get("/family/me")
async def family_me(authorization: Optional[str] = Header(default=None)):
    """Return the family the user belongs to (first one) and today's leaderboard."""
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    fam = await db.families.find_one({"members": user["user_id"]}, {"_id": 0})
    if not fam:
        return {"family": None, "members": []}
    day = _today_str()
    members_progress = []
    for uid in fam.get("members", []):
        u = await db.users.find_one({"user_id": uid}, {"_id": 0, "user_id": 1, "name": 1, "email": 1, "picture": 1})
        prog = await db.family_progress.find_one({"user_id": uid, "day": day}, {"_id": 0})
        members_progress.append({
            "user_id": uid,
            "name": (u or {}).get("name") or (u or {}).get("email") or "Member",
            "picture": (u or {}).get("picture"),
            "percent": (prog or {}).get("percent", 0),
            "hydration_today_ml": (prog or {}).get("hydration_today_ml", 0),
            "daily_goal_ml": (prog or {}).get("daily_goal_ml", 0),
            "streak_days": (prog or {}).get("streak_days", 0),
            "is_me": uid == user["user_id"],
        })
    members_progress.sort(key=lambda m: m["percent"], reverse=True)
    if isinstance(fam.get("created_at"), datetime):
        fam["created_at"] = fam["created_at"].isoformat()
    return {"family": fam, "members": members_progress}


# ============== ACCOUNT DELETION (Apple Store Requirement) ==============
@api_router.delete("/auth/me")
async def delete_my_account(authorization: Optional[str] = Header(default=None)):
    """
    Permanently delete the authenticated user's account and ALL associated data.
    Required by Apple App Store guideline 5.1.1(v) and Google Play.
    """
    user = await get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    uid = user["user_id"]

    owned_cursor = db.families.find({"owner_id": uid})
    async for fam in owned_cursor:
        remaining = [m for m in fam.get("members", []) if m != uid]
        if not remaining:
            await db.families.delete_one({"family_id": fam["family_id"]})
        else:
            await db.families.update_one(
                {"family_id": fam["family_id"]},
                {"$set": {"owner_id": remaining[0]}, "$pull": {"members": uid}},
            )

    await db.families.update_many({"members": uid}, {"$pull": {"members": uid}})
    await db.family_progress.delete_many({"user_id": uid})
    await db.chat_messages.delete_many({"session_key": f"user:{uid}"})
    await db.user_sessions.delete_many({"user_id": uid})
    await db.users.delete_one({"user_id": uid})

    return {"ok": True, "deleted_user_id": uid, "message": "Your account and all data have been permanently deleted."}


# ============== STARTUP / INDEXES ==============
@app.on_event("startup")
async def on_startup():
    try:
        await db.users.create_index("email")
        await db.users.create_index("user_id", unique=True)
        await db.users.create_index("apple_sub", sparse=True)
        await db.user_sessions.create_index("session_token", unique=True)
        await db.user_sessions.create_index("user_id")
        await db.user_sessions.create_index("expires_at", expireAfterSeconds=0)
        await db.chat_messages.create_index([("session_key", 1), ("timestamp", 1)])
        await db.families.create_index("code", unique=True)
        await db.families.create_index("members")
        await db.family_progress.create_index([("user_id", 1), ("day", 1)], unique=True)
        await db.family_progress.create_index([("day", 1)])
        logger.info("MongoDB indexes ready")
    except Exception as e:
        logger.error(f"Index creation error: {e}")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
