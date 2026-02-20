from fyers_apiv3 import fyersModel
import os
import config
import logging
from pathlib import Path

# Configure Logging
logger = logging.getLogger(__name__)

TOKEN_FILE = Path(__file__).resolve().parent / "data" / "access_token.txt"

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
            
        # Load Client ID from config or env
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

        # Step 1: Try loading saved token
        saved_token = self._load_token()
        
        # Also check env var as priority override
        env_token = os.getenv("FYERS_ACCESS_TOKEN")
        if env_token and len(env_token) > 20:
             logger.info("✅ Found Valid Token in Env Var. Using it.")
             saved_token = env_token

        if saved_token and self._validate_token(saved_token):
            logger.info("✅ Loaded valid token from file/env. Skipping auth flow.")
            self._access_token = saved_token
            self._fyers = self._build_fyers_client(saved_token)
            logger.info("✅ Fyers Connected Successfully")
            return

        # Step 2: Saved token invalid/missing - run auth flow ONCE
        if os.getenv("FYERS_NO_INTERACTIVE"):
            logger.warning("⚠️ Interactive login disabled (FYERS_NO_INTERACTIVE). Cannot authenticate.")
            return

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

        print(f"2. Login and copy the 'auth_code' from the URL after redirect.\n")
        auth_code = input("👉 Paste the Auth Code here: ").strip()

        # Exchange auth_code for access_token
        session.set_token(auth_code)
        response = session.generate_token()

        if response.get('s') == 'ok':
            return response['access_token']
        else:
            raise ConnectionError(f"Fyers token generation failed: {response}")

    def _build_fyers_client(self, access_token: str):
        """Build and return authenticated Fyers client."""
        from fyers_apiv3 import fyersModel
        
        LOG_ROOT = Path(__file__).resolve().parent / "logs"
        FYERS_REST_LOG_DIR = LOG_ROOT / "fyers_rest"
        FYERS_REST_LOG_DIR.mkdir(parents=True, exist_ok=True)

        return fyersModel.FyersModel(
            client_id=self.client_id,
            token=access_token,
            log_path=str(FYERS_REST_LOG_DIR) + os.sep,
            is_async=False 
        )

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

    def get_access_token(self) -> str:
        """Legacy method alias for backward compat."""
        return self._access_token
    
    def authenticate(self):
        """Legacy alias."""
        return self.fyers
            

if __name__ == "__main__":
    # Test Auth
    f = FyersConnect()
    f.authenticate()
