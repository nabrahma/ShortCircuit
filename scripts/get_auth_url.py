from fyers_apiv3 import fyersModel
import config

client_id = config.FYERS_CLIENT_ID
secret_key = config.FYERS_SECRET_ID
redirect_uri = config.FYERS_REDIRECT_URI

session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type="code",
    grant_type="authorization_code"
)

url = session.generate_authcode()
print(f"LOGIN_URL::{url}")
