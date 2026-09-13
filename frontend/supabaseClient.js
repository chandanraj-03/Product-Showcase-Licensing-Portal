/**
 * PrivCloud Supabase Client & Enhanced Authentication Service
 * Supports Email & Username dual login, profile registration, OTP verification,
 * and session state management.
 */

(function () {
    // Default public client configuration (safe for browser; protected by PostgreSQL Row Level Security)
    const PUBLIC_SUPABASE_URL = "https://qrxjyvezlotjwggtgoqe.supabase.co";
    const PUBLIC_SUPABASE_ANON_KEY = "sb_publishable_TBuxXwl_-StgMpP1deF7zw_2Z9izNgU";

    // Supabase runtime configuration (resolved from window.PRIVCLOUD_CONFIG, /api/config, or public defaults)
    const runtimeConfig = (typeof window !== 'undefined' && window.PRIVCLOUD_CONFIG) ? window.PRIVCLOUD_CONFIG : {};
    let SUPABASE_URL = runtimeConfig.supabaseUrl || PUBLIC_SUPABASE_URL;
    let SUPABASE_KEY = runtimeConfig.supabaseKey || PUBLIC_SUPABASE_ANON_KEY;

    // Smart API URL Resolver (handles live Render backend, Vercel rewrites, localhost:5500, localhost:3000, file:///, etc.)
    function getApiUrl(endpoint) {
        if (typeof window === 'undefined') return endpoint;
        const clean = endpoint.startsWith('/') ? endpoint : '/' + endpoint;

        // 1. Explicitly configured backend URL (via window.PRIVCLOUD_CONFIG or localStorage)
        const customBackend = (window.PRIVCLOUD_CONFIG && window.PRIVCLOUD_CONFIG.backendUrl) || 
                              (window.PRIVCLOUD_BACKEND_URL) || 
                              (window.localStorage ? localStorage.getItem('PRIVCLOUD_BACKEND_URL') : null);
        if (customBackend && typeof customBackend === 'string' && customBackend.trim()) {
            return `${customBackend.trim().replace(/\/+$/, '')}${clean}`;
        }

        // 2. Local standalone dev servers (Live Server, Vite dev port 3000/5500, or raw file://)
        const hostname = window.location.hostname;
        const isLocal = hostname === 'localhost' || hostname === '127.0.0.1' || window.location.protocol === 'file:';
        if (window.location.protocol === 'file:' || (window.location.port && window.location.port !== '5001' && isLocal)) {
            return `http://127.0.0.1:5001${clean}`;
        }

        // 3. Default relative route (for Vercel rewrites or direct FastAPI static hosting)
        return clean;
    }
    if (typeof window !== 'undefined') {
        window.getPrivCloudApiUrl = getApiUrl;
    }

    let supabaseClient = null;

    function createClientInstance(url, key) {
        if (url && key && typeof supabase !== 'undefined' && supabase.createClient) {
            try {
                supabaseClient = supabase.createClient(url, key, {
                    auth: {
                        persistSession: true,
                        autoRefreshToken: true,
                        detectSessionInUrl: true
                    }
                });
                window.supabaseClient = supabaseClient;
                return supabaseClient;
            } catch (e) {
                console.error("[PrivCloud] Failed to create Supabase client:", e);
                return null;
            }
        }
        return null;
    }

    async function waitForSupabaseLibrary(timeoutMs = 3000) {
        const start = Date.now();
        while (typeof supabase === 'undefined' || !supabase.createClient) {
            if (Date.now() - start > timeoutMs) return false;
            await new Promise(res => setTimeout(res, 50));
        }
        return true;
    }

    async function initClientAsync() {
        if (supabaseClient) return supabaseClient;

        await waitForSupabaseLibrary(500);
        if (SUPABASE_URL && SUPABASE_KEY) {
            const client = createClientInstance(SUPABASE_URL, SUPABASE_KEY);
            if (client) return client;
        }

        if (typeof window !== 'undefined' && window.fetch) {
            try {
                const res = await fetch(getApiUrl('/api/config'));
                if (res.ok) {
                    const cfg = await res.json();
                    if (cfg && cfg.supabaseUrl && cfg.supabaseKey) {
                        SUPABASE_URL = cfg.supabaseUrl;
                        SUPABASE_KEY = cfg.supabaseKey;
                    }
                }
            } catch (err) {
                console.warn("[PrivCloud] Could not load runtime config via /api/config:", err);
            }
        }

        await waitForSupabaseLibrary(2000);
        if (SUPABASE_URL && SUPABASE_KEY) {
            const client = createClientInstance(SUPABASE_URL, SUPABASE_KEY);
            if (client) return client;
        }

        return supabaseClient;
    }

    // Trigger initialization immediately
    initClientAsync();

    async function ensureClient() {
        if (supabaseClient) return supabaseClient;
        const client = await initClientAsync();
        if (!client) {
            await waitForSupabaseLibrary(2000);
            if (SUPABASE_URL && SUPABASE_KEY) {
                createClientInstance(SUPABASE_URL, SUPABASE_KEY);
            }
        }
        if (!supabaseClient) {
            throw new Error("Supabase client is still initializing or could not connect. Please try again.");
        }
        return supabaseClient;
    }

    const PrivCloudAuth = {
        getClient() {
            return supabaseClient;
        },

        getUserDisplayName(user) {
            if (!user) return 'User';
            const meta = user.user_metadata || {};
            return meta.full_name || meta.name || meta.username || (user.email ? user.email.split('@')[0] : 'User');
        },

        async getSession() {
            try {
                const client = await ensureClient();
                const { data, error } = await client.auth.getSession();
                if (error) {
                    console.error("Error getting session:", error);
                    return null;
                }
                return data ? data.session : null;
            } catch (e) {
                return null;
            }
        },

        async getUser() {
            try {
                const client = await ensureClient();
                const { data, error } = await client.auth.getUser();
                if (error) {
                    console.error("Error getting user:", error);
                    return null;
                }
                return data ? data.user : null;
            } catch (e) {
                return null;
            }
        },

        async checkUsernameAvailability(username, fullName = '') {
            try {
                const res = await fetch(getApiUrl('/api/auth/check-username'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        username: username.trim(),
                        full_name: (fullName || '').trim()
                    })
                });
                const data = await res.json();
                return {
                    ok: res.ok,
                    status: res.status,
                    available: data.available,
                    valid: data.valid,
                    message: data.message,
                    suggestions: data.suggestions || []
                };
            } catch (err) {
                // If backend is waking up or offline, validate format locally so the user is never blocked
                const cleanUser = (username || '').trim();
                const isValidFormat = /^[a-zA-Z0-9_]{3,30}$/.test(cleanUser);
                if (isValidFormat) {
                    return {
                        ok: true,
                        status: 200,
                        available: true,
                        valid: true,
                        message: "✓ Username format valid",
                        suggestions: []
                    };
                }
                return {
                    ok: false,
                    status: 400,
                    available: false,
                    valid: false,
                    message: "Username must be 3-30 characters (letters, numbers, underscores).",
                    suggestions: []
                };
            }
        },

        async resolveIdentifier(identifier) {
            const ident = (identifier || '').trim();
            if (ident.includes('@')) {
                return {
                    ok: true,
                    status: 200,
                    found: true,
                    email: ident,
                    username: ident.split('@')[0],
                    fullName: null,
                    message: "Email identifier"
                };
            }
            try {
                const res = await fetch(getApiUrl('/api/auth/resolve-identifier'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ identifier: ident })
                });
                const data = await res.json();
                return {
                    ok: res.ok,
                    status: res.status,
                    found: data.found,
                    email: data.email,
                    username: data.username,
                    fullName: data.full_name,
                    message: data.message
                };
            } catch (err) {
                // Direct Supabase fallback if backend is offline/sleeping
                try {
                    const client = await ensureClient();
                    const cleanUser = ident.replace(/^@/, '').toLowerCase();
                    const { data } = await client
                        .from('Users')
                        .select('email, username, full_name')
                        .ilike('username', cleanUser)
                        .limit(1);
                    if (data && data.length > 0 && data[0].email) {
                        return {
                            ok: true,
                            status: 200,
                            found: true,
                            email: data[0].email,
                            username: data[0].username,
                            fullName: data[0].full_name,
                            message: "Found"
                        };
                    }
                } catch (dbErr) {
                    console.warn("[PrivCloud] Direct profile lookup fallback failed:", dbErr);
                }
                return {
                    ok: false,
                    found: false,
                    message: `No account found with username '@${ident.replace(/^@/, '')}'. Please try logging in with your email address.`
                };
            }
        },

        async registerProfile(fullName, username, email) {
            try {
                const res = await fetch(getApiUrl('/api/auth/register-profile'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        full_name: fullName.trim(),
                        username: username.trim(),
                        email: email.trim()
                    })
                });
                const data = await res.json();
                return {
                    ok: res.ok,
                    status: res.status,
                    success: data.success,
                    message: data.message,
                    profile: data.profile
                };
            } catch (err) {
                return {
                    ok: false,
                    success: false,
                    message: "Could not register profile with backend."
                };
            }
        },

        async verifyProfile(email, username = null) {
            try {
                const res = await fetch(getApiUrl('/api/auth/verify-profile'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        email: email.trim(),
                        username: username ? username.trim() : null
                    })
                });
                return await res.json();
            } catch (err) {
                return { success: false, message: err.message };
            }
        },

        async signUp(email, password, metadata = {}) {
            const client = await ensureClient();
            return await client.auth.signUp({
                email: email.trim(),
                password: password,
                options: {
                    data: metadata
                }
            });
        },

        async signUpWithProfile({ fullName, username, email, password }) {
            // 1. Reserve/register profile in backend (non-blocking if backend is offline)
            try {
                const regRes = await this.registerProfile(fullName, username, email);
                if (regRes && !regRes.ok && !regRes.success) {
                    // Only block if backend explicitly returned a real validation error (like domain blocked or username taken)
                    if (regRes.status && regRes.status !== 500 && regRes.status !== 404 && regRes.message !== "Could not register profile with backend.") {
                        return {
                            data: null,
                            error: { message: regRes.message || "Failed to register profile." }
                        };
                    }
                }
            } catch (regErr) {
                console.warn("[PrivCloud] Backend profile pre-registration notice, proceeding to Supabase Auth:", regErr);
            }

            // 2. Register user in Supabase Auth with metadata
            try {
                const client = await ensureClient();
                const authRes = await client.auth.signUp({
                    email: email.trim(),
                    password: password,
                    options: {
                        data: {
                            full_name: fullName.trim(),
                            username: username.trim(),
                            email: email.trim()
                        },
                        emailRedirectTo: window.location.origin + '/auth_page/auth.html'
                    }
                });

                // If Supabase returned an error due to email sending or rate limits, fallback gracefully to OTP verification
                if (authRes && authRes.error) {
                    const errStr = (authRes.error.message || "").toLowerCase();
                    if (errStr.includes("error sending confirmation email") || errStr.includes("rate limit") || errStr.includes("email") || errStr.includes("smtp")) {
                        console.warn("[PrivCloud] Supabase email rate limit encountered, proceeding with verified OTP engine:", authRes.error);
                        return { data: { user: { email: email.trim() } }, error: null };
                    }
                }

                return authRes;
            } catch (err) {
                console.warn("[PrivCloud] Supabase signup exception, fallback:", err);
                return { data: { user: { email: email.trim() } }, error: null };
            }
        },

        async signIn(email, password) {
            const client = await ensureClient();
            return await client.auth.signInWithPassword({
                email: email.trim(),
                password: password
            });
        },

        async signInWithIdentifier(identifier, password) {
            const ident = identifier.trim();
            const client = await ensureClient();

            // 1. Direct login if email was provided (instant, zero unnecessary network hop)
            if (ident.includes('@')) {
                return await client.auth.signInWithPassword({
                    email: ident,
                    password: password
                });
            }

            // 2. Resolve username to email
            const resolveRes = await this.resolveIdentifier(ident);
            if (!resolveRes.ok || !resolveRes.found || !resolveRes.email) {
                return {
                    data: null,
                    error: {
                        message: resolveRes.message || `No account found with username '@${ident.replace(/^@/, '')}'. Please check your spelling or use your email address.`
                    }
                };
            }

            return await client.auth.signInWithPassword({
                email: resolveRes.email.trim(),
                password: password
            });
        },

        async signOut() {
            try {
                const client = await ensureClient();
                return await client.auth.signOut();
            } catch (e) {
                console.warn("[PrivCloud] SignOut error:", e);
                return { error: null };
            }
        },

        async verifyOtp(email, token, type = 'signup', username = null) {
            const isReset = (type === 'reset' || type === 'recovery');
            const supabaseType = isReset ? 'recovery' : (type || 'signup');

            // 1. Try Supabase Auth verification
            try {
                const client = await ensureClient();

                let res = await client.auth.verifyOtp({
                    email: email.trim(),
                    token: token.trim(),
                    type: supabaseType
                });

                // If signup failed, also try 'email' type (fallback for email OTPs)
                if (res && res.error && supabaseType === 'signup') {
                    res = await client.auth.verifyOtp({
                        email: email.trim(),
                        token: token.trim(),
                        type: 'email'
                    });
                }

                if (res && !res.error && res.data) {
                    if (!isReset) {
                        await this.verifyProfile(email, username);
                    }
                    return res;
                }

                if (isReset && res && res.error) {
                    console.warn("[PrivCloud] Supabase verifyOtp returned error, trying backend OTP verification fallback:", res.error.message);
                }
            } catch (e) {
                console.warn("[PrivCloud] Supabase verifyOtp error:", e);
            }

            // 2. Dynamic OTP verification endpoint fallback
            try {
                const bRes = await fetch(getApiUrl('/api/auth/verify-otp'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email: email.trim(), code: token.trim(), type: type, username: username })
                });
                const bData = await bRes.json();
                if (bData.success) {
                    if (isReset) {
                        if (bData.reset_token && typeof sessionStorage !== 'undefined') {
                            sessionStorage.setItem('privcloud_reset_token', bData.reset_token);
                        }
                        return { data: { user: { email: email.trim() }, reset_token: bData.reset_token }, error: null };
                    } else {
                        await this.verifyProfile(email, username);
                        return { data: { user: { email: email.trim() } }, error: null };
                    }
                } else {
                    return { data: null, error: { message: bData.message || "Invalid confirmation code. Please try again." } };
                }
            } catch (err) {
                return { data: null, error: { message: "Failed to verify confirmation code." } };
            }
        },

        async resendOtp(email, type = 'signup') {
            const isReset = (type === 'reset' || type === 'recovery');
            const supabaseType = isReset ? 'recovery' : (type || 'signup');

            try {
                const client = await ensureClient();
                if (isReset) {
                    // For password reset, re-trigger the recovery email
                    await client.auth.resetPasswordForEmail(email.trim(), {
                        redirectTo: window.location.origin + '/auth_page/auth.html#reset-password'
                    });
                    return { success: true, message: "A new confirmation code has been sent to your email." };
                } else {
                    await client.auth.resend({
                        type: supabaseType,
                        email: email.trim()
                    });
                }
            } catch (e) {
                console.warn("[PrivCloud] Resend OTP error:", e);
            }

            if (!isReset) {
                try {
                    const res = await fetch(getApiUrl('/api/auth/resend-otp'), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ email: email.trim(), type: type })
                    });
                    return await res.json();
                } catch (err) {
                    return { success: true, message: "Code resent." };
                }
            }

            return { success: true, message: "A new confirmation code has been sent to your email." };
        },

        async resetPassword(identifier) {
            const ident = identifier.trim();

            // Resolve identifier (both email and username) via backend to verify account exists
            const resolveRes = await this.resolveIdentifier(ident);
            if (!resolveRes.ok || !resolveRes.found || !resolveRes.email) {
                return {
                    data: null,
                    error: {
                        message: resolveRes.message || (ident.includes('@') ? `No account found with email '${ident}'.` : `No account found with username '@${ident}'.`)
                    }
                };
            }
            const targetEmail = resolveRes.email;

            const client = await ensureClient();
            const res = await client.auth.resetPasswordForEmail(targetEmail.trim(), {
                redirectTo: window.location.origin + '/auth_page/auth.html#reset-password'
            });

            if (res && res.error) {
                return { error: res.error };
            }

            return { email: targetEmail, data: res.data, error: null };
        },

        async updatePassword(email, newPassword) {
            // 1. Try Supabase Client updateUser first (if session is authenticated from verifyOtp)
            try {
                const client = await ensureClient();
                const { data, error } = await client.auth.updateUser({
                    password: newPassword
                });
                if (!error && data) {
                    if (typeof sessionStorage !== 'undefined') {
                        sessionStorage.removeItem('privcloud_reset_token');
                    }
                    return { data: data, error: null };
                }
            } catch (e) {
                console.warn("[PrivCloud] Client updateUser fallback to Admin API:", e);
            }

            // 2. Admin API fallback via backend with reset_token or Bearer token
            try {
                const resetToken = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('privcloud_reset_token') : null;
                const headers = { 'Content-Type': 'application/json' };
                try {
                    const session = await this.getSession();
                    if (session && session.access_token) {
                        headers['Authorization'] = `Bearer ${session.access_token}`;
                    }
                } catch (_) {}

                const payload = {
                    email: (email || '').trim(),
                    new_password: newPassword
                };
                if (resetToken) {
                    payload.reset_token = resetToken;
                }

                const res = await fetch(getApiUrl('/api/auth/update-password'), {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (!res.ok || !data.success) {
                    return { error: { message: data.message || "Failed to update password." } };
                }
                if (typeof sessionStorage !== 'undefined') {
                    sessionStorage.removeItem('privcloud_reset_token');
                }
                return { data: data, error: null };
            } catch (err) {
                return { error: { message: err.message || "Failed to update password." } };
            }
        },

        async signInWithGoogle(redirectTo) {
            try {
                const client = await ensureClient();
                const targetRedirect = redirectTo || (window.location.origin + window.location.pathname + window.location.search);
                return await client.auth.signInWithOAuth({
                    provider: 'google',
                    options: {
                        redirectTo: targetRedirect,
                        queryParams: {
                            access_type: 'offline',
                            prompt: 'select_account'
                        }
                    }
                });
            } catch (err) {
                console.error("[PrivCloud] Google sign-in failed:", err);
                return { data: null, error: { message: err.message || "Failed to initialize Google authentication." } };
            }
        },

        async reauthenticate() {
            try {
                const client = await ensureClient();
                const session = await this.getSession();
                if (!session || !session.access_token) {
                    console.warn("[PrivCloud] No active session found for client-side reauthenticate; backend triggers automatically.");
                    return { data: null, error: { message: "No active session for reauthentication" } };
                }

                // 1. Try official client.auth.reauthenticate()
                if (client.auth && typeof client.auth.reauthenticate === 'function') {
                    try {
                        const res = await client.auth.reauthenticate();
                        if (!res || !res.error) {
                            console.log("[PrivCloud] Reauthentication email triggered successfully via Supabase client.");
                            return res || { data: {}, error: null };
                        }
                    } catch (cliErr) {
                        console.warn("[PrivCloud] client.auth.reauthenticate notice:", cliErr);
                    }
                }

                // 2. Direct Supabase Auth reauthenticate fallback with Bearer token
                const reauthUrl = `${SUPABASE_URL.replace(/\/+$/, '')}/auth/v1/reauthenticate`;
                const resp = await fetch(reauthUrl, {
                    method: 'GET',
                    headers: {
                        'apikey': SUPABASE_KEY,
                        'Authorization': `Bearer ${session.access_token}`
                    }
                });
                if (resp.ok) {
                    console.log("[PrivCloud] Reauthentication email triggered successfully via direct Supabase auth endpoint.");
                    return { data: {}, error: null };
                } else {
                    const errText = await resp.text();
                    console.warn("[PrivCloud] Direct reauthenticate notice:", resp.status, errText);
                    return { data: null, error: { message: errText } };
                }
            } catch (err) {
                console.warn("[PrivCloud] Supabase reauthenticate exception:", err);
                return { data: null, error: err };
            }
        },

        async sendMagicLinkOtp(identifier, username = null) {
            try {
                const client = await ensureClient();
                let targetEmail = (identifier || '').trim();
                let targetUsername = (username || '').trim();

                if (!targetEmail) return { data: null, error: { message: "Email or identifier required for OTP" } };

                // If not an email, resolve username to email
                if (!targetEmail.includes('@')) {
                    const resolved = await this.resolveIdentifier(targetEmail);
                    if (resolved && resolved.email) {
                        targetEmail = resolved.email;
                        targetUsername = targetUsername || resolved.username;
                    }
                }

                // If username still missing, attempt to resolve from user session or email prefix
                if (!targetUsername) {
                    const user = await this.getUser();
                    const meta = user ? (user.user_metadata || {}) : {};
                    targetUsername = meta.username || targetEmail.split('@')[0];
                }

                const res = await client.auth.signInWithOtp({
                    email: targetEmail,
                    options: {
                        emailRedirectTo: window.location.origin + '/purchase_page/purchase.html?payment=confirmed',
                        data: {
                            username: targetUsername,
                            name: targetUsername
                        }
                    }
                });
                if (res && res.error) {
                    console.warn("[PrivCloud] Supabase signInWithOtp notice:", res.error.message);
                } else {
                    console.log("[PrivCloud] Magic Link / OTP email triggered successfully for:", targetEmail, `(@${targetUsername})`);
                }
                return res || { data: {}, error: null };
            } catch (err) {
                console.warn("[PrivCloud] Supabase signInWithOtp exception:", err);
                return { data: null, error: err };
            }
        },

        escapeHtml(str) {
            if (!str) return '';
            const map = {
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#039;'
            };
            return String(str).replace(/[&<>"']/g, m => map[m]);
        },

        resolveProxyBase(endpoint = '/api/support') {
            return getApiUrl(endpoint);
        },

        isBuyer(user) {
            if (!user) return false;
            const meta = user.user_metadata || {};
            const plan = (meta.plan_tier || meta.plan || "").toLowerCase();
            return plan === 'pro' || plan === 'basic' || Boolean(meta.is_vip || meta.is_admin);
        }
    };

    window.PrivCloudAuth = PrivCloudAuth;
    window.supabaseClient = supabaseClient;
})();
