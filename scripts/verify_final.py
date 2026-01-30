import sys
import os
from fyers_apiv3 import fyersModel
from dotenv import load_dotenv

# Load Env
load_dotenv("d:/For coding/ShortCircuit/fyers_mcp_server/.env")

client_id = os.getenv("FYERS_CLIENT_ID")
secret_key = os.getenv("FYERS_SECRET_KEY")
redirect_uri = os.getenv("FYERS_REDIRECT_URI")

auth_code = sys.argv[1]

print(f"1. Params: ID={client_id}, Secret={secret_key[:3]}..., Code={auth_code[:10]}...")

# Exchange
session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    grant_type="authorization_code"
)

session.set_token(auth_code)
response = session.generate_token()

if response.get("code") == 200:
    access_token = response["access_token"]
    print(f"2. Token Generated: {access_token[:10]}...")
    
    # Verify Immediate
    fyers = fyersModel.FyersModel(
        client_id=client_id,
        is_async=False,
        token=access_token,
        log_path=""
    )
    
    print("3. Fetching Profile...")
    profile = fyers.get_profile()
    
    if profile.get("code") == 200:
        print("SUCCESS: Profile Fetched!")
        print(profile)
        
        # Save if success
        with open("d:/For coding/ShortCircuit/fyers_mcp_server/.env", "r") as f:
            lines = f.readlines()
        
        with open("d:/For coding/ShortCircuit/fyers_mcp_server/.env", "w") as f:
            for line in lines:
                if line.startswith("FYERS_ACCESS_TOKEN="):
                    f.write(f"FYERS_ACCESS_TOKEN={access_token}\n")
                else:
                    f.write(line)
            if not any(l.startswith("FYERS_ACCESS_TOKEN=") for l in lines):
                f.write(f"\nFYERS_ACCESS_TOKEN={access_token}\n")
                
        print("4. Saved to MCP .env")
        
        # Save to main .env & txt too
        open("d:/For coding/ShortCircuit/access_token.txt", "w").write(access_token)
        print("5. Saved to access_token.txt")
        
    else:
        print(f"FAILURE: Token valid but Profile Failed: {profile}")
else:
    print(f"FAILURE: Code Exchange Failed: {response}")
