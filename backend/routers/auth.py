"""
Authentication & Configuration Router for PrivCloud.
Handles dynamic Supabase config, email provider allowlist checks,
username uniqueness verification, dual-identifier resolution (email or username),
user profile registration, OTP verification state updates, and secure password reset.
"""

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from backend.config import (
    SUPABASE_URL,
    SUPABASE_KEY,
    SUPPORT_EMAIL,
    ALLOWED_EMAIL_DOMAINS,
    PLAN_TO_TIER_MAP,
    is_allowed_email_domain
)
from backend.schemas import (
    EmailValidationRequest,
    UsernameCheckRequest,
    IdentifierResolveRequest,
    RegisterProfileRequest,
    VerifyProfileRequest,
    ForgotPasswordRequest,
    VerifyOtpRequest,
    ResendOtpRequest,
    UpdatePasswordRequest
)
from backend.users_db import (
    validate_username_format,
    is_username_available,
    resolve_identifier,
    register_user,
    generate_user_otp,
    verify_user_otp,
    mark_user_verified,
    generate_username_suggestions,
    update_user_password_admin,
    validate_and_consume_reset_token,
    verify_supabase_user_token,
    trigger_supabase_account_ready
)

router = APIRouter(prefix="/api", tags=["Authentication & Config"])

@router.get("/config", summary="Get Public Runtime Config")
async def get_config():
    """
    Dynamically serve public runtime config without exposing secrets in static files.
    Exposes Supabase keys, allowed email domains, plan mappings, and support contact (Q-2, Q-3, M-3).
    """
    return {
        'supabaseUrl': SUPABASE_URL,
        'supabaseKey': SUPABASE_KEY,
        'allowedEmailDomains': sorted(list(ALLOWED_EMAIL_DOMAINS)),
        'plans': PLAN_TO_TIER_MAP,
        'supportEmail': SUPPORT_EMAIL
    }

@router.post("/auth/validate-email", summary="Validate Email Domain")
async def validate_email(payload: EmailValidationRequest):
    """API endpoint to validate email domain against the strict provider allowlist."""
    email = payload.email.strip()
    
    if not is_allowed_email_domain(email):
        return JSONResponse(
            status_code=400,
            content={
                'valid': False,
                'message': 'Only major email providers are allowed (Gmail, Outlook, Yahoo, Proton, iCloud, Zoho).'
            }
        )

    return {
        'valid': True,
        'message': 'Email domain is verified.'
    }

@router.post("/auth/check-username", summary="Check Username Availability & Get Suggestions")
async def check_username(payload: UsernameCheckRequest):
    """
    Check if a username has valid syntax and is available.
    Returns smart available suggestions if the username is taken.
    """
    username = payload.username.strip()
    full_name = payload.full_name.strip() if payload.full_name else None
    
    valid_format, msg = validate_username_format(username)
    if not valid_format:
        suggestions = await generate_username_suggestions(base_username=username, full_name=full_name, limit=4)
        return JSONResponse(
            status_code=400,
            content={
                'available': False,
                'valid': False,
                'message': msg,
                'suggestions': suggestions
            }
        )

    available = await is_username_available(username)
    if not available:
        suggestions = await generate_username_suggestions(base_username=username, full_name=full_name, limit=4)
        return JSONResponse(
            status_code=409,
            content={
                'available': False,
                'valid': True,
                'message': 'This username is already taken. Please choose another.',
                'suggestions': suggestions
            }
        )

    return {
        'available': True,
        'valid': True,
        'message': 'Username is available.',
        'suggestions': []
    }

@router.post("/auth/resolve-identifier", summary="Resolve Username or Email to Profile")
async def resolve_auth_identifier(payload: IdentifierResolveRequest):
    """
    Resolve an identifier (username or email) into user profile details.
    Allows Instagram-style dual login (Email OR Username).
    """
    ident = payload.identifier.strip()
    if not ident:
        return JSONResponse(
            status_code=400,
            content={'found': False, 'message': 'Identifier cannot be empty.'}
        )

    user = await resolve_identifier(ident)
    if not user:
        msg = f"No account found with email '{ident}'." if '@' in ident else f"No account found with username '@{ident}'."
        return JSONResponse(
            status_code=404,
            content={'found': False, 'message': msg}
        )

    return {
        'found': True,
        'email': user.get('email'),
        'username': user.get('username'),
        'full_name': user.get('full_name')
    }

