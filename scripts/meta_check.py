"""Check the Facebook Page token: which Page and Instagram account it opens, and today's quota.

Usage (from the repo root):
    python scripts/meta_check.py

Needs META_PAGE_TOKEN (from .env or the environment). Nothing is posted.
"""
import sys
from pathlib import Path

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.config import PROJECT_ROOT, ConfigError, require_env  # noqa: E402
from src.meta import MetaClient, MetaError  # noqa: E402


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        env = require_env("META_PAGE_TOKEN")
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    meta = MetaClient(env["META_PAGE_TOKEN"])
    try:
        who = meta.whoami()
        print(f"✅ Facebook Page: {who['page_name']} (id {who['page_id']})")
        if not who["instagram_id"]:
            print("❌ No Instagram account is linked to this Page: link it in the Page's settings, then make a new token")
            return 1
        print(f"✅ Instagram: @{who['instagram_username']} (id {who['instagram_id']})")
        quota = meta.publishing_quota()
        limit = (quota.get("config") or {}).get("quota_total", 100)
        print(f"✅ Instagram posts through the API in the last 24h: {quota.get('quota_usage', 0)} of {limit}")
    except MetaError as exc:
        print(f"❌ {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
