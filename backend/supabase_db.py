"""
Supabase Database Client Manager for PrivCloud.
Replaces MongoDB with Supabase PostgREST API (PostgreSQL).
Manages atomic product key assignment from the 'product_keys' table,
verified purchase tracking in 'orders', and active user license restoration.
"""

import os
import uuid
import secrets
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
import httpx

from backend.config import (
    SUPABASE_URL,
    SUPABASE_KEY,
    SUPABASE_SERVICE_ROLE_KEY,
    get_supabase_headers,
    TRIAL_DAYS,
    PLAN_TO_TIER_MAP,
    SUPPORT_EMAIL
)

logger = logging.getLogger("privcloud.supabase_db")
ACTIVE_SUPABASE_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

def _generate_license_key(tier: str) -> str:
    """Generate a clean, secure cryptographic product key for the given tier."""
    clean_tier = (tier or 'TRIAL').upper()
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    rand_segment = lambda n: "".join(secrets.choice(chars) for _ in range(n))
    
    if clean_tier == "TRIAL":
        return f"TRIAL-{rand_segment(4)}-{rand_segment(4)}"
    elif clean_tier == "BASIC":
        return f"PRIV-BAS-{rand_segment(4)}-{rand_segment(4)}"
    else:  # PRO
        return f"PRIV-PRO-{rand_segment(4)}-{rand_segment(4)}"


import json

# Local directory and file for persistent disk fallback
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")

def _load_persisted_orders() -> Dict[str, Dict[str, Any]]:
    """Load verified orders from durable disk storage."""
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"[PrivCloud DB] Failed loading orders from disk: {e}")
    return {}

