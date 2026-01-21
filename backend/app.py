import logging
import time
import os
import requests
from typing import Optional
from dotenv import load_dotenv

from flask import Flask, request, jsonify, current_app
from flask_cors import CORS

from sqlalchemy import text, func
from sqlalchemy.exc import SQLAlchemyError, OperationalError

# local imports
from backend.models import Base, engine, SessionLocal, User, Transaction, ReferralEvent


# -------------------------
# Load environment & logging
# -------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------------
# Flask app creation  ✅ MUST COME EARLY
# -------------------------
app = Flask(__name__)
CORS(app)

# -------------------------
# TON configuration
# -------------------------
TONCENTER_API = "https://toncenter.com/api/v3"
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")
TON_COMPANY_WALLET = os.getenv("TON_COMPANY_WALLET")

if not TONCENTER_API_KEY:
    logger.warning("TONCENTER_API_KEY is NOT set")

if not TON_COMPANY_WALLET:
    logger.warning("TON_COMPANY_WALLET is NOT set")

# -------------------------
# CONFIG ENDPOINT (USED BY TELEGRAM HTML)
# -------------------------
@app.route("/config", methods=["GET"])
def get_config():
    return jsonify({
        "treasury_ton_address": TON_COMPANY_WALLET
    })

# -------------------------
# TON API HELPERS
# -------------------------
def ton_api(method: str, params: dict):
    headers = {"X-API-Key": TONCENTER_API_KEY}
    r = requests.get(
        f"{TONCENTER_API}/{method}",
        params=params,
        headers=headers,
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def verify_ton_tx_by_hash(tx_hash: str, expected_amount_ton: float) -> bool:
    if not TONCENTER_API_KEY or not TON_COMPANY_WALLET:
        raise RuntimeError("TON env vars not set")

    r = requests.get(
        f"{TONCENTER_API}/transactions",
        params={
            "account": TON_COMPANY_WALLET,
            "limit": 20
        },
        headers={
            "X-API-Key": TONCENTER_API_KEY
        },
        timeout=10
    )

    r.raise_for_status()
    data = r.json()

    for tx in data.get("transactions", []):
        if tx.get("hash") != tx_hash:
            continue

        in_msg = tx.get("in_msg")
        if not in_msg:
            continue

        value = int(in_msg.get("value", 0)) / 1e9
        dst = in_msg.get("destination")

        if dst == TON_COMPANY_WALLET and value >= expected_amount_ton:
            return True

    return False

def verify_ton_tx_with_retry(
    tx_hash: str,
    expected_amount_ton: float,
    retries: int = 10,
    delay: int = 3,
) -> bool:
    """
    Retries TON verification to wait for blockchain indexing.
    """
    for attempt in range(retries):
        try:
            if verify_ton_tx_by_hash(tx_hash, expected_amount_ton):
                logger.info(
                    "TON verified on attempt %s for tx %s",
                    attempt + 1,
                    tx_hash[:10],
                )
                return True
        except Exception as e:
            logger.warning(
                "TON verify attempt %s failed: %s",
                attempt + 1,
                e,
            )

        time.sleep(delay)

    logger.error("TON verification timeout for tx %s", tx_hash[:10])
    return False

# -------------------------
# ENV / DEBUG
# -------------------------
ENV = os.getenv("ENV", "dev").strip().lower()
DEBUG_MODE = ENV != "prod"

logger.info("ENV=%s | DEBUG_MODE=%s", ENV, DEBUG_MODE)
logger.info("BOT_TOKEN loaded: %s", "YES" if os.getenv("BOT_TOKEN") else "NO")

# -------------------------
# DEBUG KEY CHECK (UNCHANGED)
# -------------------------
def check_debug_key():
    expected = current_app.config.get("DEBUG_KEY") or os.getenv("DEBUG_KEY")
    if not expected:
        current_app.logger.warning("DEBUG_KEY not set")
        return False

    expected = str(expected).strip()

    for k in ("X-DEBUG-KEY", "X-Debug-Key", "x-debug-key"):
        val = request.headers.get(k)
        if val and val.strip() == expected:
            return True

    for hk, hv in request.headers.items():
        if "debug" in hk.lower() and "key" in hk.lower():
            if hv.strip() == expected:
                return True

    for param in ("debug_key", "key"):
        q = request.args.get(param)
        if q and q.strip() == expected:
            return True

    return False

from functools import wraps

def debug_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not DEBUG_MODE:
            return jsonify(ok=False), 404
        if not check_debug_key():
            return jsonify(ok=False, error="invalid_debug_key"), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/debug/create_user", methods=["POST"])
@debug_only
def debug_create_user():

    data = request.get_json(silent=True) or {}

    try:
        user_id = int(data["id"])
    except Exception:
        return jsonify(ok=False, error="invalid_user_id"), 400

    db = SessionLocal()
    try:
        user = User(
            id=user_id,
            first_name=data.get("first_name"),
            username=data.get("username"),
            referrer_id=data.get("referrer_id")
        )
        db.add(user)
        db.commit()
        return jsonify(ok=True)

    except Exception as e:
        db.rollback()
        return jsonify(ok=False, error=str(e)), 400

    finally:
        db.close()


@app.route("/health")
def health():
    try:
        engine.connect().close()
        return {"ok": True, "db": "up"}
    except Exception as e:
        return {"ok": False, "db": "down", "error": str(e)}, 503

# show only first 6 chars of DEBUG_KEY to confirm it's present (do not leak secret)
_debug_key = os.getenv("DEBUG_KEY") or app.config.get("DEBUG_KEY")
if _debug_key:
    app.logger.info("DEBUG_KEY present (first6): %s", str(_debug_key)[:6])
else:
    app.logger.info("DEBUG_KEY NOT present in environment.")

# safe DB url display (mask credentials if you must print)
try:
    db_url = str(engine.url)
    if "@" in db_url and ":" in db_url:
        parts = db_url.split("@", 1)
        visible = parts[1]
        app.logger.info("Flask DB URL (masked): %s", visible)
    else:
        app.logger.info("Flask DB URL: %s", db_url)
except Exception:
    app.logger.exception("Could not read engine.url")

app.logger.info("Flask CWD: %s", os.getcwd())
app.logger.info("Flask DB URL: %s", engine.url)

# -------------------------
# Helpers
# -------------------------

def split_deposit_amount(amount_ton: float):
    """
    Converts TON deposit into internal balances.
    """
    musd = round(amount_ton * 0.70, 6)
    mstc = round(amount_ton * 0.30, 6)
    return musd, mstc


def get_or_create_user(db, tg_user: dict):
    """
    Create user if not exists.
    tg_user = {id, first_name, username}
    """
    user = db.query(User).filter(User.id == tg_user["id"]).first()

    if user:
        return user

    user = User(
        id=tg_user["id"],
        first_name=tg_user.get("first_name"),
        username=tg_user.get("username"),
        role="user",
        self_activated=False,
        balance_musd=0.0,
        balance_mstc=0.0,
        total_team_business=0.0,
        active_origin_count=0,
        created_at=datetime.utcnow(),
    )

    db.add(user)
    db.commit()
    db.refresh(user)
    return user

@app.route("/debug/routes", methods=["GET"])
@debug_only
def debug_routes():
    
    routes = []
    for r in app.url_map.iter_rules():
        routes.append({
            "rule": r.rule,
            "methods": sorted(list(r.methods)),
            "endpoint": r.endpoint
        })
    return jsonify(ok=True, routes=routes)


def get_ref_from_payload(data: dict) -> Optional[int]:
    ref = data.get("ref")
    try:
        return int(ref) if ref is not None else None
    except (ValueError, TypeError):
        return None

def link_referrer_if_needed(db, user: User, maybe_referrer_id: int | None):
    if user.referrer_id is not None:
        return
    if not maybe_referrer_id:
        return
    if maybe_referrer_id == user.id:
        return
    ref = db.get(User, maybe_referrer_id)
    if not ref:
        return
    user.referrer_id = ref.id
    db.commit()
    db.refresh(user)

def get_uplines(db, user, max_levels=3):
    uplines = []
    current = user
    level = 1
    while level <= max_levels and getattr(current, 'referrer_id', None):
        upline = db.get(User, current.referrer_id)
        if not upline:
            break
        uplines.append((level, upline))
        current = upline
        level += 1
    return uplines

def verify_telegram_init_data(init_data: str):
    if not init_data:
        return None, None, None, None
    try:
        data = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None, None, None, None
    user_str = data.get("user")
    if not user_str:
        return None, None, None, None
    try:
        user = json.loads(user_str)
    except Exception:
        return None, None, None, None
    start_param = data.get("start_param")
    return user.get("id"), user.get("username"), user.get("first_name"), start_param

# -------------------------
# Business helpers
# -------------------------

def require_admin(user):
    return user and user.role in ("admin", "superadmin")

def update_rank(user: User):
    total = user.total_team_business or 0.0
    active_origins = user.active_origin_count or 0

    if total >= 100000:
        user.role = "creator"
    elif total >= 25000:
        user.role = "visionary"
    elif total >= 5000:
        user.role = "advisor"
    elif total >= 1000 and active_origins >= 10:
        user.role = "life_changer"
    elif user.self_activated and user.role == "user":
        user.role = "origin"

ROLE_LEVEL1_PCT = {
    "origin": 0.05,
    "life_changer": 0.10,
    "advisor": 0.15,
    "visionary": 0.20,
    "creator": 0.25,
}

def deduct_wallet_balance(user: User, amount: float):
    musd_cut = round(amount * 0.70, 2)
    mstc_cut = round(amount * 0.30, 2)

    if (user.balance_musd or 0) < musd_cut:
        raise ValueError("Insufficient MUSD balance")

    if (user.balance_mstc or 0) < mstc_cut:
        raise ValueError("Insufficient MSTC balance")

    user.balance_musd -= musd_cut
    user.balance_mstc -= mstc_cut

    return musd_cut, mstc_cut

def propagate_team_business(db: SessionLocal, user: User, amount: float, became_origin_now: bool):
    visited = set()
    current = user
    while getattr(current, 'referrer_id', None) and current.referrer_id not in visited:
        ref = db.get(User, current.referrer_id)
        if not ref:
            break
        visited.add(ref.id)
        ref.total_team_business = (ref.total_team_business or 0.0) + amount
        if became_origin_now:
            ref.active_origin_count = (ref.active_origin_count or 0) + 1
        update_rank(ref)
        db.add(ref)
        current = ref

def distribute_club_bonus(db: SessionLocal, amount: float) -> float:
    club_cut = round(amount * 0.02, 2)
    if club_cut <= 0:
        return 0.0

    achievers = (
        db.query(User)
        .filter(
            User.self_activated == True,
            User.role.in_(["life_changer", "advisor", "visionary", "creator"])
        )
        .all()
    )

    # 🔴 No achievers → goes to company pool
    if not achievers:
        add_to_company_pool(db, club_cut, commit=True)   # 🔥 COMMIT REQUIRED
        return club_cut

    per_user = round(club_cut / len(achievers), 2)
    distributed_total = 0.0

    for u in achievers:
        u.club_income = float(u.club_income or 0.0) + per_user
        db.add(u)
        distributed_total += per_user

    leftover = round(club_cut - distributed_total, 2)
    if leftover > 0:
        add_to_company_pool(db, leftover, commit=True)   # 🔥 COMMIT REQUIRED

    return club_cut

COMPANY_USER_ID = -999999999

def get_company_user(db: SessionLocal) -> User:
    company = db.get(User, COMPANY_USER_ID)
    if not company:
        company = User(
            id=COMPANY_USER_ID,
            username="company_pool",
            first_name="Company",
            role="company",
            self_activated=False,
            created_at=datetime.utcnow(),
            balance_musd=0.0,
            balance_mstc=0.0,
        )
        db.add(company)
        db.commit()
        db.refresh(company)
    return company

def add_to_company_pool(db: SessionLocal, amount: float, *, commit: bool = False):
    amount = float(amount or 0.0)
    if amount <= 0:
        return
    company = get_company_user(db)
    company.balance_musd = float(company.balance_musd or 0.0) + amount
    db.add(company)
    if commit:
        db.commit()
        db.refresh(company)

# -------------------------
# Routes
# -------------------------

DEPOSIT_API_KEY = os.getenv("DEPOSIT_API_KEY")

@app.route("/", methods=["GET"])
def home():
    return "Backend OK", 200

@app.route("/webapp/me", methods=["POST"])
def webapp_me():
    payload = request.get_json(silent=True) or {}
    init_data = payload.get("initData")

    telegram_id, _, _, _ = verify_telegram_init_data(init_data)
    if not telegram_id:
        return jsonify(ok=False, error="invalid_init_data"), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == telegram_id).first()
        if not user:
            return jsonify(ok=False, not_registered=True)

        return jsonify(
            ok=True,
            user={
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "role": user.role,
                "balance_mstc": float(user.balance_mstc or 0),
                "balance_musd": float(user.balance_musd or 0),
                "referrer_id": user.referrer_id,
            }
        )

    
    finally:
        db.close()

