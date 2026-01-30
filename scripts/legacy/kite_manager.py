import logging
import os
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException

# Manual setup for logger if not already configured
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class KiteManager:
    """
    Handles authentication, session management, and order placement with Zerodha Kite Connect.
    """
    def __init__(self, api_key, access_token=None, api_secret=None):
        self.api_key = api_key
        self.access_token = access_token
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=self.api_key)

        if self.access_token:
            self.kite.set_access_token(self.access_token)
            logger.info("KiteManager initialized with provided access token.")
        else:
            logger.warning("No access token provided. You may need to generate one.")

    def get_session(self):
        """Returns the active KiteConnect instance."""
        return self.kite

    def generate_session(self, request_token):
        """
        Generates a session using the request token.
        Requires api_secret to be set.
        """
        if not self.api_secret:
            raise ValueError("API Secret is required to generate a session.")
        
        try:
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            self.kite.set_access_token(data["access_token"])
            logger.info("Session generated successfully.")
            return data["access_token"]
        except Exception as e:
            logger.error(f"Error generating session: {e}")
            raise

    def place_order(self, symbol, quantity, transaction_type, order_type, price=None, trigger_price=None, tag="ScalpBot"):
        """
        Places an order with error handling.
        """
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=order_type,
                price=price,
                trigger_price=trigger_price,
                tag=tag
            )
            logger.info(f"Order placed: {symbol} {transaction_type} Qty: {quantity} ID: {order_id}")
            return order_id
        except InputException as e:
            logger.error(f"Order placement failed (InputException): {e}")
            # Potentially retry or notify
            return None
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    def modify_order(self, order_id, price=None, trigger_price=None, quantity=None):
        """
        Modifies an existing order.
        """
        try:
            order_id = self.kite.modify_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
                quantity=quantity,
                price=price,
                trigger_price=trigger_price
            )
            logger.info(f"Order modified: {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Order check failed: {e}")
            return None