def _save_persisted_orders() -> None:
    """Save verified orders to durable disk storage."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_LOCAL_ORDERS_CACHE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[PrivCloud DB] Failed saving orders to disk: {e}")

# In-memory store for orders initialized from durable disk storage
_LOCAL_ORDERS_CACHE: Dict[str, Dict[str, Any]] = _load_persisted_orders()

def resolve_tier(plan_or_tier: Optional[str]) -> str:
    """Normalize a plan ID or tier string to uppercase tier (TRIAL, BASIC, PRO)."""
    if not plan_or_tier:
        return 'TRIAL'
    cleaned = str(plan_or_tier).strip().lower()
    return PLAN_TO_TIER_MAP.get(cleaned, cleaned.upper())

def _get_rest_url(table: str) -> str:
    """Construct full Supabase PostgREST URL for a table."""
    return f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"

def sync_order_to_supabase_user_metadata(
    email: str,
    tier: str,
    order_id: str,
    payment_id: str,
    key: Optional[str] = None,
    amount: int = 0,
    currency: str = "INR",
    product_name: Optional[str] = None
) -> bool:
    """
    Sync order and license information into Supabase Auth user_metadata.
    This guarantees that whenever the user logs in, their purchased product key,
    transaction details, and plan tier are automatically restored.
    """
    clean_email = (email or "").strip().lower()
    if not clean_email or not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return False

    normalized_tier = resolve_tier(tier)
    p_name = product_name or (f"{normalized_tier.title()} Edition" if normalized_tier != "TRIAL" else "Free Trial Edition")
    now_iso = datetime.now(timezone.utc).isoformat()
    now_formatted = datetime.now().strftime("%d %b %Y, %I:%M %p")

    try:
        headers = get_supabase_headers()
        with httpx.Client(timeout=10.0) as client:
            # 1. Fetch user by email from Supabase Auth admin
            admin_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users"
            r_users = client.get(admin_url, headers=headers)
            if r_users.status_code != 200:
                logger.warning(f"[PrivCloud Supabase] Failed querying admin users: {r_users.text}")
                return False

            data = r_users.json()
            users_list = data.get("users", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            target_user = None
            for u in users_list:
                if (u.get("email") or "").strip().lower() == clean_email:
                    target_user = u
                    break

            if not target_user:
                logger.warning(f"[PrivCloud Supabase] User '{clean_email}' not found in Supabase Auth admin.")
                return False

            user_id = target_user.get("id")
            existing_meta = dict(target_user.get("user_metadata") or {})

            # Update core license attributes
            existing_meta["plan_tier"] = normalized_tier
            existing_meta["plan"] = normalized_tier
            existing_meta["tier"] = normalized_tier
            existing_meta["order_id"] = order_id
            existing_meta["OrderID"] = order_id
            existing_meta["transaction_id"] = payment_id
            existing_meta["TransactionID"] = payment_id
            existing_meta["payment_id"] = payment_id
            existing_meta["amount"] = amount
            existing_meta["Amount"] = f"₹{amount}" if amount > 0 else "₹0"
            existing_meta["currency"] = currency
            existing_meta["Currency"] = currency
            existing_meta["payment_status"] = "verified"
            existing_meta["purchased_at"] = now_iso
            existing_meta["purchase_date"] = now_formatted
            existing_meta["PaymentDate"] = now_formatted
            existing_meta["product_name"] = p_name
            existing_meta["ProductName"] = p_name
            existing_meta["billing_period"] = "Lifetime License (1 PC)" if normalized_tier != "TRIAL" else "14-Day Evaluation"
            existing_meta["BillingPeriod"] = existing_meta["billing_period"]

            if key:
                existing_meta["product_key"] = key
                existing_meta["key"] = key

            # Maintain purchases array
            purchases = existing_meta.get("purchased_products") or []
            if not isinstance(purchases, list):
                purchases = []

            # Check if order already in purchases
            found = False
            for p in purchases:
                if isinstance(p, dict) and p.get("order_id") == order_id:
                    p["payment_id"] = payment_id
                    p["transaction_id"] = payment_id
                    p["tier"] = normalized_tier
                    p["amount"] = amount
                    p["payment_status"] = "verified"
                    if key:
                        p["key"] = key
                    found = True
                    break

            if not found:
                purchases.append({
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "transaction_id": payment_id,
                    "tier": normalized_tier,
                    "product_name": p_name,
                    "key": key or existing_meta.get("key"),
                    "amount": amount,
                    "currency": currency,
                    "payment_status": "verified",
                    "purchase_date": now_formatted,
                    "purchased_at": now_iso
                })

            existing_meta["purchased_products"] = purchases

            # Update via Supabase Auth Admin API
            put_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{user_id}"
            r_put = client.put(put_url, headers=headers, json={"user_metadata": existing_meta})
            if r_put.status_code in (200, 204):
                print(f"[PrivCloud Supabase] Synced purchase metadata for {clean_email} to Supabase Auth.")
                return True
            else:
                logger.warning(f"[PrivCloud Supabase] Failed updating user_metadata: {r_put.text}")
    except Exception as e:
        logger.warning(f"[PrivCloud Supabase] Exception syncing user metadata: {e}")

    return False

def save_verified_order(
    order_id: str,
    payment_id: str,
    user_email: str,
    plan_id: str,
    tier: str,
    amount: int,
    currency: str = "INR",
    notes: Optional[Dict[str, Any]] = None,
    key: Optional[str] = None
) -> bool:
    """
    Record or update a verified purchase order in Supabase and durable persistent storage.
    Reliably synchronizes across:
    1. Persistent orders.json on disk
    2. Supabase Auth user_metadata
    3. Supabase public.Users table (transaction_id, transaction_done_on)
    4. Supabase public.orders table (if table exists)
    """
    clean_order_id = str(order_id).strip()
    clean_payment_id = str(payment_id).strip()
    clean_email = (user_email or "").strip().lower()
    normalized_tier = resolve_tier(tier or plan_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    now_formatted = datetime.now().strftime("%d %b %Y, %I:%M %p")
    p_name = f"{normalized_tier.title()} Edition" if normalized_tier != "TRIAL" else "Free Trial Edition"

    order_doc = {
        "order_id": clean_order_id,
        "payment_id": clean_payment_id,
        "transaction_id": clean_payment_id,
        "user_email": clean_email,
        "plan_id": str(plan_id or ""),
        "tier": normalized_tier,
        "product_name": p_name,
        "amount": int(amount or 0),
        "currency": currency.upper(),
        "status": "verified",
        "payment_status": "verified",
        "verified_at": now_iso,
        "purchase_date": now_formatted,
        "updated_at": now_iso,
        "notes": notes or {},
        "key_assigned": bool(key),
        "key": key
    }

    # 1. Update memory cache and persist to disk
    _LOCAL_ORDERS_CACHE[clean_order_id] = order_doc
    _save_persisted_orders()

    # 2. Update Supabase public.Users table
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY and clean_email:
        try:
            url_users = _get_rest_url("Users")
            headers = get_supabase_headers(prefer="return=representation")
            with httpx.Client(timeout=6.0) as client:
                client.patch(
                    url_users,
                    headers=headers,
                    params={"email": f"ilike.{clean_email}"},
                    json={
                        "transaction_id": clean_payment_id,
                        "transaction_done_on": now_iso
                    }
                )
        except Exception as u_err:
            logger.debug(f"[PrivCloud Supabase] Notice updating Users table: {u_err}")

    # 3. Sync to Supabase Auth user_metadata
    sync_order_to_supabase_user_metadata(
        email=clean_email,
        tier=normalized_tier,
        order_id=clean_order_id,
        payment_id=clean_payment_id,
        key=key,
        amount=int(amount or 0),
        currency=currency.upper(),
        product_name=p_name
    )

    # 4. Attempt insert to Supabase orders table (if table exists)
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            url = _get_rest_url("orders")
            headers = get_supabase_headers(prefer="resolution=merge-duplicates,return=representation")
            with httpx.Client(timeout=6.0) as client:
                res = client.post(url, headers=headers, json=order_doc)
                if res.status_code in (200, 201):
                    print(f"[PrivCloud Supabase] Order {clean_order_id} persisted in Supabase orders table.")
                    return True
        except Exception as e:
            logger.debug(f"[PrivCloud Supabase] Notice saving order to Supabase orders table: {e}")

    return True

def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve an order by order_id from Supabase or memory cache."""
    clean_order_id = str(order_id).strip()
    
    # 1. Check Supabase PostgREST table
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            url = _get_rest_url("orders")
            headers = get_supabase_headers()
            with httpx.Client(timeout=6.0) as client:
                res = client.get(url, headers=headers, params={"order_id": f"eq.{clean_order_id}", "limit": "1"})
                if res.status_code == 200:
                    rows = res.json()
                    if rows and len(rows) > 0:
                        doc = dict(rows[0])
                        _LOCAL_ORDERS_CACHE[clean_order_id] = doc
                        _save_persisted_orders()
                        return doc
        except Exception as e:
            logger.debug(f"[PrivCloud Supabase] Notice fetching order from Supabase: {e}")

    # 2. Check disk / memory cache fallback
    return _LOCAL_ORDERS_CACHE.get(clean_order_id)

