
import os
import re
from datetime import datetime

LOG_FILE = "logs/bot.log"
OUTPUT_FILE = "terminal_log.md"
DATE_STR = datetime.now().strftime('%Y-%m-%d')

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
        out_dir = os.path.dirname(OUTPUT_FILE)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir)

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write(f"# ShortCircuit Session Log\n")
            f.write(f"> **Date:** {DATE_STR} (Updated: {datetime.now().strftime('%H:%M:%S')})\n\n")
            
            if matching_lines:
                f.write("```log\n")
                # Write ALL lines as per Requirement 3
                for line in matching_lines:
                    f.write(line + "\n")
                f.write("```\n")
            else:
                f.write(f"No log entries found for {DATE_STR} in {LOG_FILE}.\n")
        
        print(f"Successfully updated {OUTPUT_FILE}")

    except Exception as e:
        print(f"Error updating log: {e}")

if __name__ == "__main__":
    update_log()
