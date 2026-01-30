import requests
from bs4 import BeautifulSoup

def debug_moneycontrol():
    url = "https://www.moneycontrol.com/stocks/market-stats/top-gainers-nse/?index=NSE&indexName=All%2520NSE&id=-2"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    print(f"Fetching {url}...")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Status Code: {response.status_code}")
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Try to find the table
        # Based on previous logic, usually it's in a div with class 'bsr_table' or similar
        tables = soup.find_all('table')
        print(f"Found {len(tables)} tables.")
        
        # The main gainers table usually has class 'mctable1' or similar, or we iterate all
        count = 0
        for i, table in enumerate(tables):
            rows = table.find_all('tr')
            print(f"Table {i}: {len(rows)} rows.")
            if not rows: continue
            
            header_text = rows[0].get_text(strip=True).lower()
            safe_header = header_text.encode('ascii', 'ignore').decode('ascii')
            print(f"  Header: {safe_header[:100]}...")
            
            # Less strict check
            if 'high' in header_text:
                print(f"Table {i} looks promising...")
                
                print("--- First 5 Rows ---")
                for row in rows[1:6]:
                    cols = row.find_all('td')
                    if not cols: continue
                    # Safe print
                    try:
                        col_texts = [c.get_text(strip=True).encode('ascii', 'ignore').decode('ascii') for c in cols]
                        print(f"Row: {col_texts}")
                    except:
                        pass
                print("--------------------")
                count += 1
        
        if count == 0:
            print("❌ Could not identify the Gainers table.")
        else:
            print("✅ Found tables.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    debug_moneycontrol()
