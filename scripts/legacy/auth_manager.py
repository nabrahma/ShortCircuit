import logging
import time
import os
import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from kiteconnect import KiteConnect
import config

logger = logging.getLogger(__name__)

class AuthManager:
    def __init__(self):
        self.api_key = config.API_KEY
        self.api_secret = config.API_SECRET
        self.access_token_file = "access_token.txt"
        self.kite = KiteConnect(api_key=self.api_key)

    def get_valid_session(self):
        """
        Returns a authenticated KiteConnect object.
        Checks for cached token, else runs automated login.
        """
        if config.SIMULATION_MODE:
            logger.warning("⚠️ SIMULATION MODE DETECTED: Skipping Auth.")
            return None

        token = self._load_cached_token()
        if token:
            self.kite.set_access_token(token)
            # Verify validity
            try:
                self.kite.profile()
                logger.info("Cached Access Token is valid.")
                return self.kite
            except Exception as e:
                logger.warning(f"Cached token invalid: {e}. Initiating Login...")

        # If invalid/missing, run login
        access_token = self._automate_login()
        if access_token:
            self.kite.set_access_token(access_token)
            self._save_token(access_token)
            return self.kite
        else:
            raise Exception("Failed to authenticate.")

    def _load_cached_token(self):
        if os.path.exists(self.access_token_file):
            # Check modification time (< 24 hrs)
            mod_time = os.path.getmtime(self.access_token_file)
            if time.time() - mod_time < 82800: # ~23 hours safety
                with open(self.access_token_file, "r") as f:
                    return f.read().strip()
        return None

    def _save_token(self, token):
        with open(self.access_token_file, "w") as f:
            f.write(token)

    def _automate_login(self):
        """
        Uses Selenium to log in and get request token.
        """
        logger.info("Starting Selenium for Auto-Login...")
        login_url = self.kite.login_url()
        
        options = webdriver.ChromeOptions()
        if config.HEADLESS_BROWSER:
            options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        
        try:
            driver.get(login_url)
            wait = WebDriverWait(driver, 10)

            # 1. Enter User ID
            user_id_field = wait.until(EC.presence_of_element_located((By.ID, "userid")))
            user_id_field.send_keys(config.USER_ID)
            
            # 2. Enter Password
            password_field = wait.until(EC.presence_of_element_located((By.ID, "password")))
            password_field.send_keys(config.PASSWORD)
            
            # 3. Click Login
            login_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[@type='submit']")))
            login_btn.click()
            
            # 4. Handle TOTP
            totp_field = wait.until(EC.presence_of_element_located((By.ID, "userid"))) # Usually field ID might change or be 'totp' or inside a container.
            # Zerodha TOTP field usually has ID 'totp' or is determined by the numeric input
            # Let's try to find by ID 'totp' or fallback.
            # Actually, after password, it shows TOTP input.
            # For 2FA, the input ID is often just a text input.
            
            # Wait for TOTP field
            # Sometimes it is labeled 'External 2FA'
            # We'll look for input[type='text'] or similar if specific ID not found. 
            # Zerodha recently updated. Let's look for By.XPATH input with 'totp' or length checks.
            try:
                totp_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='text' and @minlength='6']")))
            except:
                # Fallback
                totp_input = driver.find_element(By.CSS_SELECTOR, "input[type=number]")

            # Generate TOTP
            totp = pyotp.TOTP(config.TOTP_SECRET)
            current_otp = totp.now()
            totp_input.send_keys(current_otp)
            
            # Sometimes auto-submits, sometimes needs enter.
            # Let's trying clicking submit if exists, or hit enter.
            # Zerodha usually doesn't require button click for TOTP if 6 digits entered?
            # Let's wait for redirect.
            
            logger.info("Entered TOTP. Waiting for redirect...")
            
            # 5. Wait for Redirect to Redirect URL
            # The URL will change to something starting with the redirect_url set in your App.
            # We don't know the user's redirect URL, but it will contain 'request_token='
            
            wait.until(EC.url_contains("request_token="))
            current_url = driver.current_url
            
            # Extract Request Token
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(current_url)
            request_token = parse_qs(parsed.query)['request_token'][0]
            
            logger.info(f"Request Token Retrieved: {request_token}")
            
            # Generate Access Token
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            return data["access_token"]
            
        except Exception as e:
            logger.error(f"Selenium Login Failed: {e}")
            return None
        finally:
            driver.quit()
