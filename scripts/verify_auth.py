import logging
import config
from auth_manager import AuthManager

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def verify_auth():
    logger.info("Testing Auth Manager...")
    if not config.USER_ID or not config.PASSWORD or not config.TOTP_SECRET:
        logger.error("Missing credentials in .env! Cannot test.")
        return

    try:
        am = AuthManager()
        kite = am.get_valid_session()
        profile = kite.profile()
        logger.info(f"SUCCESS: Logged in as {profile.get('user_name', 'Unknown')}")
    except Exception as e:
        logger.error(f"Auth Test Failed: {e}")

if __name__ == "__main__":
    verify_auth()
