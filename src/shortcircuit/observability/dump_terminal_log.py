
import os
import re
from datetime import datetime

LOG_FILE = "logs/bot.log"
DATE_STR = datetime.now().strftime('%Y-%m-%d')
OUTPUT_FILE = f"logs/{DATE_STR}_session.log"

def update_log():
    if not os.path.exists(LOG_FILE):
        print(f"Log file not found: {LOG_FILE}")
        return

    print(f"Reading log file: {LOG_FILE}")
    matching_lines = []
    
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith(DATE_STR):
                    matching_lines.append(line.rstrip())
        
        print(f"Found {len(matching_lines)} lines for {DATE_STR}")
        
        # Ensure output directory exists (if path has dirs)


        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            if matching_lines:
                for line in matching_lines:
                    f.write(line + "\n")
            else:
                f.write(f"# No log entries found for {DATE_STR} in {LOG_FILE}.\n")
        
        print(f"Successfully updated {OUTPUT_FILE}")

    except Exception as e:
        print(f"Error updating log: {e}")

if __name__ == "__main__":
    update_log()