@app.route("/webapp/init", methods=["POST"])
def webapp_init():
    data = request.get_json(silent=True) or {}

    init_data = data.get("initData")
    fallback_user_id = data.get("user_id")

    telegram_id = None
    username = None
    first_name = None
    start_param = None

    # -----------------------------
    # 1️⃣ Try FULL Telegram verification
    # -----------------------------
    if init_data:
        try:
            telegram_id, username, first_name, start_param = (
                verify_telegram_init_data(init_data)
            )
        except Exception as e:
            app.logger.warning("Telegram init verify failed: %s", e)

    # -----------------------------
    # 2️⃣ SAFE FALLBACK (Mini App only)
    # -----------------------------
    if not telegram_id:
        if fallback_user_id:
            telegram_id = int(fallback_user_id)
            first_name = first_name or "Telegram User"
            username = username or None
            app.logger.warning(
                "⚠️ FALLBACK INIT used for telegram_id=%s", telegram_id
            )
        else:
            return jsonify(ok=False, error="invalid_telegram_user"), 400

    # -----------------------------
    # 3️⃣ Database logic
    # -----------------------------
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == telegram_id).first()

        if user:
            return jsonify(
                ok=True,
                exists=True,
                user={
                    "id": user.id,
                    "first_name": user.first_name,
                    "username": user.username,
                    "role": user.role,
                    "self_activated": user.self_activated,
                    "total_team_business": float(user.total_team_business or 0),
                    "active_origin_count": int(user.active_origin_count or 0),
                    "referrer_id": user.referrer_id,
                }
            )

        # New user
        referrer_id = None
        if start_param and str(start_param).isdigit():
            referrer_id = int(start_param)

        user = User(
            id=telegram_id,
            first_name=first_name,
            username=username,
            referrer_id=referrer_id,
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        return jsonify(
            ok=True,
            exists=False,
            user={
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "role": user.role,
                "self_activated": False,
                "total_team_business": 0,
                "active_origin_count": 0,
                "referrer_id": user.referrer_id,
            },
        )

    finally:
        db.close()

@app.route("/webapp/user", methods=["POST"])
def webapp_user():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData")

    telegram_id, _, _, _ = verify_telegram_init_data(init_data)
    if not telegram_id:
        return jsonify(ok=False, error="invalid_init_data"), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == telegram_id).first()
        if not user:
            return jsonify(ok=False, error="user_not_found"), 404

        admin_ids = os.getenv("ADMIN_TELEGRAM_IDS", "")
        admin_set = {int(x) for x in admin_ids.split(",") if x.strip().isdigit()}

        return jsonify(
            ok=True,
            user={
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "role": user.role,
                "self_activated": bool(user.self_activated),
                "total_team_business": float(user.total_team_business or 0),
                "active_origin_count": int(user.active_origin_count or 0),
                "is_admin": telegram_id in admin_set,
            }
        )

    except OperationalError:
        # 👈 THIS is the ONLY place DB warm handling belongs
        return jsonify(ok=False, error="db_temp_unavailable"), 503

    finally:
        db.close()

