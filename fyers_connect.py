from fyers_apiv3 import fyersModel
import webbrowser
import os
import config
import logging
import datetime

# Configure Logging
logger = logging.getLogger(__name__)

class FyersConnect:
    def __init__(self):
        self.client_id = config.FYERS_CLIENT_ID
        self.secret_key = config.FYERS_SECRET_ID
        self.redirect_uri = config.FYERS_REDIRECT_URI
        self.token_file = "access_token.txt"
        self.fyers = None

    def get_access_token(self):
        """
        Reads access token from file.
        """
        if os.path.exists(self.token_file):
            with open(self.token_file, "r") as f:
                token = f.read().strip()
                # Basic validation: check if token is empty
                if len(token) > 10:
                    return token
        return None

    def generate_token(self):
        """
        Manual Login Flow to generate new token.
        """
        session = fyersModel.SessionModel(
            client_id=self.client_id,
            secret_key=self.secret_key,
            redirect_uri=self.redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )
        
        # 1. Generate Login URL
        login_url = session.generate_authcode()
        
        print("\n--- FYERS LOGIN REQUIRED ---")
        print(f"1. Opening Login URL: {login_url}")
        print("2. Login and copy the 'auth_code' from the URL after redirect.")
        
        try:
            webbrowser.open(login_url)
        except:
            pass
            
        auth_code = input("\n👉 Paste the Auth Code here: ").strip()
        
        # 2. Generate Access Token
        session.set_token(auth_code)
        response = session.generate_token()
        
        if "access_token" in response:
            access_token = response["access_token"]
            with open(self.token_file, "w") as f:
                f.write(access_token)
            print("✅ Access Token Generated & Saved!")
            return access_token
        else:
            print(f"❌ Login Failed: {response}")
            raise Exception("Fyers Login Failed")

    def authenticate(self):
        """
        Main entry point. Returns authenticated Fyers instance.
        """
        token = self.get_access_token()
        
        if not token:
            print("⚠️ No Access Token found. Initiating Login...")
            token = self.generate_token()
            
        # Initialize Fyers Model
        self.fyers = fyersModel.FyersModel(
            client_id=self.client_id,
            is_async=False,
            token=token,
            log_path="logs"
        )
        
        # Validate Session by fetching profile
        try:
            profile = self.fyers.get_profile()
            if "code" in profile and profile["code"] != 200:
                # Token expired or invalid
                print(f"⚠️ Token Invalid/Expired ({profile.get('message')}). Re-login required.")
                token = self.generate_token()
                self.fyers = fyersModel.FyersModel(
                    client_id=self.client_id,
                    is_async=False,
                    token=token,
                    log_path="logs"
                )
            
            logger.info("✅ Fyers Connected Successfully")
            return self.fyers
            
        except Exception as e:
            logger.error(f"Fyers Connection Error: {e}")
            raise

if __name__ == "__main__":
    # Test Auth
    f = FyersConnect()
    f.authenticate()
