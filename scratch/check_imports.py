import os
import re

def check_config_imports():
    root_dir = "/home/nabbaazz/ShortCircuit"
    files_to_check = []
    for root, dirs, files in os.walk(root_dir):
        if ".venv" in root or ".git" in root or "__pycache__" in root:
            continue
        for file in files:
            if file.endswith(".py") and file != "config.py":
                files_to_check.append(os.path.join(root, file))

    issues = []
    for file_path in files_to_check:
        with open(file_path, "r") as f:
            content = f.read()
            has_config_usage = "config." in content
            has_top_level_import = re.search(r"^import config", content, re.MULTILINE) is not None
            has_from_import = re.search(r"^from .* import config", content, re.MULTILINE) is not None
            
            if has_config_usage and not (has_top_level_import or has_from_import):
                issues.append(file_path)
    
    return issues

if __name__ == "__main__":
    issues = check_config_imports()
    for issue in issues:
        print(issue)