@app.route("/admin/users", methods=["POST"])
def admin_users():
    
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData")

    if not init_data:
        return jsonify({
            "ok": False,
            "error": "missing_init_data"
        }), 400

    uid, _, _, _ = verify_telegram_init_data(init_data)
    if not uid:
        return jsonify({
            "ok": False,
            "error": "unauthorized"
        }), 401

   
    db = SessionLocal()
    try:
        admin_user = (
            db.query(User)
            .filter(User.id == uid)
            .first()
        )

        if not require_admin(admin_user):
            return jsonify({
                "ok": False,
                "error": "forbidden"
            }), 403

        users = (
            db.query(User)
            .order_by(User.created_at.desc())
            .limit(50)
            .all()
        )

        return jsonify({
            "ok": True,
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "role": u.role,
                    "balance_musd": float(u.balance_musd or 0),
                    "balance_mstc": float(u.balance_mstc or 0),
                    "active": bool(u.active)
                }
                for u in users
            ]
        })

    finally:
        db.close()

@app.route("/admin/update_user", methods=["POST"])
def admin_update_user():
    
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData")
    target_id = data.get("user_id")
    action = data.get("action")

    if not init_data or not target_id or not action:
        return jsonify({
            "ok": False,
            "error": "missing_params"
        }), 400

    admin_id, _, _, _ = verify_telegram_init_data(init_data)
    if not admin_id:
        return jsonify({
            "ok": False,
            "error": "unauthorized"
        }), 401

    
    db = SessionLocal()
    try:
        admin = (
            db.query(User)
            .filter(User.id == admin_id)
            .first()
        )

        if not admin or admin.role not in ("admin", "superadmin"):
            return jsonify({
                "ok": False,
                "error": "forbidden"
            }), 403

        user = (
            db.query(User)
            .filter(User.id == int(target_id))
            .first()
        )

        if not user:
            return jsonify({
                "ok": False,
                "error": "user_not_found"
            }), 404

        # -------- ACTIONS --------
        if action == "promote":
            user.role = "admin"

        elif action == "demote":
            user.role = "user"

        elif action == "activate":
            user.active = True

        elif action == "deactivate":
            user.active = False

        else:
            return jsonify({
                "ok": False,
                "error": "invalid_action"
            }), 400

        db.commit()

        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "role": user.role,
                "active": bool(user.active)
            }
        })

    finally:
        db.close()

