import sys
from fyers_apiv3 import fyersModel
import os
from dotenv import load_dotenv

# Load env from MCP folder for consistency (or main folder)
load_dotenv("d:/For coding/ShortCircuit/fyers_mcp_server/.env")

client_id = os.getenv("FYERS_CLIENT_ID")
secret_key = os.getenv("FYERS_SECRET_KEY")
redirect_uri = os.getenv("FYERS_REDIRECT_URI")

# auth_code passed as arg
auth_code = sys.argv[1]

print(f"Exchanging Code: {auth_code[:10]}...")

session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    grant_type="authorization_code"
)

session.set_token(auth_code)
response = session.generate_token()

if response.get("code") == 200:
    token = response["access_token"]
    print(f"SUCCESS_TOKEN:{token}")
else:
    print(f"ERROR:{response}")
