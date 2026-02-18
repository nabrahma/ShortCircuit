
import sys
import os
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fyers_connect import FyersConnect
from database import DatabaseManager
from eod_analyzer import EODAnalyzer

# Configure logging to show info
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    logger.info("🚀 Starting Manual EOD Analysis...")
    
    try:
        # Authenticate Fyers (needed for soft stop analysis data fetching)
        fyers_conn = FyersConnect()
        fyers = fyers_conn.authenticate()
        
        # Initialize DB
        db = DatabaseManager()
        
        # Initialize Analyzer
        analyzer = EODAnalyzer(fyers, db)
        
        # Run Analysis
        report = analyzer.run_daily_analysis()
        
        if report:
            print("\n" + "="*50)
            print("📅 DAILY TRADING REPORT")
            print("="*50)
            print(report)
            print("="*50 + "\n")
        else:
            logger.warning("No report generated (Maybe no trades today?)")

    except Exception as e:
        logger.error(f"EOD Analysis Failed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