@app.route("/admin/impersonate", methods=["POST"])
def admin_impersonate():
   
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        init_data = data.get("initData")
        target_id = data.get("user_id")

        if not init_data or not target_id:
            return jsonify({"ok": False}), 400

        admin_id, _, _, _ = verify_telegram_init_data(init_data)
        admin = db.query(User).filter(User.id == admin_id).first()

        if not admin or admin.role not in ("admin", "superadmin"):
            return jsonify({"ok": False, "error": "forbidden"}), 403

        target = db.query(User).filter(User.id == target_id).first()
        if not target or target.role in ("admin", "superadmin"):
            return jsonify({"ok": False, "error": "cannot_impersonate"}), 400

        return jsonify({
            "ok": True,
            "impersonated_user": {
                "id": target.id,
                "first_name": target.first_name,
                "username": target.username,
                "role": target.role
            }
        })

    except Exception:
        logger.exception("admin_impersonate failed")
        return jsonify({"ok": False}), 500
    finally:
        db.close()

@app.route("/admin/stats", methods=["POST"])
def admin_stats():
    
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData")

    if not init_data:
        return jsonify({
            "ok": False,
            "error": "missing_init_data"
        }), 400

    admin_id, _, _, _ = verify_telegram_init_data(init_data)
    if not admin_id:
        return jsonify({
            "ok": False,
            "error": "unauthorized"
        }), 401

    
    db = SessionLocal()
    try:
        admin = (
            db.query(User)
            .filter(User.id == admin_id)
            .first()
        )

        if not admin or not require_admin(admin):
            return jsonify({
                "ok": False,
                "error": "forbidden"
            }), 403

        # --------- STATS ----------
        total_users = db.query(User).count()

        active_users = (
            db.query(User)
            .filter(User.active.is_(True))
            .count()
        )

        admin_count = (
            db.query(User)
            .filter(User.role.in_(("admin", "superadmin")))
            .count()
        )

        total_team_business = (
            db.query(func.coalesce(func.sum(User.total_team_business), 0))
            .scalar()
        )

        total_musd_balance = (
            db.query(func.coalesce(func.sum(User.balance_musd), 0))
            .scalar()
        )

        today = datetime.utcnow().date()

        today_deposits = (
            db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(func.date(Transaction.created_at) == today)
            .scalar()
        )

        return jsonify({
            "ok": True,
            "stats": {
                "total_users": int(total_users),
                "active_users": int(active_users),
                "admin_count": int(admin_count),
                "total_team_business": float(total_team_business or 0),
                "total_musd_balance": float(total_musd_balance or 0),
                "today_deposits": float(today_deposits or 0),
            }
        })

    finally:
        db.close()

