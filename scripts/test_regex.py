import re

text = "15.100.10(0.67%)"
match = re.search(r'(\d+\.?\d*)\s*%', text)

if match:
    print(f"Match: {match.group(1)}")
else:
    print("No Match")

text2 = "1.930.17(9.66%)"
match2 = re.search(r'(\d+\.?\d*)\s*%', text2)
if match2:
    print(f"Match2: {match2.group(1)}")
else:
    print("No Match2")
