#!/usr/bin/env python3
"""Write .env file with Twitter cookies."""
import os

auth_token = "3716b64c5e00b94c01222c7f099446685f5ef738"
ct0 = "ca75411521d42941e83412b0d8fa469b04d1bea9b3e4622b9ec109b1f53fc2764cd7e42b15e5ae457e620c499819212923f9ca8423fb18885e647652b86645ed9342250b48239ffb511bf827dd9c28e3"

lines = []
lines.append("# Twitter/X Cookie")
lines.append("KB_HOST=0.0.0.0")
lines.append("KB_PORT=8501")
lines.append("KB_DATA_DIR=/data")
lines.append("KB_DB_PATH=/data/knowledge.db")
lines.append("KB_API_URL=http://knowledge-base:8501")

env_path = "/opt/data/projects/knowledge-base/.env"
with open(env_path, "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"Written to {env_path}")
print(f"Auth token: {len(auth_token)} chars")
print(f"CT0: {len(ct0)} chars")