@app.route("/webapp/save_wallet", methods=["POST"])
def save_wallet():
    db = SessionLocal()
    try:
        data = request.get_json()
        init_data = data.get("initData")
        ton_wallet = data.get("ton_wallet")

        telegram_id, _, _, _ = verify_telegram_init_data(init_data)
        if not telegram_id:
            return jsonify({"ok": False, "error": "invalid_init_data"}), 400

        user = db.query(User).filter(User.id == telegram_id).first()
        if not user:
            return jsonify({"ok": False, "error": "user_not_found"}), 404

        user.ton_wallet = ton_wallet
        db.commit()

        return jsonify({"ok": True, "ton_wallet": ton_wallet})

    except Exception:
        app.logger.exception("save_wallet error")
        return jsonify({"ok": False, "error": "server_error"}), 500
    finally:
        db.close()

@app.post("/bot/start")
def bot_start():
    data = request.get_json(silent=True) or {}

    tg_id = data.get("telegram_id")
    first_name = data.get("first_name")

    if not tg_id:
        return jsonify({"ok": False, "error": "missing_telegram_id"}), 400

    db = SessionLocal()
    try:
        # 🔒 READ ONLY — NO CREATE HERE
        user = (
            db.query(User)
            .filter(User.id == int(tg_id))
            .first()
        )

        if user:
            message = f"Welcome back, {first_name or ''}! Tap below to continue."
            button_label = "Open Deposit Mini App"
        else:
            message = f"Welcome {first_name or ''}! Tap below to register."
            button_label = "Register / Open Mini App"

        webapp_url = (
            f"{os.getenv('BASE_URL', 'https://mstcbotnew-production.up.railway.app')}"
            "/static/telegram_mini_app.html"
        )

        return jsonify({
            "ok": True,
            "message": message,
            "button_label": button_label,
            "webapp_url": webapp_url,
        })

    finally:
        db.close()