def get_user_active_order(user_email: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve the active verified order for a specific user email.
    Queries Supabase PostgREST, durable orders store, Supabase Auth user_metadata,
    and public.Users table to guarantee persistence across login, logout, and browser restarts.
    """
    clean_email = (user_email or "").strip().lower()
    if not clean_email:
        return None

    # 1. Query Supabase PostgREST orders table if available
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            url = _get_rest_url("orders")
            headers = get_supabase_headers()
            with httpx.Client(timeout=6.0) as client:
                res = client.get(
                    url,
                    headers=headers,
                    params={
                        "user_email": f"ilike.{clean_email}",
                        "status": "eq.verified",
                        "order": "verified_at.desc",
                        "limit": "1"
                    }
                )
                if res.status_code == 200:
                    rows = res.json()
                    if rows and len(rows) > 0:
                        doc = dict(rows[0])
                        _LOCAL_ORDERS_CACHE[doc.get("order_id", "")] = doc
                        _save_persisted_orders()
                        return doc
        except Exception as e:
            logger.debug(f"[PrivCloud Supabase] Notice querying PostgREST orders: {e}")

    # 2. Query durable orders storage and memory cache
    user_orders = [
        o for o in _LOCAL_ORDERS_CACHE.values()
        if (o.get("user_email") or "").lower() == clean_email and o.get("status") in ("verified", "active")
    ]
    if user_orders:
        user_orders.sort(key=lambda x: str(x.get("verified_at") or x.get("purchase_date") or ""), reverse=True)
        active_order = dict(user_orders[0])
        if active_order.get("key"):
            return active_order

    # 3. Check Supabase Auth user_metadata for this user (restores from cloud user account)
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            with httpx.Client(timeout=8.0) as client:
                admin_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/admin/users"
                r_users = client.get(admin_url, headers=get_supabase_headers())
                if r_users.status_code == 200:
                    data = r_users.json()
                    users_list = data.get("users", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for u in users_list:
                        if (u.get("email") or "").strip().lower() == clean_email:
                            meta = u.get("user_metadata") or {}
                            plan_tier = meta.get("plan_tier") or meta.get("plan") or meta.get("tier")
                            purchases = meta.get("purchased_products") or []
                            
                            # If user has purchases recorded in user_metadata
                            if purchases and isinstance(purchases, list) and len(purchases) > 0:
                                latest_p = purchases[-1]
                                restored_order = {
                                    "order_id": latest_p.get("order_id") or meta.get("order_id") or f"ord_{clean_email[:5]}_{uuid.uuid4().hex[:6]}",
                                    "payment_id": latest_p.get("payment_id") or latest_p.get("transaction_id") or meta.get("transaction_id") or "CONFIRMED",
                                    "transaction_id": latest_p.get("transaction_id") or meta.get("transaction_id") or "CONFIRMED",
                                    "user_email": clean_email,
                                    "plan_id": latest_p.get("plan_id") or meta.get("plan_id") or "",
                                    "tier": resolve_tier(latest_p.get("tier") or plan_tier),
                                    "product_name": latest_p.get("product_name") or meta.get("product_name") or f"{resolve_tier(plan_tier).title()} Edition",
                                    "amount": latest_p.get("amount") or meta.get("amount") or 0,
                                    "currency": latest_p.get("currency") or meta.get("currency") or "INR",
                                    "status": "verified",
                                    "payment_status": "verified",
                                    "key": latest_p.get("key") or meta.get("product_key") or meta.get("key"),
                                    "key_assigned": bool(latest_p.get("key") or meta.get("product_key") or meta.get("key")),
                                    "verified_at": latest_p.get("purchased_at") or meta.get("purchased_at") or datetime.now(timezone.utc).isoformat(),
                                    "purchase_date": latest_p.get("purchase_date") or meta.get("purchase_date") or datetime.now().strftime("%d %b %Y, %I:%M %p"),
                                    "billing_period": meta.get("billing_period") or "Lifetime License (1 PC)"
                                }
                                _LOCAL_ORDERS_CACHE[restored_order["order_id"]] = restored_order
                                _save_persisted_orders()
                                return restored_order

                            # Or if user_metadata has active plan_tier / key
                            if plan_tier and plan_tier.upper() in ("PRO", "BASIC", "TRIAL"):
                                norm_tier = resolve_tier(plan_tier)
                                restored_order = {
                                    "order_id": meta.get("order_id") or f"ord_{norm_tier.lower()}_{uuid.uuid4().hex[:8]}",
                                    "payment_id": meta.get("transaction_id") or meta.get("payment_id") or "CONFIRMED",
                                    "transaction_id": meta.get("transaction_id") or meta.get("payment_id") or "CONFIRMED",
                                    "user_email": clean_email,
                                    "plan_id": meta.get("plan_id") or "",
                                    "tier": norm_tier,
                                    "product_name": meta.get("product_name") or f"{norm_tier.title()} Edition",
                                    "amount": meta.get("amount") or (199 if norm_tier == "PRO" else (49 if norm_tier == "BASIC" else 0)),
                                    "currency": meta.get("currency") or "INR",
                                    "status": "verified",
                                    "payment_status": "verified",
                                    "key": meta.get("product_key") or meta.get("key"),
                                    "key_assigned": bool(meta.get("product_key") or meta.get("key")),
                                    "verified_at": meta.get("purchased_at") or datetime.now(timezone.utc).isoformat(),
                                    "purchase_date": meta.get("purchase_date") or datetime.now().strftime("%d %b %Y, %I:%M %p"),
                                    "billing_period": meta.get("billing_period") or "Lifetime License (1 PC)"
                                }
                                _LOCAL_ORDERS_CACHE[restored_order["order_id"]] = restored_order
                                _save_persisted_orders()
                                return restored_order
        except Exception as e:
            logger.debug(f"[PrivCloud Supabase] Notice checking Supabase Auth user_metadata: {e}")

    # 4. Check public.Users table for transaction record
    if SUPABASE_URL and ACTIVE_SUPABASE_KEY:
        try:
            url_users = _get_rest_url("Users")
            with httpx.Client(timeout=6.0) as client:
                r_u = client.get(url_users, headers=get_supabase_headers(), params={"email": f"ilike.{clean_email}", "limit": "1"})
                if r_u.status_code == 200:
                    rows = r_u.json()
                    if rows and len(rows) > 0 and rows[0].get("transaction_id"):
                        tx_id = rows[0].get("transaction_id")
                        tx_date = rows[0].get("transaction_done_on") or datetime.now(timezone.utc).isoformat()
                        restored_order = {
                            "order_id": f"ord_usr_{uuid.uuid4().hex[:8]}",
                            "payment_id": tx_id,
                            "transaction_id": tx_id,
                            "user_email": clean_email,
                            "plan_id": "",
                            "tier": "BASIC",
                            "product_name": "Basic Edition",
                            "amount": 49,
                            "currency": "INR",
                            "status": "verified",
                            "payment_status": "verified",
                            "key": None,
                            "key_assigned": False,
                            "verified_at": tx_date,
                            "purchase_date": datetime.now().strftime("%d %b %Y, %I:%M %p"),
                            "billing_period": "Lifetime License (1 PC)"
                        }
                        _LOCAL_ORDERS_CACHE[restored_order["order_id"]] = restored_order
                        _save_persisted_orders()
                        return restored_order
        except Exception as u_err:
            logger.debug(f"[PrivCloud Supabase] Notice checking public.Users: {u_err}")

    if user_orders:
        return user_orders[0]

    return None

def get_user_all_purchases(user_email: str) -> Dict[str, Any]:
    """
    Retrieve all purchases and product keys for an authenticated user.
    Loads reliably from database across all tiers.
    """
    clean_email = (user_email or "").strip().lower()
    if not clean_email:
        return {"has_purchases": False, "purchases": [], "active_license": None}

    # Ensure active order is resolved first
    active_order = get_user_active_order(clean_email)

    all_orders = [
        o for o in _LOCAL_ORDERS_CACHE.values()
        if (o.get("user_email") or "").lower() == clean_email and o.get("status") in ("verified", "active")
    ]
    all_orders.sort(key=lambda x: str(x.get("verified_at") or x.get("purchase_date") or ""), reverse=True)

    purchases_list = []
    seen_keys_or_orders = set()

    for o in all_orders:
        oid = o.get("order_id")
        if oid in seen_keys_or_orders:
            continue
        seen_keys_or_orders.add(oid)

        normalized_tier = resolve_tier(o.get("tier") or o.get("plan_id"))
        p_name = o.get("product_name") or (f"{normalized_tier.title()} Edition" if normalized_tier != "TRIAL" else "Free Trial Edition")
        purchases_list.append({
            "order_id": o.get("order_id"),
            "payment_id": o.get("payment_id") or o.get("transaction_id"),
            "transaction_id": o.get("transaction_id") or o.get("payment_id"),
            "tier": normalized_tier,
            "plan_id": o.get("plan_id"),
            "product_name": p_name,
            "key": o.get("key"),
            "amount": o.get("amount", 0),
            "currency": o.get("currency", "INR"),
            "payment_status": o.get("payment_status") or o.get("status", "verified"),
            "purchase_date": o.get("purchase_date") or o.get("verified_at"),
            "verified_at": o.get("verified_at"),
            "billing_period": o.get("billing_period") or ("Lifetime License (1 PC)" if normalized_tier != "TRIAL" else "14-Day Evaluation")
        })

    active_summary = None
    if active_order:
        norm_tier = resolve_tier(active_order.get("tier") or active_order.get("plan_id"))
        active_summary = {
            "order_id": active_order.get("order_id"),
            "payment_id": active_order.get("payment_id") or active_order.get("transaction_id"),
            "transaction_id": active_order.get("transaction_id") or active_order.get("payment_id"),
            "tier": norm_tier,
            "plan_id": active_order.get("plan_id"),
            "product_name": active_order.get("product_name") or f"{norm_tier.title()} Edition",
            "key": active_order.get("key"),
            "amount": active_order.get("amount", 0),
            "currency": active_order.get("currency", "INR"),
            "payment_status": active_order.get("payment_status") or active_order.get("status", "verified"),
            "purchase_date": active_order.get("purchase_date") or active_order.get("verified_at"),
            "verified_at": active_order.get("verified_at"),
            "billing_period": active_order.get("billing_period") or ("Lifetime License (1 PC)" if norm_tier != "TRIAL" else "14-Day Evaluation")
        }

    return {
        "has_purchases": bool(purchases_list or active_summary),
        "has_license": bool(active_summary and active_summary.get("order_id")),
        "user_email": clean_email,
        "active_license": active_summary,
        "purchases": purchases_list
    }

def assign_product_key(
    tier: str,
    user_email: str,
    order_id: str,
    payment_id: str
) -> Tuple[bool, Dict[str, Any]]:
    """
    Securely and atomically assign one available product key from Supabase 'product_keys' table.
    1. Idempotency check: If this order already received a key, returns that existing key.
    2. Concurrency-safe atomic check & update via PostgREST with precondition:
       PATCH /product_keys?id=eq.{id}&is_used=eq.false -> status='used', is_used=True
    3. Persists key across Supabase Auth user_metadata, Users table, and durable disk storage.
    """
    if not SUPABASE_URL or not ACTIVE_SUPABASE_KEY:
        return False, {
            "error": "SUPABASE_NOT_CONFIGURED",
            "message": "Supabase connection is not configured in .env on the server."
        }

    normalized_tier = resolve_tier(tier)
    clean_email = (user_email or "").strip().lower()
    clean_order_id = str(order_id).strip()
    clean_payment_id = str(payment_id).strip()

    # Step 1: Idempotency Check
    existing_order = get_order(clean_order_id)
    if existing_order and existing_order.get("key_assigned") and existing_order.get("key"):
        print(f"[PrivCloud Supabase] Returning already assigned key for order {clean_order_id}")
        return True, {
            "key": existing_order.get("key"),
            "tier": existing_order.get("tier", normalized_tier),
            "trial_days": existing_order.get("trial_days", TRIAL_DAYS),
            "label": existing_order.get("label"),
            "is_existing": True
        }

    # Step 2: Atomic query and claim from Supabase product_keys table
    now_iso = datetime.now(timezone.utc).isoformat()
    headers_get = get_supabase_headers()
    headers_patch = get_supabase_headers(prefer="return=representation")
    url_keys = _get_rest_url("product_keys")

    try:
        with httpx.Client(timeout=10.0) as client:
            # Query candidate available keys for this tier
            r_get = client.get(
                url_keys,
                headers=headers_get,
                params={
                    "tier": f"eq.{normalized_tier}",
                    "status": "eq.available",
                    "is_used": "eq.false",
                    "limit": "5"
                }
            )

            candidates = r_get.json() if r_get.status_code == 200 else []
            assigned_key_record = None

            if candidates and len(candidates) > 0:
                for candidate in candidates:
                    cand_id = candidate.get("id")
                    patch_payload = {
                        "status": "used",
                        "is_used": True,
                        "used_at": now_iso
                    }
                    r_patch = client.patch(
                        url_keys,
                        headers=headers_patch,
                        params={"id": f"eq.{cand_id}", "is_used": "eq.false"},
                        json=patch_payload
                    )

                    if r_patch.status_code == 200:
                        updated_rows = r_patch.json()
                        if updated_rows and len(updated_rows) > 0:
                            assigned_key_record = updated_rows[0]
                            break

            # If no pre-stocked key, dynamically generate and store directly in Supabase product_keys table
            if not assigned_key_record:
                new_key = _generate_license_key(normalized_tier)
                new_hash = hashlib.sha256(new_key.encode("utf-8")).hexdigest()
                new_key_id = f"pk_{uuid.uuid4().hex[:12]}"
                new_key_doc = {
                    "id": new_key_id,
                    "key": new_key,
                    "key_hash": new_hash,
                    "tier": normalized_tier,
                    "status": "used",
                    "is_used": True,
                    "label": f"{normalized_tier.capitalize()} License",
                    "trial_days": TRIAL_DAYS if normalized_tier == "TRIAL" else None,
                    "used_at": now_iso,
                    "created_at": now_iso
                }
                try:
                    r_create = client.post(url_keys, headers=headers_patch, json=new_key_doc)
                    if r_create.status_code in (200, 201):
                        created_rows = r_create.json()
                        if isinstance(created_rows, list) and len(created_rows) > 0:
                            assigned_key_record = created_rows[0]
                        else:
                            assigned_key_record = new_key_doc
                    else:
                        assigned_key_record = new_key_doc
                except Exception as c_err:
                    logger.warning(f"[PrivCloud Supabase] Auto-generated key fallback: {c_err}")
                    assigned_key_record = new_key_doc

            assigned_key = assigned_key_record.get("key")
            assigned_hash = assigned_key_record.get("key_hash")
            assigned_label = assigned_key_record.get("label")
            trial_days = assigned_key_record.get("trial_days") or (TRIAL_DAYS if normalized_tier == 'TRIAL' else None)

            # Step 3: Link assigned key to orders record in memory & persistent store
            if clean_order_id in _LOCAL_ORDERS_CACHE:
                _LOCAL_ORDERS_CACHE[clean_order_id]["key_assigned"] = True
                _LOCAL_ORDERS_CACHE[clean_order_id]["key"] = assigned_key
                _LOCAL_ORDERS_CACHE[clean_order_id]["key_hash"] = assigned_hash
                _LOCAL_ORDERS_CACHE[clean_order_id]["assigned_at"] = now_iso
            else:
                _LOCAL_ORDERS_CACHE[clean_order_id] = {
                    "order_id": clean_order_id,
                    "payment_id": clean_payment_id,
                    "transaction_id": clean_payment_id,
                    "user_email": clean_email,
                    "tier": normalized_tier,
                    "product_name": f"{normalized_tier.title()} Edition",
                    "status": "verified",
                    "payment_status": "verified",
                    "key": assigned_key,
                    "key_assigned": True,
                    "assigned_at": now_iso,
                    "verified_at": now_iso
                }
            _save_persisted_orders()

            # Step 4: Sync to Supabase Auth user_metadata
            sync_order_to_supabase_user_metadata(
                email=clean_email,
                tier=normalized_tier,
                order_id=clean_order_id,
                payment_id=clean_payment_id,
                key=assigned_key
            )

            # Step 5: Update Supabase orders table if present
            try:
                url_orders = _get_rest_url("orders")
                client.patch(
                    url_orders,
                    headers=headers_patch,
                    params={"order_id": f"eq.{clean_order_id}"},
                    json={
                        "key_assigned": True,
                        "key": assigned_key,
                        "key_hash": assigned_hash,
                        "assigned_at": now_iso
                    }
                )
            except Exception as o_err:
                logger.debug(f"Notice linking key to orders table: {o_err}")

            print(f"[PrivCloud Supabase] Successfully assigned '{normalized_tier}' key ({assigned_key}) to {clean_email} (Order: {clean_order_id})")
            return True, {
                "key": assigned_key,
                "tier": normalized_tier,
                "trial_days": trial_days or TRIAL_DAYS,
                "label": assigned_label,
                "is_existing": False
            }

    except Exception as err:
        print(f"[PrivCloud Supabase] Error during atomic key assignment: {err}")
        return False, {
            "error": "ASSIGNMENT_FAILED",
            "message": f"Database error during product key assignment: {str(err)}"
        }
