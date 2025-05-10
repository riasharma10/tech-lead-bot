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


def download_model():
    """Download the model using torchtune."""
    import subprocess
    import os
    import shutil

    # Clean up existing model directory if it exists
    if MODEL_PATH.exists():
        print(f"Cleaning up existing model directory: {MODEL_PATH}")
        shutil.rmtree(MODEL_PATH)
    
    MODEL_PATH.mkdir(parents=True, exist_ok=True)
    token = os.getenv("HUGGINGFACE_TOKEN")
    
    print(f"Downloading model {MODEL_NAME} to {MODEL_PATH}")
    try:
        subprocess.run(
            [
                "tune",
                "download",
                MODEL_NAME,
                "--output-dir",
                MODEL_PATH.as_posix(),
                "--ignore-patterns",
                "original/consolidated.00.pth",
                "--hf-token",  # Changed from --token to --hf-token
                token,
            ],
            check=True  # This will raise an exception if the command fails
        )
        
        # Verify the download
        print("\nDownloaded files:")
        for root, dirs, files in os.walk(MODEL_PATH):
            level = root.replace(str(MODEL_PATH), '').count(os.sep)
            indent = ' ' * 4 * level
            print(f"{indent}{os.path.basename(root)}/")
            subindent = ' ' * 4 * (level + 1)
            for f in files:
                print(f"{subindent}{f}")
        
        print("\nModel download complete!")
        
    except subprocess.CalledProcessError as e:
        print(f"Error downloading model: {e}")
        raise
    except Exception as e:
        print(f"Unexpected error during model download: {e}")
        raise

def prepare_adapter_for_inference(base_model_path, adapter_path):
    """Copy necessary tokenizer files to make adapter work with vLLM."""
    import os
    import shutil

    print(f"\nPreparing adapter for inference:")
    print(f"Base model path: {base_model_path}")
    print(f"Adapter path: {adapter_path}")

    # Check if tokenizer files already exist in adapter directory
    adapter_tokenizer_files = [f for f in os.listdir(adapter_path) 
                              if f.startswith("tokenizer") or f.startswith("special_tokens")]

    if len(adapter_tokenizer_files) > 0:
        print("Tokenizer files already exist in adapter directory, skipping copy.")
        print(f"Found files: {adapter_tokenizer_files}")
        return

    # No tokenizer files in adapter directory: copy from base model
    print("No tokenizer files found in adapter directory, copying from base model.")
    
    # Find tokenizer files in base model
    tokenizer_files = [f for f in os.listdir(base_model_path) 
                      if f.startswith("tokenizer") or f.startswith("special_tokens")]
    
    print(f"Found tokenizer files in base model: {tokenizer_files}")
    
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
    
    print("\nCurrent adapter config:")
    print(json.dumps(config, indent=2))
    
    # Add missing fields if needed
    updated = False
    if "base_model_name_or_path" not in config:
        config["base_model_name_or_path"] = "meta-llama/Meta-Llama-3.1-8B-Instruct"
        updated = True
    if "task_type" not in config:
        config["task_type"] = "CAUSAL_LM"
        updated = True
    
    if updated:
        print("\nUpdating adapter config with missing fields...")
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print("Updated adapter config:")
        print(json.dumps(config, indent=2))
    
    print("\nAdapter preparation complete!")

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
def finetune(username: str, repo_owner: str = None, recipe_args: str = None, cleanup: bool = False, repo_name: str = None, force_reload: bool = False):
    """Fine-tune a model on the user's GitHub comment history.
    
    Args:
        username: GitHub username
        repo_owner: Repository owner for data path
        recipe_args: Additional arguments to pass to torchtune
        cleanup: Remove user data after fine-tuning
    """
    import shlex
    import shutil

    #  if something happens to the model, we can download it again
    if not MODEL_PATH.exists():
        print("Downloading model...")
        download_model()
        output_vol.commit()

    data_path = get_user_data_path(username, repo_name)
    print(f"Data path: {data_path}")
    output_dir = get_user_model_path(username, repo_name)
    print(f"Output dir: {output_dir}")

    if output_dir.exists() and (output_dir / "epoch_1").exists() and not force_reload:
        print(f"Model already exists for {username}/{repo_name}, skipping fine-tuning.")
        return

    print(f"Username: {username}")
    print(f"Repo name: {repo_name}")
    print(f"Repo owner: {repo_owner}")

    if force_reload and output_dir.exists():
        print(f"Removing existing model for {username}/{repo_name}")
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    wandb_args = [
        "metric_logger._component_=torchtune.training.metric_logging.WandBLogger",
        f"metric_logger.project={WANDB_PROJECT}",
    ]

    print("Starting fine-tuning...")

    try: 
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
        )

        print("Fine-tuning complete.")
        print(f"Model saved to {output_dir}")

        # Verify the model was saved correctly
        epoch_dir = output_dir / "epoch_1"
        if not epoch_dir.exists():
            raise FileNotFoundError(f"Model directory not found at {epoch_dir}")

        # List contents of epoch_1 directory
        print("\nContents of epoch_1 directory:")
        for file in epoch_dir.iterdir():
            print(f"- {file.name}")
            if file.name in ["adapter_model.pt", "adapter_model.safetensors"]:
                print(f"  Size: {file.stat().st_size} bytes")

        prepare_adapter_for_inference(MODEL_PATH, (output_dir / "epoch_1"))
        
        # delete user data after finetuning
        os.remove(data_path)
                
        return {"status": "success", "model_path": str(output_dir)}
        
    except subprocess.CalledProcessError as e:
        print(f"Fine-tuning failed with error: {e}")
        raise
    except Exception as e:
        print(f"Unexpected error during fine-tuning: {e}")
        raise
