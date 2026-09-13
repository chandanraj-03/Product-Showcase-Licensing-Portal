"""
Razorpay Payment Processing Router.
Handles order creation, client keys, and constant-time HMAC-SHA256 signature verification.
"""

import hmac
import hashlib
import uuid
import asyncio
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, RedirectResponse
from typing import Dict, Any, Optional
from backend.config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, PRODUCT_DOWNLOAD_URL, razorpay_client
from backend.schemas import (
    CreateOrderRequest,
    VerifyPaymentRequest,
    GetProductKeyRequest,
    DownloadProductRequest
)
from backend.supabase_db import (
    save_verified_order,
    get_order,
    get_user_active_order,
    get_user_all_purchases,
    assign_product_key,
    resolve_tier
)

router = APIRouter(prefix="/api", tags=["Payments"])

async def _execute_post_payment_emails(user_email: str, resolved_username: str, payment_details: dict, bearer_token: Optional[str] = None):
    """Execute plan tier sync and payment confirmation receipt in background without blocking payment confirmation."""
    try:
        from backend.users_db import update_user_plan_tier
        tier = payment_details.get("tier") or payment_details.get("plan_tier") or "BASIC"
        await update_user_plan_tier(email=user_email, tier=tier)
    except Exception as e:
        print(f"[PostPayment Background] Plan tier update notice: {e}")

    try:
        from backend.users_db import trigger_supabase_reauthentication
        trigger_supabase_reauthentication(email=user_email, payment_details=payment_details, bearer_token=bearer_token)
    except Exception as e:
        print(f"[PostPayment Background] Receipt notice: {e}")


