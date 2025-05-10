from cryptography.fernet import Fernet
import modal
import os
import json
from pathlib import Path

from common import (
    VOL_MOUNT_PATH,
)


def store_token(username: str, token: str):
    # Get encryption key from Modal secret
    key = os.environ["ENCRYPTION_KEY"]
    fernet = Fernet(key)

    """Store an encrypted GitHub token for a user"""
    path = Path(f"{VOL_MOUNT_PATH}/tokens/{username}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    
    encrypted = fernet.encrypt(token.encode())
    with open(path, "w") as f:
        json.dump({"token": encrypted.decode()}, f)

def load_token(username: str) -> str:
    """Load and decrypt a user's GitHub token"""
    # Get encryption key from Modal secret
    key = os.environ["ENCRYPTION_KEY"]
    fernet = Fernet(key)

    path = Path(f"{VOL_MOUNT_PATH}/tokens/{username}.json")
    if not path.exists():
        return None
        
    with open(path) as f:
        data = json.load(f)
        return fernet.decrypt(data["token"].encode()).decode()
    
from fastapi import HTTPException
import requests

async def refresh_token(username: str, old_token: str):
    """Attempt to refresh an expired token"""
    # Get encryption key from Modal secret

    client_id = os.environ["GITHUB_CLIENT_ID"]
    client_secret = os.environ["GITHUB_CLIENT_SECRET"]

    try:
        # Try to get a new token using the old one
        response = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={
                "Accept": "application/json"
            },
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": old_token
            }
        )
        
        if response.status_code == 200:
            new_token = response.json().get("access_token")
            if new_token:
                store_token(username, new_token)
                return new_token
    except:
        pass
    
    return None

async def get_github_token(username: str):
    """Dependency to get and validate GitHub token"""
    # Get encryption key from Modal secret

    token = load_token(username)
    if not token:
        await refresh_token(username, token)
    
    # Verify token is still valid
    try:
        response = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=401,
                detail="Token expired. Please re-authenticate at /auth/github/login"
            )
    except:
        raise HTTPException(
            status_code=401,
            detail="Token invalid. Please re-authenticate at /auth/github/login"
        )
    
    return token
