from pathlib import Path
from typing import Optional
import modal 

# Constants
MODEL_NAME = "meta-llama/Meta-Llama-3.1-8B-Instruct"
VOL_MOUNT_PATH = Path("/my_vol")
MODEL_PATH = VOL_MOUNT_PATH / "model"
WANDB_PROJECT = "github-comment-finetune"
MINUTES = 60  # seconds
HOURS = 60 * MINUTES
REMOTE_CONFIG_PATH = Path("/llama3_1_8B_lora.yaml")

# System prompt for code review
SYSTEM_PROMPT = """You are {USERNAME}, a developer who writes code review comments on GitHub.
Review the code below and provide constructive feedback in your personal comment style.
Be specific, helpful, and focus on important issues. Your tone should be snarky. Your comment should only be 1 line long. Do not include any code in your comment.
"""

app = modal.App(name="github-codereview-bot")
output_vol = modal.Volume.from_name("github-codereview-vol", create_if_missing=True)

# Images
base_image = (
    modal.Image.debian_slim()
    .pip_install("requests", "pandas", "tqdm", "cryptography",
        "fastapi",
        "uvicorn")
)

training_image = (
    modal.Image.debian_slim()
    .pip_install("wandb", "torch", "torchao", "torchvision")
    .apt_install("git")
    .pip_install("git+https://github.com/pytorch/torchtune.git@06a837953a89cdb805c7538ff5e0cc86c7ab44d9")
    .add_local_file(Path(__file__).parent / "llama3_1_8B_lora.yaml", REMOTE_CONFIG_PATH.as_posix())
)

vllm_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "vllm==0.6.3post1", "fastapi[standard]==0.115.4"
)

# Common path functions
def get_user_data_path(username: str, repo_name: Optional[str] = None) -> Path:
    """Get path to user's training data"""
    return VOL_MOUNT_PATH / (repo_name or "data") / username / "data.json"

def get_user_model_path(username: str, repo_name: Optional[str] = None) -> Path:
    """Get path to user's model directory"""
    return VOL_MOUNT_PATH / (repo_name or "data") / username / "model"

def get_user_checkpoint_path(username: str, repo_name: Optional[str] = None, version: Optional[int] = None) -> Path:
    """Get path to specific checkpoint"""
    user_model_path = get_user_model_path(username, repo_name)
    if version is None:
        version = find_latest_version(user_model_path)
    else:
        version = f"epoch_{int(version)}"
    return user_model_path / version

def find_latest_version(directory: Path) -> str:
    """Find latest epoch checkpoint in directory"""
    import re
    pattern = re.compile(r"^epoch_(\d+)$")
    largest = -1

    if not directory.exists():
        return ""

    for entry in directory.iterdir():
        if entry.is_dir():
            match = pattern.match(entry.name)
            if match:
                value = int(match.group(1))
                if value > largest:
                    largest = value

    return f"epoch_{largest}"