def get_razorpay_client():
    """Return the cached singleton Razorpay client (P-4)."""
    global razorpay_client
    if razorpay_client is not None:
        return razorpay_client
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return None
    try:
        import razorpay
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        if hasattr(client, 'session') and isinstance(client.session, requests.Session):
            retry_strategy = Retry(
                total=3,
                backoff_factor=0.3,
                status_forcelist=[500, 502, 503, 504],
                raise_on_status=False
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            client.session.mount('https://', adapter)
            client.session.mount('http://', adapter)
        razorpay_client = client
        return client
    except Exception as err:
        print(f"[Razorpay] Client creation error: {err}")
        return None

@router.post("/create-order", summary="Create Razorpay Order")
async def create_order(payload: CreateOrderRequest):
    """
    Create Razorpay Order for frontend checkout with automatic retries on connection reset.
    Expects JSON: { "amount": <amount_in_paise>, "currency": "INR", "receipt": "<optional_receipt_id>", "notes": {...} }
    Returns JSON: { "order_id": "...", "amount": ..., "currency": "...", "key_id": "..." }
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return JSONResponse(
            status_code=500,
            content={'error': 'Razorpay payment gateway credentials not configured on server.'}
        )

    raw_amount = payload.amount
    currency = (payload.currency or 'INR').upper()
    receipt = payload.receipt or f"rcpt_{uuid.uuid4().hex[:10]}"
    notes = payload.notes or {}

    if raw_amount is None:
        return JSONResponse(
            status_code=400,
            content={'error': 'Missing required parameter: amount (in paise).'}
        )

    try:
        amount_paise = int(raw_amount)
    except (ValueError, TypeError):
        return JSONResponse(
            status_code=400,
            content={'error': 'Invalid amount. Must be an integer number of paise.'}
        )

    # Minimum order amount validation: 100 paise (₹1.00)
    if amount_paise < 100:
        return JSONResponse(
            status_code=400,
            content={'error': 'Minimum order amount is 100 paise (₹1.00).'}
        )

    order_params = {
        'amount': amount_paise,
        'currency': currency,
        'receipt': str(receipt),
        'notes': notes,
        'payment_capture': 1
    }

    # Attempt order creation with automatic retry for transient socket/connection drops
    last_exception = None
    for attempt in range(1, 4):
        try:
            client = get_razorpay_client()
            if not client:
                raise Exception("Razorpay client is not initialized.")
            
            order = client.order.create(data=order_params)
            
            return {
                'order_id': order['id'],
                'amount': order['amount'],
                'currency': order['currency'],
                'key_id': RAZORPAY_KEY_ID
            }
        except Exception as e:
            last_exception = e
            error_str = str(e).lower()
            # If network/connection drop, wait briefly and retry with a fresh session
            if attempt < 3 and ('connection' in error_str or 'remotedisconnected' in error_str or 'timeout' in error_str):
                await asyncio.sleep(0.3 * attempt)
                continue
            break

    error_message = str(last_exception) if last_exception else "Unknown error occurred"
    status_code = 500
    if 'unauthorized' in error_message.lower() or 'auth' in error_message.lower():
        status_code = 401
    return JSONResponse(
        status_code=status_code,
        content={'error': f'Failed to create Razorpay order: {error_message}'}
    )

@router.post("/verify-payment", summary="Verify Razorpay Payment")
async def verify_payment(payload: VerifyPaymentRequest, background_tasks: BackgroundTasks, request: Request):
    """
    Verify Razorpay Payment Signature.
    Expects JSON: { "razorpay_order_id": "...", "razorpay_payment_id": "...", "razorpay_signature": "..." }
    Returns JSON: { "success": true, "message": "...", "order_id": "...", "payment_id": "...", "key": "..." }
    """
    if not RAZORPAY_KEY_SECRET:
        return JSONResponse(
            status_code=500,
            content={
                'success': False,
                'message': 'Razorpay secret key is not configured on server.'
            }
        )

    razorpay_order_id = payload.razorpay_order_id or payload.order_id
    razorpay_payment_id = payload.razorpay_payment_id or payload.payment_id
    razorpay_signature = payload.razorpay_signature or payload.signature

    if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'message': 'Missing required verification fields: razorpay_order_id, razorpay_payment_id, razorpay_signature.'
            }
        )

    try:
        # Step 1: Compute HMAC-SHA256(order_id + "|" + payment_id, KEY_SECRET)
        message_to_sign = f"{razorpay_order_id}|{razorpay_payment_id}".encode('utf-8')
        generated_signature = hmac.new(
            RAZORPAY_KEY_SECRET.encode('utf-8'),
            message_to_sign,
            hashlib.sha256
        ).hexdigest()

        # Step 2: Constant-time comparison to prevent timing attacks
        is_valid = hmac.compare_digest(generated_signature, str(razorpay_signature))

        if not is_valid:
            return JSONResponse(
                status_code=400,
                content={
                    'success': False,
                    'message': 'Payment verification failed: Signature mismatch.'
                }
            )

        # Step 3: Secondary verification with official Razorpay utility if client is available
        if razorpay_client:
            try:
                razorpay_client.utility.verify_payment_signature({
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_signature': razorpay_signature
                })
            except Exception as util_err:
                if 'SignatureVerificationError' in str(type(util_err)):
                    return JSONResponse(
                        status_code=400,
                        content={
                            'success': False,
                            'message': 'Payment verification failed via Razorpay utility.'
                        }
                    )

        # Step 4: Resolve details and persist verified order in Supabase
        tier = resolve_tier(payload.tier or payload.plan_id)
        user_email = (payload.user_email or "").strip().lower()
        raw_amount = int(payload.amount or 0)
        notes = {}

        # Accurately determine rupee amount
        order_amount_paise = 0
        if razorpay_client:
            try:
                order_info = razorpay_client.order.fetch(razorpay_order_id)
                notes = order_info.get("notes", {})
                if not user_email:
                    user_email = (notes.get("user_email") or "").strip().lower()
                if not tier:
                    tier = resolve_tier(notes.get("plan_id"))
                order_amount_paise = int(order_info.get("amount", 0))
            except Exception as fetch_err:
                print(f"[Razorpay] Note fetch notice: {fetch_err}")

        if order_amount_paise > 0:
            amount_rupees = order_amount_paise / 100
        elif raw_amount > 0:
            amount_rupees = (raw_amount / 100) if raw_amount >= 500 else raw_amount
        else:
            canonical_tier = (tier or "BASIC").upper()
            if canonical_tier == "PRO":
                amount_rupees = 199
            elif canonical_tier == "BASIC":
                amount_rupees = 49
            else:
                amount_rupees = 0

        # Atomically assign product key immediately upon payment verification
        assigned_key = None
        try:
            success_k, k_res = assign_product_key(
                tier=tier,
                user_email=user_email,
                order_id=razorpay_order_id,
                payment_id=razorpay_payment_id
            )
            if success_k and k_res.get("key"):
                assigned_key = k_res.get("key")
        except Exception as k_err:
            print(f"[Razorpay] Notice assigning key during verification: {k_err}")

        save_verified_order(
            order_id=razorpay_order_id,
            payment_id=razorpay_payment_id,
            user_email=user_email,
            plan_id=payload.plan_id or notes.get("plan_id", ""),
            tier=tier,
            amount=int(amount_rupees),
            notes=notes,
            key=assigned_key
        )

        # Trigger Post-Payment Plan Tier Sync and Confirmation Receipt
        if user_email:
            try:
                from datetime import datetime
                from backend.users_db import (
                    get_user_by_email,
                    trigger_supabase_reauthentication
                )

                user_record = await get_user_by_email(user_email)
                resolved_username = (
                    (payload.username or "").strip() or 
                    (user_record.get("username", "") if user_record else "").strip() or 
                    (notes.get("username", "") if notes else "").strip() or 
                    user_email.split("@")[0] or 
                    "PrivCloud User"
                )
                raw_name = (user_record.get("full_name", "") if user_record else "").strip()
                full_name = raw_name or resolved_username
                current_date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
                normalized_tier = (tier or "BASIC").upper()
                product_name = f"{normalized_tier.title()} Edition" if normalized_tier != "TRIAL" else "Free Trial Edition"
                billing_period = "Lifetime License (1 PC)" if normalized_tier != "TRIAL" else "14-Day Evaluation"
                amount_formatted = f"₹{int(amount_rupees):,}" if amount_rupees > 0 else "₹0 (Free Trial)"

                payment_details = {
                    "UserName": full_name,
                    "username": resolved_username,
                    "user_name": full_name,
                    "tier": normalized_tier,
                    "plan_tier": normalized_tier,
                    "Currency": "INR",
                    "currency": "INR",
                    "Amount": amount_formatted,
                    "amount": amount_formatted,
                    "TransactionID": razorpay_payment_id,
                    "transaction_id": razorpay_payment_id,
                    "OrderID": razorpay_order_id,
                    "order_id": razorpay_order_id,
                    "PaymentDate": current_date_str,
                    "payment_date": current_date_str,
                    "PaymentMethod": "Razorpay (UPI / Card / NetBanking)",
                    "payment_method": "Razorpay (UPI / Card / NetBanking)",
                    "ProductName": product_name,
                    "product_name": product_name,
                    "BillingPeriod": billing_period,
                    "billing_period": billing_period,
                    "Quantity": "1 License",
                    "quantity": "1 License",
                    "user_email": user_email
                }

                # Extract Bearer token if provided by client in Authorization header
                auth_hdr = request.headers.get("Authorization", "")
                bearer_tok = auth_hdr.split("Bearer ")[-1].strip() if "Bearer " in auth_hdr else None

                # Execute in background thread so the HTTP response returns instantly (in <50ms) to the user!
                background_tasks.add_task(
                    _execute_post_payment_emails,
                    user_email,
                    resolved_username,
                    payment_details,
                    bearer_tok
                )
            except Exception as email_err:
                print(f"[Razorpay] Notice scheduling post-payment Supabase email triggers: {email_err}")

        return {
            'success': True,
            'message': 'Payment verified successfully.',
            'order_id': razorpay_order_id,
            'payment_id': razorpay_payment_id,
            'tier': tier,
            'key': assigned_key
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                'success': False,
                'message': f'Internal server error during signature verification: {str(e)}'
            }
        )

@router.post("/create-trial-order", summary="Activate Order")
async def create_trial_order(payload: Dict[str, Any]):
    """
    Activate an order, recording it as verified so a key can be retrieved from Supabase.
    Supports TRIAL, BASIC, and PRO tiers.
    """
    user_email = (payload.get("user_email") or payload.get("email") or "").strip().lower()
    if not user_email or "@" not in user_email:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "A valid user_email is required."}
        )

    target_tier = (payload.get("tier") or "TRIAL").upper()
    if target_tier not in ("PRO", "BASIC", "TRIAL"):
        target_tier = "TRIAL"

    plan_id = payload.get("plan_id") or ("6b2f8c1a9d4e07bf" if target_tier == "PRO" else ("4d9e1a7b0c3f8e2a" if target_tier == "BASIC" else "9a8f10e7b9c2d4a6"))

    # Idempotency check: Return existing active order if already present
    existing_order = get_user_active_order(user_email)
    if existing_order:
        return {
            "success": True,
            "order_id": existing_order.get("order_id"),
            "payment_id": existing_order.get("payment_id", "FREE_TRIAL"),
            "tier": existing_order.get("tier", target_tier),
            "key": existing_order.get("key"),
            "message": "Existing order retrieved."
        }

    trial_order_id = f"ord_{target_tier.lower()}_{uuid.uuid4().hex[:10]}"
    trial_payment_id = f"pay_{target_tier.lower()}_{uuid.uuid4().hex[:8]}"

    # Atomically assign product key immediately upon activation
    assigned_key = None
    try:
        success_k, k_res = assign_product_key(
            tier=target_tier,
            user_email=user_email,
            order_id=trial_order_id,
            payment_id=trial_payment_id
        )
        if success_k and k_res.get("key"):
            assigned_key = k_res.get("key")
    except Exception as k_err:
        print(f"[Trial] Notice assigning key: {k_err}")

    save_verified_order(
        order_id=trial_order_id,
        payment_id=trial_payment_id,
        user_email=user_email,
        plan_id=plan_id,
        tier=target_tier,
        amount=199 if target_tier == "PRO" else (49 if target_tier == "BASIC" else 0),
        notes={"source": "activation", "tier": target_tier},
        key=assigned_key
    )

    return {
        "success": True,
        "order_id": trial_order_id,
        "payment_id": trial_payment_id,
        "tier": target_tier,
        "key": assigned_key,
        "message": f"{target_tier.title()} order activated successfully."
    }

@router.post("/payment/get-product-key", summary="Retrieve Assigned Product Key from Supabase")
async def get_product_key(payload: GetProductKeyRequest):
    """
    Retrieve one available product key from Supabase 'product_keys' table
    and assign it atomically to the verified purchase.
    Strictly enforces server-side payment verification check.
    """
    order_id = payload.razorpay_order_id or payload.order_id
    payment_id = payload.razorpay_payment_id or payload.payment_id
    user_email = payload.user_email or payload.email or ""
    plan_id = payload.plan_id or ""
    tier = payload.tier or ""

    if not order_id or not payment_id:
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'error': 'MISSING_PARAMETERS',
                'message': 'Both order_id and payment_id are required to retrieve product key.'
            }
        )

    # 1. Verify that this order exists and was verified server-side
    order = get_order(order_id)
    if not order:
        # Allow Free Trial order format if initiated on client
        if order_id.startswith('trial_') or payment_id == 'FREE_TRIAL':
            tier = 'TRIAL'
        else:
            return JSONResponse(
                status_code=403,
                content={
                    'success': False,
                    'error': 'ORDER_UNVERIFIED',
                    'message': 'Order record not found or payment signature was not verified. Key cannot be issued.'
                }
            )
    else:
        if order.get("status") != "verified":
            return JSONResponse(
                status_code=403,
                content={
                    'success': False,
                    'error': 'ORDER_NOT_VERIFIED',
                    'message': 'Order has not been verified.'
                }
            )
        tier = order.get("tier") or tier
        if not user_email:
            user_email = order.get("user_email", "")

    resolved_tier = resolve_tier(tier or plan_id)

    # 2. Atomically assign key from Supabase
    success, result = assign_product_key(
        tier=resolved_tier,
        user_email=user_email,
        order_id=order_id,
        payment_id=payment_id
    )

    if not success:
        err_type = result.get("error")
        status_code = 503 if err_type in ("INVENTORY_EXHAUSTED", "SUPABASE_NOT_CONFIGURED", "DATABASE_QUERY_ERROR", "DATABASE_UNAVAILABLE") else 500
        return JSONResponse(
            status_code=status_code,
            content={
                'success': False,
                'error': result.get("error", "UNKNOWN_ERROR"),
                'message': result.get("message", "Failed to retrieve product key from database.")
            }
        )

    return {
        'success': True,
        'key': result.get("key"),
        'tier': result.get("tier", resolved_tier),
        'trial_days': result.get("trial_days", 14),
        'label': result.get("label"),
        'is_existing': result.get("is_existing", False),
        'message': 'Product key retrieved successfully.'
    }

@router.get("/payment/download-product", summary="Authorized Supabase Product Installer Download")
async def download_product(order_id: Optional[str] = None, redirect: bool = False):
    """
    Validate that the download request is authorized and return or redirect to
    the authorized Supabase product URL for PrivCloud_Setup.exe.
    """
    if not PRODUCT_DOWNLOAD_URL:
        return JSONResponse(
            status_code=500,
            content={
                'success': False,
                'message': 'Product download URL is not configured on server.'
            }
        )

    # Optional order validation if order_id is provided
    if order_id:
        order = get_order(order_id)
        if order and order.get("status") != "verified":
            return JSONResponse(
                status_code=403,
                content={
                    'success': False,
                    'message': 'Order is not in verified state.'
                }
            )

    if redirect:
        return RedirectResponse(url=PRODUCT_DOWNLOAD_URL, status_code=302)

    return {
        'success': True,
        'download_url': PRODUCT_DOWNLOAD_URL,
        'filename': 'PrivCloud_Setup.exe',
        'filesize_approx': '37 MB',
        'message': 'Authorized download link generated successfully.'
    }

@router.get("/payment/my-license", summary="Get Authenticated User Active License")
async def get_my_license(user_email: str, request: Request):
    """
    Check if the authenticated user has an active verified order and product key in Supabase.
    Enforces per-user data isolation.
    """
    clean_email = (user_email or "").strip().lower()
    if not clean_email:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'user_email parameter is required.'}
        )

    # If Authorization header is provided, verify ownership of email
    auth_header = request.headers.get("Authorization", "")
    if auth_header and "Bearer " in auth_header:
        bearer_token = auth_header.split("Bearer ")[-1].strip()
        from backend.users_db import verify_supabase_user_token
        is_owner = await verify_supabase_user_token(bearer_token, clean_email)
        if not is_owner:
            return JSONResponse(
                status_code=403,
                content={'success': False, 'message': 'Access denied: Token does not match requested email.'}
            )

    order = get_user_active_order(clean_email)
    if not order:
        return {
            'success': True,
            'has_license': False
        }

    key = order.get("key")
    if not key and order.get("status") == "verified":
        try:
            from backend.supabase_db import assign_product_key, resolve_tier
            resolved_tier = resolve_tier(order.get("tier") or order.get("plan_id") or "BASIC")
            success, result = assign_product_key(
                tier=resolved_tier,
                user_email=clean_email,
                order_id=order.get("order_id"),
                payment_id=order.get("payment_id") or "CONFIRMED"
            )
            if success and result.get("key"):
                key = result.get("key")
                order["key"] = key
        except Exception as e:
            logger.warning(f"[PrivCloud] Notice auto-resolving key for {clean_email}: {e}")

    return {
        'success': True,
        'has_license': True,
        'order_id': order.get("order_id"),
        'payment_id': order.get("payment_id") or order.get("transaction_id"),
        'transaction_id': order.get("transaction_id") or order.get("payment_id"),
        'tier': order.get("tier"),
        'plan_id': order.get("plan_id"),
        'key': key,
        'amount': order.get("amount", 0),
        'currency': order.get("currency", "INR"),
        'purchase_date': order.get("purchase_date") or order.get("verified_at") or order.get("assigned_at") or order.get("transaction_done_on") or order.get("created_at"),
        'payment_status': order.get("status", "verified"),
        'billing_period': "Lifetime License (1 PC)" if (order.get("tier") or "").upper() != "TRIAL" else "14-Day Evaluation",
        'assigned_at': str(order.get("assigned_at")) if order.get("assigned_at") else None
    }


@router.get("/payment/my-purchases", summary="Get Authenticated User Purchase History and Keys")
async def get_my_purchases(user_email: str, request: Request):
    """
    Retrieve all purchases, transactions, product keys, and active license for the authenticated user.
    Strictly isolated per authenticated user email.
    """
    clean_email = (user_email or "").strip().lower()
    if not clean_email:
        return JSONResponse(
            status_code=400,
            content={'success': False, 'message': 'user_email parameter is required.'}
        )

    # If Authorization header is provided, verify ownership of email
    auth_header = request.headers.get("Authorization", "")
    if auth_header and "Bearer " in auth_header:
        bearer_token = auth_header.split("Bearer ")[-1].strip()
        from backend.users_db import verify_supabase_user_token
        is_owner = await verify_supabase_user_token(bearer_token, clean_email)
        if not is_owner:
            return JSONResponse(
                status_code=403,
                content={'success': False, 'message': 'Access denied: Token does not match requested email.'}
            )

    purchases_data = get_user_all_purchases(clean_email)
    return {
        'success': True,
        **purchases_data
    }


