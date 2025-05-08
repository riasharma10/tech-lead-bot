from common import (
    MODEL_NAME,
    MODEL_PATH,
    REMOTE_CONFIG_PATH,
    get_user_data_path,
    get_user_model_path,
    output_vol,
    HOURS,
    VOL_MOUNT_PATH,
    WANDB_PROJECT,
    training_image,
    app,
)
import os
import subprocess
import modal
from modal import Image
from typing import Optional
from pathlib import Path

# Training Module
def download_model():
    """Download the model using torchtune."""
    # subprocess.run(
    #     [
    #         "tune",
    #         "download",
    #         MODEL_NAME,
    #         "--output-dir",
    #         MODEL_PATH.as_posix(),
    #         "--ignore-patterns",
    #         "original/consolidated.00.pth",
    #     ]
    # )
    # def download_model():
    """Ensure the model is properly downloaded with all config files."""
    import os
    from huggingface_hub import snapshot_download
    
    # Get token from environment
    token = os.getenv("HUGGINGFACE_TOKEN")
    
    # Download complete model (this is more reliable than tune download)
    snapshot_download(
        repo_id=MODEL_NAME,
        token=token,
        local_dir=str(MODEL_PATH),
        # ignore_patterns=["*.bin", "*.safetensors"],  # Only download config files for now
    )
    
    print(f"Downloaded model config files to {MODEL_PATH}")
    print(f"Files: {os.listdir(str(MODEL_PATH))}")

def prepare_adapter_for_inference(base_model_path, adapter_path):
    """Copy necessary tokenizer files to make adapter work with vLLM."""
    import os
    import shutil

    # Check if tokenizer files already exist in adapter directory
    adapter_tokenizer_files = [f for f in os.listdir(adapter_path) 
                              if f.startswith("tokenizer") or f.startswith("special_tokens")]

    if len(adapter_tokenizer_files) > 0:
        print("Tokenizer files already exist in adapter directory, skipping copy.")
        return
    # No tokenizer files in adapter directory: copy from base model
    print("No tokenizer files found in adapter directory, copying from base model.")
    
    # Find tokenizer files in base model
    tokenizer_files = [f for f in os.listdir(base_model_path) 
                      if f.startswith("tokenizer") or f.startswith("special_tokens")]
    
    # Copy to adapter directory
    for file in tokenizer_files:
        src = os.path.join(base_model_path, file)
        dst = os.path.join(adapter_path, file)
        shutil.copy(src, dst)
        print(f"Copied {file} to adapter directory")
    
    # Ensure adapter_config.json has all required fields
    import json
    config_path = os.path.join(adapter_path, "adapter_config.json")
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    # Add missing fields if needed
    updated = False
    if "base_model_name_or_path" not in config:
        config["base_model_name_or_path"] = "meta-llama/Meta-Llama-3.1-8B-Instruct"
        updated = True
    if "task_type" not in config:
        config["task_type"] = "CAUSAL_LM"
        updated = True
    
    if updated:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print("Updated adapter_config.json with missing fields")


@app.function(
    image=training_image,
    gpu="H100",
    volumes={VOL_MOUNT_PATH: output_vol},
    timeout=2 * HOURS,
    secrets=[
        modal.Secret.from_name("huggingface-secret"),
        modal.Secret.from_name("wandb-secret")
    ],
)
def finetune(username: str, repo_owner: str = None, recipe_args: str = None, cleanup: bool = False):
    """Fine-tune a model on the user's GitHub comment history.
    
    Args:
        username: GitHub username
        repo_owner: Repository owner for data path
        recipe_args: Additional arguments to pass to torchtune
        cleanup: Remove user data after fine-tuning
    """
    import shlex
    import shutil

    # if MODEL_PATH.exists():
    #      shutil.rmtree(MODEL_PATH)

    # if get_user_model_path(username, repo_owner).exists():
    #      shutil.rmtree(get_user_model_path(username, repo_owner))

    # print("Downloading model...")
    # download_model()
    # output_vol.commit()

    data_path = get_user_data_path(username, repo_owner)
    output_dir = get_user_model_path(username, repo_owner)
    output_dir.mkdir(parents=True, exist_ok=True)

    if recipe_args is not None:
        recipe_args = shlex.split(recipe_args)
    else:
        recipe_args = []

    wandb_args = [
        "metric_logger._component_=torchtune.training.metric_logging.WandBLogger",
        f"metric_logger.project={WANDB_PROJECT}",
    ]

    print("Starting fine-tuning...")

    subprocess.run(
        [
            "tune",
            "run",
            "lora_finetune_single_device",
            "--config",
            REMOTE_CONFIG_PATH,
            f"output_dir={output_dir.as_posix()}",
            f"dataset_path={data_path.as_posix()}",
            f"model_path={MODEL_PATH.as_posix()}",
            *wandb_args,
        ]
        + recipe_args
    )

    print("Fine-tuning complete.")
    print(f"Model saved to {output_dir}")

    prepare_adapter_for_inference(MODEL_PATH, output_dir / "epoch_1")
    # Check if the model was saved correctly    
    if not (output_dir / "epoch_1").exists():
        print("Error: Model not saved correctly.")
        return

    if cleanup and username != "test":
        # Delete scraped data after fine-tuning
        os.remove(data_path)
