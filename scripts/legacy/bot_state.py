import logging
import config

class BoState:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(BoState, cls).__new__(cls)
            # Initialize with Config value, but allow runtime changes
            cls._instance.ENABLE_AUTO_TRADE = config.ENABLE_AUTO_TRADE
        return cls._instance

# Global Singleton
bot_state = BoState()
