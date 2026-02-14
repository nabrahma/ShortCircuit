from fyers_apiv3 import fyersModel
import config

auth_code = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBfaWQiOiJNOVFYODBSQ1RTIiwidXVpZCI6IjQzYjljY2QxNjVjMzRiNmM5NGVhZjE3ZjhjZmIxZTUxIiwiaXBBZGRyIjoiIiwibm9uY2UiOiIiLCJzY29wZSI6IiIsImRpc3BsYXlfbmFtZSI6IkZBSDgyOTcwIiwib21zIjoiSzEiLCJoc21fa2V5IjoiNWEwNGZhODU0ZmEwNWFiNGNiNTU5NmEyZmQ4N2E4Mjg3MTA5ZjY0MmQ5MDI0ZTZkMWQzYzM3MTAiLCJpc0RkcGlFbmFibGVkIjoiTiIsImlzTXRmRW5hYmxlZCI6Ik4iLCJhdWQiOiJbXCJkOjFcIixcImQ6MlwiLFwieDowXCIsXCJ4OjFcIixcIng6MlwiXSIsImV4cCI6MTc2Nzk2MDI4OSwiaWF0IjoxNzY3OTMwMjg5LCJpc3MiOiJhcGkubG9naW4uZnllcnMuaW4iLCJuYmYiOjE3Njc5MzAyODksInN1YiI6ImF1dGhfY29kZSJ9.jjiHZvFEoLnbQGG-Md5iqE34nHJla4DpyAT4H_lZ8nM"

session = fyersModel.SessionModel(
    client_id=config.FYERS_CLIENT_ID,
    secret_key=config.FYERS_SECRET_ID,
    redirect_uri=config.FYERS_REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

session.set_token(auth_code)
response = session.generate_token()

if "access_token" in response:
    with open("access_token.txt", "w") as f:
        f.write(response["access_token"])
    print("SUCCESS")
else:
    print(f"FAILED: {response}")