@app.route("/webapp/profile", methods=["POST"])
def webapp_profile():
    
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        init_data = data.get("initData")

        uid, _, _, _ = verify_telegram_init_data(init_data)
        if not uid:
            return jsonify({"ok": False}), 401

        user = db.query(User).filter(User.id == uid).first()
        if not user:
            return jsonify({"ok": False}), 404

        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "role": user.role,
                "balance_mstc": float(user.balance_mstc),
                "balance_musd": float(user.balance_musd),
                "total_team_business": float(user.total_team_business),
                "active_origin_count": user.active_origin_count
            }
        })
    finally:
        db.close()

@app.route("/webapp/downlines", methods=["POST"])
def webapp_downlines():
    
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        init_data = data.get("initData")

        uid, _, _, _ = verify_telegram_init_data(init_data)
        if not uid:
            return jsonify({"ok": False}), 401

        downlines = db.query(User).filter(User.referrer_id == uid).all()

        return jsonify({
            "ok": True,
            "downlines": [
                {
                    "id": u.id,
                    "first_name": u.first_name,
                    "username": u.username,
                    "role": u.role,
                    "team_business": float(u.total_team_business)
                } for u in downlines
            ]
        })
    finally:
        db.close()

@app.route("/webapp/role", methods=["POST"])
def webapp_role():
    
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        init_data = data.get("initData")

        uid, _, _, _ = verify_telegram_init_data(init_data)
        if not uid:
            return jsonify({"ok": False}), 401

        user = db.query(User).filter(User.id == uid).first()
        if not user:
            return jsonify({"ok": False}), 404

        return jsonify({
            "ok": True,
            "role": user.role,
            "active_origin_count": user.active_origin_count,
            "total_team_business": float(user.total_team_business)
        })
    finally:
        db.close()

# -------------------------
# Debug / admin endpoints
# -------------------------

@app.route("/debug/downlines/<int:user_id>")
@debug_only
def debug_downlines(user_id):
  
  
  db = SessionLocal()
  try:
        user = (
            db.query(User)
            .filter(User.id == user_id)
            .first()
        )

        if not user:
            return jsonify({
                "ok": False,
                "error": "user_not_found"
            }), 404

        direct_downlines = (
            db.query(User)
            .filter(User.referrer_id == user_id)
            .all()
        )

        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "role": user.role,
                "self_activated": bool(user.self_activated),
                "referrer_id": user.referrer_id,
                "total_team_business": float(user.total_team_business or 0),
            },
            "direct_downlines": [
                {
                    "id": d.id,
                    "first_name": d.first_name,
                    "username": d.username,
                    "role": d.role,
                    "self_activated": bool(d.self_activated),
                    "referrer_id": d.referrer_id,
                    "total_team_business": float(d.total_team_business or 0),
                }
                for d in direct_downlines
            ],
            "direct_downline_count": len(direct_downlines),
        })

  finally:
        db.close()