@router.post("/auth/register-profile", summary="Register User Profile")
async def register_profile(payload: RegisterProfileRequest):
    """
    Validate and persist user profile metadata (full_name, username, email)
    ensuring uniqueness of both username and email before OTP verification.
    """
    email = payload.email.strip()
    username = payload.username.strip()
    full_name = payload.full_name.strip()

    if not full_name or len(full_name) < 2:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'Please provide a valid full name.'}
        )

    if not is_allowed_email_domain(email):
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'message': 'Only major email providers are allowed (Gmail, Outlook, Yahoo, Proton, iCloud, Zoho).'
            }
        )

    valid_format, msg = validate_username_format(username)
    if not valid_format:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': msg}
        )

    success, msg, record = await register_user(full_name, username, email, is_verified=False)
    if not success:
        return JSONResponse(
            status_code=409,
            content={'success': False, 'message': msg}
        )

    return {
        'success': True,
        'message': 'Profile registered successfully. Awaiting account confirmation.',
        'profile': record
    }

@router.post("/auth/verify-profile", summary="Mark Profile as Verified")
async def verify_profile(payload: VerifyProfileRequest, background_tasks: BackgroundTasks):
    """
    Mark the user's registered profile as verified after successful OTP verification.
    """
    email = payload.email.strip()
    username = payload.username.strip() if payload.username else None
    
    updated = await mark_user_verified(email=email, username=username)
    if updated and email:
        background_tasks.add_task(trigger_supabase_account_ready, email, username)
    return {
        'success': updated,
        'message': 'Profile verification status updated.' if updated else 'Profile not found.'
    }

@router.post("/auth/forgot-password", summary="Initiate Forgot Password")
async def forgot_password(payload: ForgotPasswordRequest):
    """
    Resolve identifier (username or email) and return the email to send reset instructions to.
    """
    ident = payload.identifier.strip()
    if not ident:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'Please enter your email or username.'}
        )

    user = await resolve_identifier(ident)
    if not user:
        msg = f"No account found with email '{ident}'." if '@' in ident else f"No account found with username '@{ident}'."
        return JSONResponse(
            status_code=404,
            content={'success': False, 'message': msg}
        )

    target_email = user.get('email')

    return {
        'success': True,
        'email': target_email,
        'message': f"Password reset instructions will be sent to {target_email}."
    }

@router.post("/auth/verify-otp", summary="Verify OTP Code")
async def verify_otp_endpoint(payload: VerifyOtpRequest, background_tasks: BackgroundTasks):
    """
    Verify user 6-digit confirmation code.
    If verifying for password reset/recovery, generates a secure single-use reset token (S-2).
    """
    email = payload.email.strip()
    code = payload.code.strip()
    otp_type = (payload.type or "signup").strip().lower()
    
    success, message, reset_token = await verify_user_otp(email=email, code=code, otp_type=otp_type)
    if not success:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': message}
        )
        
    result = {
        'success': True,
        'message': message
    }
    if reset_token:
        result['reset_token'] = reset_token

    if otp_type == "signup":
        background_tasks.add_task(trigger_supabase_account_ready, email)

    return result

@router.post("/auth/resend-otp", summary="Resend OTP Code")
async def resend_otp_endpoint(payload: ResendOtpRequest):
    """
    Generate and resend OTP code for email.
    """
    email = payload.email.strip()
    if not is_allowed_email_domain(email):
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'Invalid email domain.'}
        )

    otp_type = (payload.type or "signup").strip().lower()
    otp = await generate_user_otp(email, purpose=otp_type)
    
    return {
        'success': True,
        'message': f"A new 6-digit confirmation code has been generated for {email}."
    }

@router.post("/auth/update-password", summary="Update User Password")
async def update_password_endpoint(payload: UpdatePasswordRequest, request: Request):
    """
    Secure backend endpoint to update a user's password using Supabase Service Role Admin API.
    Guarded by:
    1. Single-use reset_token issued by verify-otp, OR
    2. Active Supabase Auth Bearer token matching the user email (S-2).
    """
    email = payload.email.strip().lower()
    new_password = payload.new_password.strip()
    reset_token = (payload.reset_token or "").strip()
    
    if not email:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'Email is required.'}
        )
        
    if not new_password or len(new_password) < 6:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'Password must be at least 6 characters long.'}
        )

    # Validate authorization: Check reset_token or Bearer token
    auth_header = request.headers.get("Authorization", "")
    bearer_token = auth_header.split("Bearer ")[-1].strip() if "Bearer " in auth_header else ""
    
    is_authorized = False
    if reset_token:
        is_authorized = validate_and_consume_reset_token(email, reset_token)
    
    if not is_authorized and bearer_token:
        is_authorized = await verify_supabase_user_token(bearer_token, email)

    if not is_authorized:
        return JSONResponse(
            status_code=401,
            content={
                'success': False,
                'message': 'Unauthorized. A verified confirmation code or active session is required to update password.'
            }
        )
        
    success, message = await update_user_password_admin(email=email, new_password=new_password)
    if not success:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': message}
        )
        
    return {
        'success': True,
        'message': message
    }
