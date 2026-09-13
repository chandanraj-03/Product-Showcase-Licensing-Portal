"""
User Profile and Authentication Registry for PrivCloud backed by Supabase.
Handles persistent user metadata, username uniqueness validation,
email mapping, secure reset tokens, and dynamic OTP verification.
"""

import re
import time
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, Any, Tuple
import httpx

from backend.config import (
    SUPABASE_URL,
    SUPABASE_KEY,
    SUPABASE_SERVICE_ROLE_KEY,
    get_supabase_headers,
    RESEND_API_KEY,
    RESEND_FROM,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASS,
    SMTP_FROM
)

ACTIVE_SUPABASE_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY
TABLE_NAME = "Users"

# Dynamic In-Memory Registries (Fallback & Fast Cache)
_ACTIVE_OTPS: Dict[str, Dict[str, Any]] = {}
_ACTIVE_RESET_TOKENS: Dict[str, Dict[str, Any]] = {}
_PENDING_PROFILES: Dict[str, Dict[str, Any]] = {}
_USERS_CACHE: Dict[str, Tuple[float, Any]] = {}
_AUTH_USERS_CACHE: Optional[Tuple[float, list]] = None

CACHE_TTL = 60.0  # 60 seconds TTL
OTP_EXPIRY_SECONDS = 600  # 10 minutes
RESET_TOKEN_EXPIRY_SECONDS = 900  # 15 minutes
MAX_VERIFY_ATTEMPTS = 5


async def _supabase_query_async(params: Dict[str, str]) -> list:
    """Execute an async GET query against Supabase Users table."""
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return []
    
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE_NAME}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=get_supabase_headers(), params=params)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[PrivCloud Supabase] Async query exception: {e}")
    return []


def _supabase_query(params: Dict[str, str]) -> list:
    """Synchronous fallback GET query against Supabase Users table."""
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE_NAME}"
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, headers=get_supabase_headers(), params=params)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[PrivCloud Supabase] Sync query exception: {e}")
    return []