@app.route("/debug/link_referrer", methods=["POST"])
@debug_only
def debug_link_referrer():
            
    data = request.get_json(silent=True) or {}

    try:
        user_id = int(data.get("user_id"))
        referrer_id = int(data.get("referrer_id"))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "invalid_ids"
        }), 400

    if user_id == referrer_id:
        return jsonify({
            "ok": False,
            "error": "cannot_self_refer"
        }), 400

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.id == user_id)
            .first()
        )

        referrer = (
            db.query(User)
            .filter(User.id == referrer_id)
            .first()
        )

        if not user or not referrer:
            return jsonify({
                "ok": False,
                "error": "user_or_referrer_not_found"
            }), 404

        # Prevent overwriting existing referrer
        if user.referrer_id is not None:
            return jsonify({
                "ok": False,
                "error": "referrer_already_set"
            }), 400

        user.referrer_id = referrer.id
        db.commit()

        return jsonify({
            "ok": True,
            "user_id": user.id,
            "referrer_id": referrer.id
        })

    except OperationalError:
        db.rollback()
        app.logger.warning("DB error during link_referrer")
        
    except Exception as e:
        db.rollback()
        app.logger.exception("Error in /debug/link_referrer")
        return jsonify({
            "ok": False,
            "error": "internal_error"
        }), 500

    finally:
        db.close()

@app.route("/debug/list_users", methods=["GET"])
@debug_only
def debug_list_users():
            
    db = SessionLocal()
    try:
        users = db.query(User).all()

        return jsonify(
            ok=True,
            users=[
                {
                    "id": u.id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "self_activated": bool(u.self_activated),
                    "referrer_id": u.referrer_id,
                    "total_team_business": float(u.total_team_business or 0),
                    "active_origin_count": int(u.active_origin_count or 0),
                    "role": u.role,
                }
                for u in users
            ],
        )

    except Exception:
        app.logger.exception("debug_list_users failed")
        return jsonify(ok=False, error="server_error"), 500
    finally:
        db.close()

@app.route("/debug/company_pool", methods=["GET"])
@debug_only
def debug_company_pool():
            
    db = SessionLocal()
    try:
        company = db.query(User).filter(User.id == COMPANY_USER_ID).first()

        if not company:
            return jsonify(
                ok=True,
                exists=False,
                balance_musd=0.0,
                balance_mstc=0.0,
                club_income=0.0,
            )

        return jsonify(
            ok=True,
            exists=True,
            user_id=company.id,
            username=company.username,
            role=company.role,
            balance_musd=float(company.balance_musd or 0),
            balance_mstc=float(company.balance_mstc or 0),
            club_income=float(getattr(company, "club_income", 0.0) or 0),
        )

    except Exception:
        app.logger.exception("debug_company_pool failed")
        return jsonify(ok=False, error="server_error"), 500
    finally:
        db.close()

# Single, canonical debug simulate_deposit implementation
@app.route("/debug/simulate_deposit", methods=["POST"])
@debug_only
def debug_simulate_deposit():

    payload = request.get_json(silent=True) or {}

    try:
        tg_id = int(payload.get("user_id"))
        amount_ton = float(payload.get("amount"))   # treat as TON-equivalent
        tx_hash = str(payload.get("tx_musd") or "DEBUG_TX")
    except Exception:
        return jsonify(ok=False, error="missing_user_or_amount"), 400

    if amount_ton <= 0:
        return jsonify(ok=False, error="invalid_amount"), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == tg_id).first()
        if not user:
            return jsonify(ok=False, error="user_not_found"), 404

        became_origin_now = False

        # 🔹 1️⃣ Activate user if eligible
        if amount_ton >= 20:
            if not user.self_activated:
                user.self_activated = True

            if user.role == "user":
                user.role = "origin"
                became_origin_now = True

        # 🔹 2️⃣ SPLIT TON → MUSD / MSTC
        musd_amount, mstc_amount = split_deposit_amount(amount_ton)

        user.balance_musd = float(user.balance_musd or 0) + musd_amount
        user.balance_mstc = float(user.balance_mstc or 0) + mstc_amount

        # 🔹 3️⃣ Business volume
        user.total_team_business = float(user.total_team_business or 0) + amount_ton
        db.add(user)

        # 🔹 4️⃣ Team propagation
        propagate_team_business(db, user, amount_ton, became_origin_now)

        # 🔹 5️⃣ Rank update
        update_rank(user)

        # 🔹 6️⃣ Club bonus (2%)
        club_cut = distribute_club_bonus(db, amount_ton)

        # 🔹 7️⃣ Transaction record (DEBUG)
        db.add(Transaction(
            user_id=user.id,
            amount=amount_ton,
            currency="TON",
            type="deposit",
            external_id=tx_hash,
            created_at=datetime.utcnow(),
        ))

        # 🔹 8️⃣ Commit
        db.commit()
        db.refresh(user)

        return jsonify(
            ok=True,
            credited={
                "musd": musd_amount,
                "mstc": mstc_amount
            },
            user={
                "id": user.id,
                "role": user.role,
                "balance_musd": float(user.balance_musd or 0),
                "balance_mstc": float(user.balance_mstc or 0),
            },
            club_cut=club_cut
        )

    except Exception:
        db.rollback()
        current_app.logger.exception("debug_simulate_deposit failed")
        return jsonify(ok=False, error="server_error"), 500

    finally:
        db.close()
 
