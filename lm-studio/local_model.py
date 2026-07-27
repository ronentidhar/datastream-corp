"""Local model backend (LM Studio, OpenAI-compatible API).

The workshop (see ../aws-Strands) ran on Amazon Bedrock, which is Strands' default when
an Agent is created without a `model=`. To run off AWS we point Strands at
LM Studio's local OpenAI-compatible server instead.

Prereqs:
  lms server start
  lms load google/gemma-4-e4b   # fast + reliable tool-caller; fits 24GB RAM easily.
                                # google/gemma-4-26b-a4b is more precise but heavy/slow.
                                # Llama 3.1 8B loops here (no reliable termination).

Override the model/endpoint with env vars if needed:
  LOCAL_MODEL_ID   (default: google/gemma-4-e4b)
  LOCAL_MODEL_URL  (default: http://localhost:1234/v1)
"""

import logging
import os

from strands.models.openai import OpenAIModel

# Gemma 4 emits internal reasoning tokens. Strands can't replay that block to
# LM Studio's Chat Completions API on later turns, so it logs a warning on every
# multi-turn message. The answers are unaffected — silence just this one logger.
logging.getLogger("strands.models.openai").setLevel(logging.ERROR)


def get_model() -> OpenAIModel:
    """Build a Strands model backed by the local LM Studio server."""
    return OpenAIModel(
        client_args={
            "api_key": "lm-studio",  # LM Studio ignores the value but requires one
            "base_url": os.environ.get("LOCAL_MODEL_URL", "http://localhost:1234/v1"),
        },
        model_id=os.environ.get("LOCAL_MODEL_ID", "google/gemma-4-e4b"),
        params={"temperature": 0.2},
    )
