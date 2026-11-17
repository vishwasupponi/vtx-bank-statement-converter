import os
import json
import sys

if getattr(sys, 'frozen', False):
    app_dir = os.path.dirname(sys.executable)
else:
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_FILE = os.path.join(app_dir, "bank_statement_config.vu")

DEFAULT_CONFIG = {
    "gemini_api_key": "",
    "theme": "dark",  # "dark" or "light"
    "last_export_dir": "",
    "consolidate_by_default": True
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            # Ensure all default keys exist
            for k, v in DEFAULT_CONFIG.items():
                if k not in config:
                    config[k] = v
            return config
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        # Atomic write: write to temp file first, then replace
        temp_path = CONFIG_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        os.replace(temp_path, CONFIG_FILE)
        return True
    except Exception:
        # Clean up temp file if it exists
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False

def get_api_key():
    config = load_config()
    return config.get("gemini_api_key", "")

def set_api_key(api_key):
    config = load_config()
    config["gemini_api_key"] = api_key.strip()
    save_config(config)
