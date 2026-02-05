
import sys
import os
import config
from fyers_apiv3 import fyersModel

# Add parent dir
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

def get_url():
    session = fyersModel.SessionModel(
        client_id=config.FYERS_CLIENT_ID,
        secret_key=config.FYERS_SECRET_ID,
        redirect_uri=config.FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )
    print(session.generate_authcode())

if __name__ == "__main__":
    get_url()
