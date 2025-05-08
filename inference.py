from common import (
    get_user_checkpoint_path,
    SYSTEM_PROMPT,
    MODEL_PATH,
    vllm_image,
    output_vol,
    VOL_MOUNT_PATH,
    MINUTES,
    app,
)

from typing import Optional, AsyncIterator
import time
from pathlib import Path
import modal
from modal import Image, Function, Secret, Stub
from modal import asgi_app, method




# Inference Module
with vllm_image.imports():
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.engine.async_llm_engine import AsyncLLMEngine
    from vllm.lora.request import LoRARequest
    from vllm.sampling_params import SamplingParams
    from vllm.utils import random_uuid

@app.cls(
    image=vllm_image,
    gpu="L40S",
    scaledown_window=10 * MINUTES,
    timeout=5 * MINUTES,
    allow_concurrent_inputs=50,
    volumes={VOL_MOUNT_PATH: output_vol},
)
class Inference:
    @modal.enter()
    def enter(self):
        """Initialize the inference engine."""
        engine_args = AsyncEngineArgs(
            model=str(MODEL_PATH) if isinstance(MODEL_PATH, Path) else MODEL_PATH,
            gpu_memory_utilization=0.95,
            tensor_parallel_size=1,
            enable_lora=True,
            enforce_eager=True,
            max_lora_rank=32,
            max_model_len=4096,
            max_loras=16,
            enable_prefix_caching=True,
        )
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        self.loras: dict[str, int] = dict()  # per replica LoRA identifier

    @modal.method()
    async def generate(
        self, 
        code_content: str, 
        file_path: str, 
        username: str, 
        repo_owner: Optional[str] = None
    ) -> AsyncIterator[str]:
        """Generate a code review comment for the given code.
        
        Args:
            code_content: The code to review
            file_path: Path to the file being reviewed
            username: GitHub username of reviewer to emulate
            repo_owner: Repository owner for model path
            
        Returns:
            Generated comment as an async stream
        """
        ident = f"{username}-{repo_owner}"
        if ident not in self.loras:
            self.loras[ident] = len(self.loras) + 1
        
        output_vol.reload()
        checkpoint_path = get_user_checkpoint_path(username, repo_owner)
        lora_request = LoRARequest(ident, self.loras[ident], lora_local_path=checkpoint_path)
        print(f"Using LoRA {lora_request} for {username}")

        tokenizer = await self.engine.get_tokenizer(lora_request=lora_request)
        
        # Prepare prompt
        conversation = [
            {"role": "system", "content": SYSTEM_PROMPT.replace("{USERNAME}", username)},
            {"role": "user", "content": f"File: {file_path}\n\nCode:\n```\n{code_content}\n```"}
        ]
        
        prompt = tokenizer.apply_chat_template(
            conversation=conversation, 
            tokenize=False, 
            add_generation_prompt=True
        )
        
        sampling_params = SamplingParams(
            repetition_penalty=1.1,
            temperature=0.2,
            top_p=0.95,
            top_k=50,
            max_tokens=1024,
        )
        
        request_id = random_uuid()
        results_generator = self.engine.generate(
            prompt,
            sampling_params,
            request_id,
            lora_request=lora_request,
        )

        t0 = time.time()
        index, tokens = 0, 0
        async for request_output in results_generator:
            yield request_output.outputs[0].text[index:]
            index = len(request_output.outputs[0].text)

        tokens = len(request_output.outputs[0].token_ids)
        throughput = tokens / (time.time() - t0)
        print(f"🧠: Effective throughput of {throughput:.2f} tok/s")