@app.route("/debug/user/<int:user_id>")
@debug_only
def debug_user(user_id):
            
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify(ok=False, exists=False)

        return jsonify(
            ok=True,
            exists=True,
            user={
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "self_activated": bool(user.self_activated),
                "role": user.role,
                "referrer_id": user.referrer_id,
                "total_team_business": float(user.total_team_business or 0),
                "active_origin_count": int(user.active_origin_count or 0),
            },
        )
    finally:
        db.close()

@app.route("/debug/reset_user/<int:user_id>", methods=["POST"])
@debug_only
def debug_reset_user(user_id):
        
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify(ok=False, error="user_not_found"), 404

        db.query(ReferralEvent).filter(
            (ReferralEvent.from_user == user.id)
            | (ReferralEvent.to_user == user.id)
        ).delete(synchronize_session=False)

        db.query(Transaction).filter(
            Transaction.user_id == user.id
        ).delete(synchronize_session=False)

        user.balance_musd = 0.0
        user.balance_mstc = 0.0
        user.total_team_business = 0.0
        user.active_origin_count = 0
        user.self_activated = False
        user.referrer_id = None
        user.role = "user"

        db.commit()

        return jsonify(ok=True, user_id=user.id)

    except Exception:
        db.rollback()
        app.logger.exception("debug_reset_user failed")
        return jsonify(ok=False, error="server_error"), 500
    finally:
        db.close()

@app.route("/debug/transactions/<int:user_id>", methods=["GET"])
@debug_only
def debug_transactions(user_id):
            
    db = SessionLocal()
    try:
        txs = (
            db.query(Transaction)
            .filter(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc())
            .all()
        )

        return jsonify(
            ok=True,
            transactions=[
                {
                    "id": t.id,
                    "user_id": t.user_id,
                    "amount": float(t.amount or 0),
                    "currency": t.currency,
                    "type": t.type,
                    "external_id": t.external_id,
                    "created_at": t.created_at.isoformat(),
                }
                for t in txs
            ],
        )
    finally:
        db.close()

@app.route("/deposit/submit", methods=["POST"])
def deposit_submit():
    data = request.get_json(silent=True) or {}

    try:
        user_id = int(data["user_id"])
        amount_ton = float(data["amount"])   # TON sent on-chain
        tx_hash = str(data["tx_hash"]).strip()
    except Exception:
        return jsonify(ok=False, error="invalid_payload"), 400

    if amount_ton <= 0 or not tx_hash:
        return jsonify(ok=False, error="invalid_amount_or_tx"), 400

    db = SessionLocal()
    try:
        # 1️⃣ user exists
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify(ok=False, error="user_not_found"), 404

        # 2️⃣ prevent duplicate tx
        exists = (
            db.query(Transaction)
            .filter(Transaction.external_id == tx_hash)
            .first()
        )
        if exists:
            return jsonify(ok=False, error="tx_already_processed"), 409

        # 3️⃣ store TX as PENDING (NO VERIFICATION HERE)
        tx = Transaction(
            user_id=user.id,
            amount=amount_ton,
            currency="TON",
            type="deposit",
            external_id=tx_hash,
            status="pending",          # 🔑 IMPORTANT
            created_at=datetime.utcnow()
        )
        db.add(tx)
        db.commit()

        # 4️⃣ respond immediately
        return jsonify(
            ok=True,
            status="pending",
            message="Transaction received. Verification in progress."
        )

    except Exception:
        db.rollback()
        current_app.logger.exception("deposit_submit failed")
        return jsonify(ok=False, error="server_error"), 500
    finally:
        db.close()
 
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True)
    app.logger.info("Webhook update: %s", update)

    if not update:
        return jsonify(ok=False), 400

    # Respond immediately to Telegram
    response = jsonify(ok=True)

    try:
        from backend.telegram_bot import handle_command
        handle_command(update)
    except Exception:
        app.logger.exception("handle_command failed")

    return response, 200
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=DEBUG_MODE)