async def _get_auth_admin_users_cached() -> list:
    """Fetch all users from Supabase Auth Admin API with 60-second TTL cache (P-1 optimization)."""
    global _AUTH_USERS_CACHE
    now = time.time()
    if _AUTH_USERS_CACHE and (now - _AUTH_USERS_CACHE[0] < CACHE_TTL):
        return _AUTH_USERS_CACHE[1]

    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return []

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users",
                headers=get_supabase_headers()
            )
            if r.status_code == 200:
                data = r.json()
                users = data.get("users", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                _AUTH_USERS_CACHE = (now, users)
                return users
    except Exception as e:
        print(f"[PrivCloud Supabase] Error fetching admin users: {e}")
    return []


def validate_username_format(username: str) -> Tuple[bool, str]:
    """
    Validate username format:
    - 3 to 30 characters
    - Alphanumeric, underscores, and dots
    - Cannot start or end with a dot or underscore
    - No consecutive dots or underscores
    """
    if not username:
        return False, "Username cannot be empty."
    username = username.strip()
    if len(username) < 3:
        return False, "Username must be at least 3 characters long."
    if len(username) > 30:
        return False, "Username cannot exceed 30 characters."
    if not re.match(r'^[a-zA-Z0-9._]+$', username):
        return False, "Username can only contain letters, numbers, underscores (_), and periods (.)."
    if username.startswith('.') or username.startswith('_') or username.endswith('.') or username.endswith('_'):
        return False, "Username cannot begin or end with a period or underscore."
    if '..' in username or '__' in username or '._' in username or '_.' in username:
        return False, "Username cannot contain consecutive symbols."
    return True, "Valid username format."


async def is_username_available(username: str) -> bool:
    """Check if a username is available in Supabase Users table, Auth metadata, or pending registrations (case-insensitive)."""
    key = username.strip().lower()
    
    # 1. Check TTL cache
    cached = _USERS_CACHE.get(f"uname:{key}")
    if cached and (time.time() - cached[0] < CACHE_TTL):
        return cached[1]

    # 2. Check Supabase public.Users table
    rows = await _supabase_query_async({"username": f"ilike.{key}", "select": "id,username"})
    if rows and len(rows) > 0:
        _USERS_CACHE[f"uname:{key}"] = (time.time(), False)
        return False
        
    # 3. Check Supabase auth.users metadata (via cached admin list)
    admin_users = await _get_auth_admin_users_cached()
    for u in admin_users:
        meta = u.get("user_metadata", {})
        uname = meta.get("username")
        if uname and uname.strip().lower() == key:
            _USERS_CACHE[f"uname:{key}"] = (time.time(), False)
            return False

    # 4. Check in-memory pending registrations awaiting OTP
    for p_email, p_data in _PENDING_PROFILES.items():
        p_uname = p_data.get("username")
        if p_uname and p_uname.strip().lower() == key:
            _USERS_CACHE[f"uname:{key}"] = (time.time(), False)
            return False
            
    _USERS_CACHE[f"uname:{key}"] = (time.time(), True)
    return True


async def _get_taken_usernames_set() -> set:
    """Fetch all taken usernames from Supabase using cached records."""
    taken = set()
    rows = await _supabase_query_async({"select": "username"})
    for r in rows:
        u = r.get("username")
        if u:
            taken.add(u.strip().lower())
            
    admin_users = await _get_auth_admin_users_cached()
    for u in admin_users:
        meta = u.get("user_metadata", {})
        uname = meta.get("username")
        if uname:
            taken.add(uname.strip().lower())
            
    return taken


async def generate_username_suggestions(base_username: str, full_name: Optional[str] = None, limit: int = 4) -> list:
    """
    Generate smart, clean username suggestions based on user's full name and base username.
    Checks candidates against Supabase in a single fast batch to guarantee availability.
    """
    first = ""
    last = ""
    if full_name:
        parts = [p.lower() for p in re.findall(r'[a-zA-Z0-9]+', full_name)]
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
        elif len(parts) == 1:
            first = parts[0]
            
    base = re.sub(r'[^a-zA-Z0-9_.]', '', base_username.strip().lower())
    taken = await _get_taken_usernames_set()
    
    candidates = []
    if first and last:
        candidates.extend([
            f"{first}.{last}",
            f"{first}_{last}",
            f"{first}{last}",
            f"{first[0]}.{last}",
            f"{first[0]}_{last}",
            f"{first}_{last}_{secrets.randbelow(90) + 10}",
            f"{first}.{last}_{secrets.randbelow(90) + 10}"
        ])
    elif first:
        candidates.extend([
            f"{first}_{secrets.randbelow(900) + 100}",
            f"{first}.{secrets.randbelow(900) + 100}"
        ])
        
    if base:
        candidates.extend([
            f"{base}_{secrets.randbelow(900) + 100}",
            f"{base}.{secrets.randbelow(900) + 100}",
            f"{base}_{secrets.randbelow(90) + 10}",
            f"the.{base}",
            f"{base}.cloud"
        ])
        
    seen = set()
    valid_available_suggestions = []
    
    for cand in candidates:
        pat = cand.lower().strip()
        if pat in seen or pat in taken:
            continue
        seen.add(pat)
        
        is_valid, _ = validate_username_format(pat)
        if is_valid:
            valid_available_suggestions.append(pat)
            if len(valid_available_suggestions) >= limit:
                break
                
    while len(valid_available_suggestions) < limit:
        seed = base or first or "user"
        cand = f"{seed}_{secrets.randbelow(9000) + 1000}"
        if cand not in seen and cand not in taken:
            seen.add(cand)
            valid_available_suggestions.append(cand)
                
    return valid_available_suggestions[:limit]


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Retrieve user record from Supabase by username, enriched with full_name."""
    key = username.strip().lower()
    user_record = None
    rows = await _supabase_query_async({"username": f"ilike.{key}", "select": "*"})
    if rows and len(rows) > 0:
        user_record = dict(rows[0])
        
    # Check Supabase auth users to enrich with full_name
    admin_users = await _get_auth_admin_users_cached()
    for u in admin_users:
        meta = u.get("user_metadata", {})
        if (meta.get("username") or "").lower() == key or (user_record and (u.get("email") or "").lower() == (user_record.get("email") or "").lower()):
            full_name = meta.get("full_name") or meta.get("name")
            if user_record:
                user_record["full_name"] = full_name
                return user_record
            return {
                "id": u.get("id"),
                "email": u.get("email"),
                "username": meta.get("username"),
                "full_name": full_name
            }
            
    return user_record


async def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve user record from Supabase by email, enriched with full_name."""
    key = email.strip().lower()
    user_record = None
    rows = await _supabase_query_async({"email": f"ilike.{key}", "select": "*"})
    if rows and len(rows) > 0:
        user_record = dict(rows[0])
        
    # Check Supabase auth users to enrich with full_name
    admin_users = await _get_auth_admin_users_cached()
    for u in admin_users:
        if (u.get("email") or "").lower() == key:
            meta = u.get("user_metadata", {})
            full_name = meta.get("full_name") or meta.get("name")
            if user_record:
                user_record["full_name"] = full_name
                return user_record
            return {
                "id": u.get("id"),
                "email": u.get("email"),
                "username": meta.get("username") or key.split('@')[0],
                "full_name": full_name
            }
            
    return user_record


async def resolve_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """
    Resolve an identifier (either email or username) to the user record in Supabase.
    """
    if not identifier:
        return None
    ident = identifier.strip()
    if '@' in ident:
        return await get_user_by_email(ident)
    return await get_user_by_username(ident)


async def sync_user_to_supabase_table(email: str, username: Optional[str] = None, full_name: Optional[str] = None, user_id: Optional[str] = None) -> bool:
    """
    Persist user data (id, email, username, version) into the Supabase 'Users' table.
    """
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return False

    e_key = email.strip().lower()
    target_id = user_id
    target_username = username
    target_full_name = full_name

    headers = get_supabase_headers(prefer="resolution=merge-duplicates,return=representation")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # If user_id or username missing, retrieve from Supabase Auth admin API
            if not target_id or not target_username:
                admin_users = await _get_auth_admin_users_cached()
                for u in admin_users:
                    if (u.get("email") or "").lower() == e_key:
                        target_id = target_id or u.get("id")
                        meta = u.get("user_metadata", {})
                        target_username = target_username or meta.get("username") or _PENDING_PROFILES.get(e_key, {}).get("username") or e_key.split('@')[0]
                        target_full_name = target_full_name or meta.get("full_name") or _PENDING_PROFILES.get(e_key, {}).get("full_name")
                        break

            # If still missing from auth (pending verification), use pending profile
            if not target_username and e_key in _PENDING_PROFILES:
                target_username = _PENDING_PROFILES[e_key].get("username")

            if target_id and target_username:
                payload = {
                    "id": target_id,
                    "email": e_key,
                    "username": target_username.strip(),
                    "version": "1.0.0"
                }
                r_post = await client.post(f"{SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE_NAME}", headers=headers, json=payload)
                if r_post.status_code in (200, 201):
                    print(f"[PrivCloud Supabase] Successfully persisted user in Users table: {e_key} -> @{target_username}")
                    # Invalidate cache
                    _USERS_CACHE.pop(f"uname:{target_username.lower()}", None)
                    return True
    except Exception as e:
        print(f"[PrivCloud Supabase] Exception syncing to Users table: {e}")

    return False


async def register_user(full_name: str, username: str, email: str, is_verified: bool = False, password: Optional[str] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Validate and prepare profile for Supabase Auth registration.
    Ensures uniqueness of both username and email.
    """
    valid_format, msg = validate_username_format(username)
    if not valid_format:
        return False, msg, None

    u_key = username.strip().lower()
    e_key = email.strip().lower()

    if not await is_username_available(u_key):
        return False, "This username is already taken. Please choose another.", None

    existing_email_user = await get_user_by_email(e_key)
    if existing_email_user:
        return False, "An account with this email address already exists. Please log in.", None

    _PENDING_PROFILES[e_key] = {
        "full_name": full_name.strip(),
        "username": u_key,
        "email": e_key,
        "created_at": time.time()
    }

    record = {
        "full_name": full_name.strip(),
        "username": u_key,
        "email": e_key,
        "is_verified": is_verified
    }

    return True, "Profile registered successfully.", record


async def mark_user_verified(email: str, username: Optional[str] = None) -> bool:
    """Mark user account as verified and persist in Supabase Users table."""
    e_key = email.strip().lower()
    u_name = username or _PENDING_PROFILES.get(e_key, {}).get("username")
    f_name = _PENDING_PROFILES.get(e_key, {}).get("full_name")
    
    return await sync_user_to_supabase_table(email=e_key, username=u_name, full_name=f_name)


# ----------------------------------------------------
# Dynamic Cryptographic OTP & Password Reset Engine
# ----------------------------------------------------
async def _dispatch_otp_email_async(email: str, code: str) -> None:
    """Send OTP via Resend API or SMTP asynchronously."""
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 500px; margin: 0 auto; padding: 28px; background: #ffffff; border: 1px solid #eaeaea; border-radius: 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        <div style="text-align: center; margin-bottom: 20px;">
            <h2 style="color: #0f172a; margin: 0; font-size: 22px;">PrivCloud Verification</h2>
            <p style="color: #64748b; font-size: 14px; margin-top: 6px;">Sign in or complete account registration</p>
        </div>
        <p style="color: #334155; font-size: 15px; margin-bottom: 20px;">Your 6-digit confirmation code is:</p>
        <div style="background: #f8fafc; border: 1px dashed #cbd5e1; padding: 18px; text-align: center; border-radius: 10px; font-size: 34px; font-weight: 700; letter-spacing: 8px; color: #0284c7; font-family: monospace;">
            {code}
        </div>
        <p style="color: #94a3b8; font-size: 13px; margin-top: 24px; margin-bottom: 0;">This code is valid for 10 minutes and can only be used once. If you did not request this, please ignore this email.</p>
    </div>
    """

    if RESEND_API_KEY:
        try:
            resend_url = "https://api.resend.com/emails"
            resend_payload = {
                "from": RESEND_FROM,
                "to": [email],
                "subject": f"Your PrivCloud Verification Code: {code}",
                "html": html_content
            }
            resend_headers = {
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(resend_url, headers=resend_headers, json=resend_payload)
        except Exception as e:
            print(f"[PrivCloud Resend] Resend dispatch exception: {e}")

    elif SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Your PrivCloud Verification Code: {code}"
            msg["From"] = SMTP_FROM
            msg["To"] = email
            msg.attach(MIMEText(html_content, "html"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8.0) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [email], msg.as_string())
        except Exception as e:
            print(f"[PrivCloud Mailer] SMTP delivery exception: {e}")


async def generate_user_otp(email: str, purpose: str = 'signup') -> str:
    """
    Generate a cryptographically random 6-digit numeric OTP,
    persist it to memory with automatic TTL expiry, and dispatch.
    """
    e_key = email.strip().lower()
    code = f"{secrets.randbelow(900000) + 100000}"
    now = time.time()

    # In-memory storage with automatic TTL expiry
    _ACTIVE_OTPS[e_key] = {
        "code": code,
        "expires_at": now + OTP_EXPIRY_SECONDS,
        "attempts": 0,
        "purpose": purpose,
        "created_at": now
    }

    await _dispatch_otp_email_async(e_key, code)
    return code


async def verify_user_otp(email: str, code: str, otp_type: str = "signup") -> Tuple[bool, str, Optional[str]]:
    """
    Verify the dynamic 6-digit OTP for the specified email.
    If verifying for reset/recovery, generates a secure single-use reset_token (S-2).
    """
    if not code or len(code.strip()) != 6 or not code.strip().isdigit():
        return False, "OTP code must be exactly 6 digits.", None
    
    e_key = email.strip().lower()
    clean_input_code = code.strip()

    # Check in-memory store
    otp_record = _ACTIVE_OTPS.get(e_key)
    if not otp_record:
        return False, "No active confirmation code found for this email. Please request a new one.", None
        
    now = time.time()
    if now > otp_record.get("expires_at", 0):
        _ACTIVE_OTPS.pop(e_key, None)
        return False, "Confirmation code has expired. Please request a new code.", None
    
    if otp_record.get("attempts", 0) >= MAX_VERIFY_ATTEMPTS:
        _ACTIVE_OTPS.pop(e_key, None)
        return False, "Too many invalid attempts. Please request a new confirmation code.", None
    
    stored_code = otp_record.get("code")
    if secrets.compare_digest(clean_input_code, stored_code):
        _ACTIVE_OTPS.pop(e_key, None)
        verified = True
    else:
        otp_record["attempts"] = otp_record.get("attempts", 0) + 1
        remaining = MAX_VERIFY_ATTEMPTS - otp_record["attempts"]
        if remaining <= 0:
            _ACTIVE_OTPS.pop(e_key, None)
            return False, "Too many invalid attempts. Please request a new confirmation code.", None
        return False, f"Invalid confirmation code. {remaining} attempt(s) remaining.", None

    if verified:
        await mark_user_verified(email=e_key)
        
        # If reset flow, generate and store a secure single-use reset token
        reset_token = None
        if otp_type in ("reset", "recovery"):
            reset_token = secrets.token_urlsafe(32)
            _ACTIVE_RESET_TOKENS[e_key] = {
                "token": reset_token,
                "expires_at": time.time() + RESET_TOKEN_EXPIRY_SECONDS
            }
        return True, "Code verified successfully.", reset_token

    return False, "Invalid confirmation code.", None


def validate_and_consume_reset_token(email: str, token: str) -> bool:
    """Validate and immediately consume a single-use password reset token (S-2 protection)."""
    if not email or not token:
        return False
    e_key = email.strip().lower()
    record = _ACTIVE_RESET_TOKENS.get(e_key)
    if not record:
        return False
    if time.time() > record.get("expires_at", 0):
        _ACTIVE_RESET_TOKENS.pop(e_key, None)
        return False
    if secrets.compare_digest(token.strip(), record.get("token", "")):
        _ACTIVE_RESET_TOKENS.pop(e_key, None)
        return True
    return False


async def verify_supabase_user_token(token: str, expected_email: str) -> bool:
    """Validate a Supabase user access token against Supabase Auth API."""
    if not SUPABASE_URL or not token or not expected_email:
        return False
    try:
        url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/user"
        headers = {
            "apikey": ACTIVE_SUPABASE_KEY or SUPABASE_KEY,
            "Authorization": f"Bearer {token}"
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                u_email = (resp.json().get("email") or "").strip().lower()
                return u_email == expected_email.strip().lower()
    except Exception as e:
        print(f"[PrivCloud Auth] Token verification notice: {e}")
    return False


async def update_user_password_admin(email: str, new_password: str) -> Tuple[bool, str]:
    """
    Update a user's password using Supabase Auth Admin API with Service Role Key.
    """
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return False, "Database connection not configured."
    
    target_email = (email or "").strip().lower()
    if not target_email:
        return False, "Email cannot be empty."
    
    if not new_password or len(new_password) < 6:
        return False, "Password must be at least 6 characters long."
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            user_id = None

            # 1. Fetch user ID from Supabase Admin API (cached)
            admin_users = await _get_auth_admin_users_cached()
            for u in admin_users:
                if (u.get("email") or "").strip().lower() == target_email:
                    user_id = u.get("id")
                    break

            # 2. Fallback: check public.Users table
            if not user_id:
                db_rows = await _supabase_query_async({"email": f"ilike.{target_email}", "select": "id,email"})
                if db_rows and len(db_rows) > 0:
                    user_id = db_rows[0].get("id")

            if not user_id:
                return False, f"No account found associated with '{target_email}'."

            # 3. Update user password via Supabase Admin API
            put_resp = await client.put(
                f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{user_id}",
                headers=get_supabase_headers(),
                json={"password": new_password}
            )

            if put_resp.status_code in (200, 204):
                print(f"[PrivCloud Auth] Password successfully updated for {target_email} (User ID: {user_id}).")
                await trigger_supabase_invite_async(target_email)
                return True, "Password has been successfully updated."
            else:
                err_msg = put_resp.text
                try:
                    err_json = put_resp.json()
                    err_msg = err_json.get("msg") or err_json.get("message") or err_json.get("error_description") or err_msg
                except Exception:
                    pass
                return False, f"Failed to update password: {err_msg}"
    except Exception as e:
        print(f"[PrivCloud Auth] Exception during password update: {e}")
        return False, f"Password update failed: {str(e)}"


async def trigger_supabase_invite_async(email: str) -> bool:
    """Send a password change security notification or trigger invite."""
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY or not email:
        return False

    target_email = email.strip().lower()
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{SUPABASE_URL.rstrip('/')}/auth/v1/invite",
                headers=get_supabase_headers(),
                json={"email": target_email}
            )
            if resp.status_code in (200, 201):
                print(f"[PrivCloud Auth] Supabase invite email ('Your Privcloud Password was changed') dispatched to {target_email}.")
                return True
            else:
                print(f"[PrivCloud Auth] Supabase invite notice for {target_email}: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[PrivCloud Auth] Exception dispatching Supabase invite for {target_email}: {e}")

    _dispatch_password_change_notice(target_email)
    return False


async def trigger_supabase_account_ready(email: str, username: Optional[str] = None) -> bool:
    """
    Trigger Supabase 'Magic link or OTP' email template:
    'Welcome to PrivCloud — Your Account Is Ready!'
    Called when a user finishes signup OTP verification and their account is ready.
    """
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY or not email:
        return False

    clean_email = email.strip().lower()
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{SUPABASE_URL.rstrip('/')}/auth/v1/otp",
                headers=get_supabase_headers(),
                json={"email": clean_email, "create_user": False}
            )
            if resp.status_code in (200, 204):
                print(f"[PrivCloud Supabase] Account ready email ('Welcome to PrivCloud — Your Account Is Ready!') dispatched to {clean_email}.")
                return True
            else:
                print(f"[PrivCloud Supabase] Account ready email notice for {clean_email}: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[PrivCloud Supabase] Exception dispatching account ready email: {e}")
    return False


def _dispatch_password_change_notice(email: str) -> None:
    """Send a password change security notification email."""
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 500px; margin: 0 auto; padding: 28px; background: #ffffff; border: 1px solid #eaeaea; border-radius: 14px;">
        <div style="text-align: center; margin-bottom: 20px;">
            <h2 style="color: #0f172a; margin: 0; font-size: 22px;">Password Changed</h2>
            <p style="color: #64748b; font-size: 14px; margin-top: 6px;">PrivCloud Security Notice</p>
        </div>
        <p style="color: #334155; font-size: 15px; margin-bottom: 16px;">The password for your PrivCloud account (<strong>{email}</strong>) was recently updated.</p>
        <p style="color: #64748b; font-size: 13px;">If you performed this change, no further action is required. If you did not make this change, please contact support immediately.</p>
    </div>
    """
    if RESEND_API_KEY:
        try:
            with httpx.Client(timeout=8.0) as client:
                client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={"from": RESEND_FROM, "to": [email], "subject": "Security Notice: Your PrivCloud Password Was Updated", "html": html_content}
                )
        except Exception:
            pass
    elif SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "Security Notice: Your PrivCloud Password Was Updated"
            msg["From"] = SMTP_FROM
            msg["To"] = email
            msg.attach(MIMEText(html_content, "html"))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8.0) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [email], msg.as_string())
        except Exception:
            pass


def trigger_supabase_reauthentication(
    email: str,
    payment_details: Optional[Dict[str, Any]] = None,
    bearer_token: Optional[str] = None
) -> bool:
    """
    Dispatch official payment confirmation email to the customer using Supabase Auth
    Reauthentication email template: 'PrivCloud Payment Confirmation — {{ .TransactionID }}'.
    Populates dynamic template variables including {{ .TransactionID }} in Supabase Auth user_metadata.
    """
    if not email:
        return False

    clean_email = email.strip().lower()
    tx_id = "CONFIRMED"
    if payment_details:
        tx_id = str(payment_details.get("TransactionID") or payment_details.get("transaction_id") or "CONFIRMED").strip()

    # 1. Update user_metadata in Supabase Auth with TransactionID so {{ .TransactionID }} is rendered
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            headers = get_supabase_headers()
            with httpx.Client(timeout=10.0) as client:
                admin_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users"
                r_users = client.get(admin_url, headers=headers)
                if r_users.status_code == 200:
                    data = r_users.json()
                    users_list = data.get("users", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    target_user = None
                    for u in users_list:
                        if (u.get("email") or "").strip().lower() == clean_email:
                            target_user = u
                            break

                    if target_user:
                        user_id = target_user.get("id")
                        existing_meta = dict(target_user.get("user_metadata") or {})
                        
                        # Populate template dynamic variables
                        existing_meta["TransactionID"] = tx_id
                        existing_meta["transaction_id"] = tx_id
                        if payment_details:
                            for k, v in payment_details.items():
                                if v is not None:
                                    existing_meta[k] = v

                        # Update user_metadata
                        client.put(
                            f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{user_id}",
                            headers=headers,
                            json={"user_metadata": existing_meta}
                        )
        except Exception as meta_err:
            print(f"[PrivCloud Supabase] Notice updating metadata for reauthentication: {meta_err}")

    # 2. Trigger Supabase Reauthentication email via GET /auth/v1/reauthenticate
    reauth_triggered = False
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            token = bearer_token
            # If no bearer_token supplied, generate a session token via admin generate_link & verify
            if not token:
                with httpx.Client(timeout=10.0) as client:
                    gl_res = client.post(
                        f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/generate_link",
                        headers=get_supabase_headers(),
                        json={"type": "magiclink", "email": clean_email}
                    )
                    if gl_res.status_code == 200:
                        otp_code = gl_res.json().get("email_otp")
                        if otp_code:
                            vt_res = client.post(
                                f"{SUPABASE_URL.rstrip('/')}/auth/v1/verify",
                                headers={"apikey": SUPABASE_KEY or ACTIVE_SUPABASE_KEY},
                                json={"type": "magiclink", "token": otp_code, "email": clean_email}
                            )
                            if vt_res.status_code == 200:
                                token = vt_res.json().get("access_token")

            if token:
                with httpx.Client(timeout=25.0) as client:
                    reauth_res = client.get(
                        f"{SUPABASE_URL.rstrip('/')}/auth/v1/reauthenticate",
                        headers={
                            "apikey": SUPABASE_KEY or ACTIVE_SUPABASE_KEY,
                            "Authorization": f"Bearer {token}"
                        }
                    )
                    if reauth_res.status_code in (200, 204):
                        print(f"[PrivCloud Supabase] Reauthentication email ('PrivCloud Payment Confirmation — {tx_id}') successfully triggered for {clean_email}.")
                        reauth_triggered = True
                    else:
                        print(f"[PrivCloud Supabase] Reauthentication notice for {clean_email}: {reauth_res.status_code} - {reauth_res.text}")
        except Exception as reauth_err:
            print(f"[PrivCloud Supabase] Reauthentication dispatch exception for {clean_email}: {reauth_err}")

    # 3. Direct receipt delivery fallback
    if payment_details:
        try:
            dispatch_direct_payment_receipt(clean_email, payment_details)
        except Exception:
            pass

    return reauth_triggered


async def update_user_plan_tier(email: str, tier: str) -> bool:
    """
    Update a user's plan_tier in Supabase user_metadata without overwriting existing metadata.
    Also syncs to the public Users table if present.
    """
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY or not email:
        return False

    target_email = email.strip().lower()
    tier_normalized = tier.strip().upper()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            admin_users = await _get_auth_admin_users_cached()
            for u in admin_users:
                if (u.get("email") or "").strip().lower() == target_email:
                    user_id = u.get("id")
                    existing_meta = dict(u.get("user_metadata") or {})
                    existing_meta["plan_tier"] = tier_normalized
                    existing_meta["plan"] = tier_normalized

                    put_resp = await client.put(
                        f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{user_id}",
                        headers=get_supabase_headers(),
                        json={"user_metadata": existing_meta}
                    )
                    if put_resp.status_code in (200, 204):
                        print(f"[PrivCloud Supabase] Successfully updated user_metadata.plan_tier to '{tier_normalized}' for {target_email}.")
                        # Invalidate admin user cache
                        global _AUTH_USERS_CACHE
                        _AUTH_USERS_CACHE = None
                        return True
                    break
    except Exception as e:
        print(f"[PrivCloud Supabase] Exception updating user plan_tier: {e}")

    return False


def dispatch_direct_payment_receipt(email: str, details: Dict[str, Any]) -> None:
    """
    Directly deliver the payment confirmation receipt email if SMTP or Resend is available.
    Supports both standardized snake_case and TitleCase keys seamlessly (M-5).
    """
    def _val(k1: str, k2: Optional[str] = None, default: str = "N/A") -> str:
        v = details.get(k1)
        if (v is None or not str(v).strip()) and k2:
            v = details.get(k2)
        if v is not None:
            s = str(v).strip()
            if s:
                return s
        return default

    clean_email = (email or details.get("user_email") or "").strip().lower()
    email_prefix = clean_email.split('@')[0] if '@' in clean_email else "Valued Customer"
    uname = _val("user_name", "UserName", default=_val("username", default=email_prefix))
    amt = _val("amount", "Amount", default="₹1,499")
    txid = _val("transaction_id", "TransactionID", default="Confirmed")
    oid = _val("order_id", "OrderID", default="Confirmed")
    pdate = _val("payment_date", "PaymentDate", default="Recently")
    pmethod = _val("payment_method", "PaymentMethod", default="Razorpay (UPI / Card / NetBanking)")
    pname = _val("product_name", "ProductName", default="PrivCloud Edition")
    bperiod = _val("billing_period", "BillingPeriod", default="Lifetime License (1 PC)")
    qty = _val("quantity", "Quantity", default="1 License")
    user_email = clean_email or _val("user_email", default=email_prefix)

    html = f"""
    <div style="margin:0;padding:0;width:100%;background-color:#f5f7fb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#18181b;">
      <div style="width:100%;padding:28px 12px;box-sizing:border-box;">
        <div style="width:100%;max-width:580px;margin:0 auto;background:#ffffff;border:1px solid #e8ebf0;border-radius:20px;overflow:hidden;box-shadow:0 10px 35px rgba(15,23,42,0.08);">
          <div style="padding:34px 24px 28px;text-align:center;background:linear-gradient(180deg,#f8fbff 0%,#ffffff 100%);">
            <img src="https://qrxjyvezlotjwggtgoqe.supabase.co/storage/v1/object/public/assets/logo.png" alt="PrivCloud" style="display:block;width:auto;max-width:180px;height:auto;max-height:70px;margin:0 auto;border:0;" />
            <div style="margin-top:14px;font-size:11px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:#94a3b8;">Payment Confirmation</div>
          </div>
          <div style="padding:0 24px 36px;">
            <div style="text-align:center;margin:4px 0 20px;">
              <div style="display:inline-block;width:54px;height:54px;line-height:54px;border-radius:50%;background:#ecfdf5;color:#16a34a;font-size:25px;font-weight:700;">✓</div>
            </div>
            <h1 style="margin:0;text-align:center;font-size:26px;font-weight:750;letter-spacing:-0.6px;color:#18181b;">Payment successful</h1>
            <p style="margin:14px auto 28px;max-width:450px;text-align:center;font-size:15px;line-height:1.65;color:#64748b;">
              Thank you, {uname}. Your payment has been successfully processed and your transaction is confirmed.
            </p>
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:22px 16px 24px;text-align:center;">
              <div style="font-size:11px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;color:#64748b;margin-bottom:10px;">Amount Paid</div>
              <div style="font-size:34px;line-height:1.2;font-weight:800;color:#1d4ed8;">{amt}</div>
              <div style="margin-top:10px;font-size:12px;color:#94a3b8;">Payment completed successfully</div>
            </div>
            <div style="margin-top:22px;border:1px solid #e2e8f0;border-radius:16px;overflow:hidden;">
              <div style="padding:15px 16px;background:#f8fafc;border-bottom:1px solid #e2e8f0;font-size:13px;font-weight:700;color:#334155;">Transaction Details</div>
              <div style="padding:16px;">
                <table role="presentation" style="width:100%;border-collapse:collapse;font-size:13px;">
                  <tr><td style="padding:7px 0;color:#64748b;">Transaction ID</td><td style="padding:7px 0;text-align:right;font-weight:600;color:#18181b;">{txid}</td></tr>
                  <tr><td style="padding:7px 0;color:#64748b;">Order / Invoice</td><td style="padding:7px 0;text-align:right;font-weight:600;color:#18181b;">{oid}</td></tr>
                  <tr><td style="padding:7px 0;color:#64748b;">Payment Date</td><td style="padding:7px 0;text-align:right;font-weight:600;color:#18181b;">{pdate}</td></tr>
                  <tr><td style="padding:7px 0;color:#64748b;">Payment Method</td><td style="padding:7px 0;text-align:right;font-weight:600;color:#18181b;">{pmethod}</td></tr>
                  <tr><td style="padding:7px 0;color:#64748b;">Payment Status</td><td style="padding:7px 0;text-align:right;font-weight:700;color:#16a34a;">Successful</td></tr>
                </table>
              </div>
            </div>
            <div style="margin-top:18px;padding:16px;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0;">
              <div style="font-size:13px;font-weight:700;color:#334155;margin-bottom:8px;">Purchase Details</div>
              <div style="font-size:12px;line-height:1.7;color:#64748b;">
                <strong style="color:#334155;">Plan / Product:</strong> {pname}<br>
                <strong style="color:#334155;">Billing Period:</strong> {bperiod}<br>
                <strong style="color:#334155;">Quantity:</strong> {qty}<br>
                <strong style="color:#334155;">Account Email:</strong> {user_email}
              </div>
            </div>
            <div style="margin-top:22px;padding:14px 16px;border-radius:12px;background:#ecfdf5;border:1px solid #bbf7d0;">
              <div style="font-size:13px;font-weight:700;color:#166534;">✓ Payment confirmed</div>
              <div style="font-size:12px;color:#15803d;margin-top:4px;">Your payment has been received successfully. Please keep this email for your records.</div>
            </div>
            <p style="margin:22px 0 0;text-align:center;font-size:12px;line-height:1.6;color:#94a3b8;">
              If you don't recognize this transaction or believe there is an issue with your payment, please contact PrivCloud support.
            </p>
          </div>
        </div>
      </div>
    </div>
    """
    if RESEND_API_KEY:
        try:
            with httpx.Client(timeout=8.0) as client:
                client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={"from": RESEND_FROM, "to": [email], "subject": f"Payment Confirmation - {pname}", "html": html}
                )
        except Exception:
            pass
    elif SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Payment Confirmation - {pname}"
            msg["From"] = SMTP_FROM
            msg["To"] = email
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8.0) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [email], msg.as_string())
        except Exception:
            pass
