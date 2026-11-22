from fyers_apiv3 import fyersModel
import os
from shortcircuit import config
import logging
from pathlib import Path

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure Logging
logger = logging.getLogger(__name__)

# Resolved against the repository root, not this file's directory: a __file__
# relative path would move the cached token when the module moves, forcing an
# interactive re-auth on every start. Pinned by tests/unit/test_paths.py.
from shortcircuit.paths import TOKEN_FILE  # noqa: E402

# Timeouts and the metered transport live in shortcircuit.broker.transport, the
# one module allowed to talk to the rate limiter. Re-exported here because the
# rest of the codebase has always imported the timeout budgets from this module.
from shortcircuit.broker import transport  # noqa: E402
from shortcircuit.broker.transport import (  # noqa: E402,F401
    ASYNC_CALL_TIMEOUT,
    ASYNC_RETRIED_TIMEOUT,
    DEFAULT_HTTP_TIMEOUT,
    HTTP_MAX_RETRIES,
    HTTP_READ_TIMEOUT,
)


class TimeoutHTTPAdapter(HTTPAdapter):
    """
    HTTPAdapter that applies a default timeout to every request.

    requests only accepts a timeout per-call, and the vendored SDK never passes
    one. Injecting it at the adapter layer bounds every SDK call without having to
    patch the SDK itself.
    """

    def __init__(self, *args, timeout=DEFAULT_HTTP_TIMEOUT, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


# Set once transport.install() succeeds, so the 'no Session' path can tell an
# expected 3.1.7 layout from a genuinely unprotected client.
_REST_BOUNDED = False

# Backward-compatible name for transport.install.
enforce_rest_timeouts = transport.install


def harden_fyers_session(client, label: str = "fyers") -> bool:
    """
    Attach connection pooling, bounded retries and a hard timeout to a FyersModel.

    The broker interface already tuned its own rest_client, but the client built
    here — the one the scanner, analyzer and focus engine all share, and which
    makes every /history call — was left with library defaults: a 10-connection
    pool and no timeout whatsoever.
    """
    # FyersModel does not expose `.session`. Later SDKs keep one at
    # .service.session; the pinned 3.1.7 has none at all and calls requests.*
    # directly, so the module-level calls are bounded instead. Checking
    # hasattr(client, 'session') silently did nothing on either.
    global _REST_BOUNDED
    _REST_BOUNDED = transport.install()

    session = None
    for path in ("session", "service.session", "_service.session"):
        obj = client
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is not None and hasattr(obj, "mount"):
            session = obj
            logger.debug("[HTTP] %s: session found at client.%s", label, path)
            break

    if session is None and _REST_BOUNDED:
        # 3.1.7 has no Session, but the module-level calls are now bounded, so
        # this is expected and harmless — not the UNBOUNDED emergency the old
        # message announced twice a session for eighteen days.
        logger.debug(
            "[HTTP] %s: no requests.Session on this client (fyers-apiv3 3.1.7); "
            "calls are bounded by the module-level timeout instead.", label,
        )
        return

    if session is None:
        logger.error(
            "[HTTP] %s: could not locate a requests.Session — calls will be "
            "UNBOUNDED. Check fyers-apiv3 internals.", label
        )
        return False

    retry = Retry(
        total=HTTP_MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],   # never auto-retry POSTs (orders)
        raise_on_status=False,
    )
    adapter = TimeoutHTTPAdapter(
        pool_connections=50,
        pool_maxsize=50,
        max_retries=retry,
        timeout=DEFAULT_HTTP_TIMEOUT,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    logger.info(
        "[HTTP] %s session hardened — pool=50, timeout=%ss connect / %ss read",
        label, DEFAULT_HTTP_TIMEOUT[0], DEFAULT_HTTP_TIMEOUT[1],
    )
    return True


def _token_expired(token: str, skew_s: int = 60) -> bool:
    """
    True when the JWT's `exp` claim is in the past.

    A local check first, so a token that cannot possibly work does not consume a
    network round trip — and so the log says *why* it was skipped rather than
    reporting a generic validation failure. Undecodable tokens return False: that
    is not evidence of expiry, and the API call remains the authority.
    """
    try:
        import base64
        import json
        import time as _t

        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp")
        return bool(exp) and (_t.time() + skew_s) >= float(exp)
    except Exception:
        return False


class FyersConnect:
    """
    Singleton Fyers connection manager.

    CRITICAL RULE: Only ONE instance of this class can exist per process.
    All modules must receive this instance via dependency injection.
    Never call FyersConnect() more than once.
    """

    _instance = None  # Singleton holder

    def __new__(cls, config=None):
        """
        Singleton __new__.
        Returns existing instance if already created.
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config=None):
        """
        Initialize only once.
        Second+ calls are no-ops (returns immediately).
        """
        # GUARD: If already initialized, do nothing
        if self._initialized:
            return

        self._initialized = True
        self.config = config or {} # Handle None config
        self._access_token = None
        self._fyers = None
        
        # Helper to get config value from dict or module
        def get_cfg(key):
            if isinstance(self.config, dict):
                return self.config.get(key)
            return getattr(self.config, key, None)
            
        # Load Client ID from shortcircuit.config or env
        self.client_id = get_cfg('FYERS_CLIENT_ID') or os.getenv('FYERS_CLIENT_ID')
        self.secret_key = get_cfg('FYERS_SECRET_KEY') or get_cfg('FYERS_SECRET_ID') or os.getenv('FYERS_SECRET_KEY')
        self.redirect_uri = get_cfg('FYERS_REDIRECT_URI') or os.getenv('FYERS_REDIRECT_URI')
        
        # Ensure data directory exists
        if not TOKEN_FILE.parent.exists():
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Initialize connection
        self._connect()

    def _connect(self):
        """
        Connect to Fyers API.
        Loads saved token if valid, otherwise runs auth flow.
        """
        from fyers_apiv3 import fyersModel

        # Before the first request of the process — token validation and the
        # login exchange included — so nothing reaches the broker unmetered.
        transport.install()

        # Step 1: try every token source, validating each before use.
        #
        # NOT a way around the daily login — SEBI requires re-authentication each
        # trading day and Fyers expires the token nightly. Never persist one
        # across days. This only chooses between sources WITHIN a day, so a
        # restart at 11:00 reuses the 09:10 token. FYERS_ACCESS_TOKEN still wins
        # when valid, but it is now validated: preferring it unconditionally let
        # a stale .env line beat a live cached token and force a re-login.
        candidates = []
        env_token = (os.getenv("FYERS_ACCESS_TOKEN") or "").strip()
        if len(env_token) > 20:
            candidates.append(("env FYERS_ACCESS_TOKEN", env_token))
        file_token = self._load_token()
        if file_token:
            candidates.append((str(TOKEN_FILE), file_token))

        for source, token in candidates:
            if _token_expired(token):
                logger.warning(
                    "Token from %s is past its exp claim — skipping without an API call.",
                    source,
                )
                continue
            if self._validate_token(token):
                logger.info("✅ Using valid token from %s. Skipping auth flow.", source)
                self._access_token = token
                self._fyers = self._build_fyers_client(token)
                logger.info("✅ Fyers Connected Successfully")
                return
            logger.warning("Token from %s failed validation — trying next source.", source)

        # Step 2: Saved token invalid/missing - run auth flow ONCE
        if os.getenv("FYERS_NO_INTERACTIVE"):
            logger.error("⚠️ Token expired/missing. FYERS_NO_INTERACTIVE is set — cannot re-authenticate.")
            raise ConnectionError("No valid token and interactive login is disabled.")

        logger.info("⚠️ Token Invalid/Expired. Re-login required.")
        self._access_token = self._run_auth_flow()
        self._fyers = self._build_fyers_client(self._access_token)

        # Step 3: Save token for next run
        self._save_token(self._access_token)
        logger.info("✅ Access Token Generated & Saved!")
        logger.info("✅ Fyers Connected Successfully")

    def _run_auth_flow(self) -> str:
        """
        Run Fyers OAuth flow ONCE.
        Opens browser, waits for auth_code, exchanges for access_token.
        """
        import webbrowser
        from fyers_apiv3 import fyersModel

        # Use pre-loaded credentials
        client_id = self.client_id
        secret_key = self.secret_key
        redirect_uri = self.redirect_uri or 'https://trade.fyers.in/api-login/redirect-uri/index.html'

        if not client_id or not secret_key:
             raise ValueError("Missing FYERS_CLIENT_ID or FYERS_SECRET_ID in config/env")

        # Build auth URL
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type='code',
            grant_type='authorization_code'
        )

        auth_url = session.generate_authcode()

        print(f"\n--- FYERS LOGIN REQUIRED ---")
        print(f"1. Opening Login URL: {auth_url}")

        try:
            webbrowser.open(auth_url)
        except:
            pass

        auth_code_raw = input("👉 Paste the Auth Code (or complete redirect URL) here: ").strip()
        auth_code = auth_code_raw
        
        # Accept either a bare auth code or the whole redirect URL.
        if "auth_code=" in auth_code_raw:
             try:
                 # Extract auth_code from URL parameters
                 from urllib.parse import urlparse, parse_qs
                 parsed_url = urlparse(auth_code_raw)
                 query_params = parse_qs(parsed_url.query)
                 if 'auth_code' in query_params:
                     auth_code = query_params['auth_code'][0]
                     logger.info(f"✅ Extracted auth_code from URL: {auth_code[:5]}...{auth_code[-5:]}")
             except Exception as e:
                 logger.warning(f"Failed to parse auth_code from URL: {e}")

        # Exchange auth_code for access_token
        session.set_token(auth_code)
        response = session.generate_token()

        if response.get('s') == 'ok':
            return response['access_token']
        else:
            # If token generation fails, clear the bad token file and env var
            logger.error(f"Fyers token generation failed: {response}")
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()
            if "FYERS_ACCESS_TOKEN" in os.environ:
                del os.environ["FYERS_ACCESS_TOKEN"]
            raise ConnectionError(f"Fyers token generation failed: {response}. Bad token cleared, please try again.")

    def _build_fyers_client(self, access_token: str):
        """Build and return authenticated Fyers client."""
        from fyers_apiv3 import fyersModel
        
        # Repository-anchored, for the same reason as TOKEN_FILE above.
        from shortcircuit.paths import FYERS_REST_LOG_DIR
        FYERS_REST_LOG_DIR.mkdir(parents=True, exist_ok=True)

        client = fyersModel.FyersModel(
            client_id=self.client_id,
            token=access_token,
            log_path=str(FYERS_REST_LOG_DIR) + os.sep,
            is_async=False
        )
        # Bound every call made through this client. Without it a single stalled
        # /history request hangs whichever worker issued it, indefinitely.
        harden_fyers_session(client, label="scanner/analyzer client")
        return client

    def _validate_token(self, token: str) -> bool:
        """
        Validate token by making a lightweight API call.
        Returns True if token is valid, False if expired/invalid.
        """
        try:
            # We construct a temp client just for validation
            test_client = self._build_fyers_client(token)
            response = test_client.get_profile()
            return response.get('s') == 'ok'

        except Exception as e:
            logger.warning(f"Token validation failed: {e}")
            return False

    def _save_token(self, token: str):
        """Save access token to file."""
        try:
            TOKEN_FILE.write_text(token.strip())
            logger.debug(f"Token saved to {TOKEN_FILE}")
        except Exception as e:
            logger.warning(f"Could not save token: {e}")

    def _load_token(self) -> str | None:
        """Load access token from file."""
        try:
            if TOKEN_FILE.exists():
                token = TOKEN_FILE.read_text().strip()
                if token:
                    return token
        except Exception as e:
            logger.warning(f"Could not load token: {e}")
        return None
    
    # Delegate other methods for backward compatibility if needed,
    # but preferably access .fyers directly.
    
    @property
    def fyers(self):
        return self._fyers

    @property
    def access_token(self) -> str:
        return self._access_token

    
    def authenticate(self):
        """Legacy alias."""
        return self.fyers
            

if __name__ == "__main__":
    # Test Auth
    f = FyersConnect()
    f.authenticate()
