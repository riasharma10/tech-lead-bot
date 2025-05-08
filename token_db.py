from cryptography.fernet import Fernet
import modal
import os
import json

from common import (
    VOL_MOUNT_PATH,
)

key = os.environ["ENCRYPTION_KEY"]  # put this in modal.Secret
fernet = Fernet(key)

def store_token(username: str, token: str):
    path = f"{VOL_MOUNT_PATH}/tokens/{username}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        encrypted = fernet.encrypt(token.encode())
        json.dump({"token": encrypted.decode()}, f)

def load_token(username: str) -> str:
    path = f"{VOL_MOUNT_PATH}/tokens/{username}.json"
    with open(path) as f:
        data = json.load(f)
        return fernet.decrypt(data["token"].encode()).decode()
