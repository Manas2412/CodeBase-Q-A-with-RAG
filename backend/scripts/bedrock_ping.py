"""Minimal Bedrock connectivity test — bypasses the app wrapper entirely.

Verifies (in order):
  1. .env loads and the four required env vars are present
  2. boto3 can construct a bedrock-runtime client with those creds
  3. The configured Bedrock model accepts an invoke_model call
  4. The Anthropic Messages body format we use is wire-correct
  5. The model's reply parses out cleanly

Run from backend/ with the venv active:
    uv run python scripts/bedrock_ping.py

If THIS script fails, the problem is one of:
    - credentials don't have bedrock:InvokeModel permission
    - the principal is the root user (Bedrock runtime blocks root)
    - the model isn't enabled in Bedrock → Model access for this region
    - the model id needs a cross-region inference profile prefix
      (us./eu./apac. for Claude Opus/Sonnet 4.x)

Fix the underlying issue, re-run this script, then move on to the
application-level smoke (which uses the same call shape).
"""

from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


def fail(msg: str) -> None:
    print(f"\n❌ {msg}\n")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def main() -> None:
    # ── 1. Load env ────────────────────────────────────────────────
    load_dotenv()

    ak = os.getenv("AWS_ACCESS_KEY_ID")
    sk = os.getenv("AWS_SECRET_ACCESS_KEY")
    region = os.getenv("AWS_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_LLM_MODEL", "us.anthropic.claude-opus-4-6-v1")

    if not ak or not sk:
        fail(
            "AWS_ACCESS_KEY_ID and/or AWS_SECRET_ACCESS_KEY missing from .env.\n"
            "   These are required for explicit boto3 credentials. "
            "If you intend to use the AWS credential chain (IAM role, ~/.aws/credentials),\n"
            "   comment out this check and re-run."
        )
    ok(f"env loaded   region={region!r}  model={model_id!r}")
    ok(f"             AWS_ACCESS_KEY_ID={ak[:6]}…  (length {len(ak)})")

    # ── 2. Construct client ───────────────────────────────────────
    try:
        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
        )
    except Exception as e:
        fail(f"boto3.client(bedrock-runtime) construction failed: {e}")
    ok("boto3 bedrock-runtime client constructed")

    # ── 3. Verify caller identity (lightweight permission probe) ──
    try:
        sts = boto3.client(
            "sts",
            region_name=region,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
        )
        identity = sts.get_caller_identity()
        arn = identity.get("Arn", "?")
        ok(f"identity     {arn}")
        if arn.endswith(":root"):
            print(
                "\n⚠️  WARNING: you're calling Bedrock with ROOT credentials.\n"
                "   AWS hard-blocks root from bedrock-runtime — invoke_model below\n"
                "   will return 'Operation not allowed' even when the model + region\n"
                "   are correctly configured. Create an IAM user with\n"
                "   AmazonBedrockFullAccess and re-run with that user's keys.\n"
            )
    except Exception as e:
        print(f"⚠️  could not check sts caller identity: {e}")

    # ── 4. Invoke model with Anthropic Messages body ──────────────
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 20,
            "messages": [
                {
                    "role": "user",
                    "content": "Say the single word 'ping' and nothing else.",
                }
            ],
        }
    )

    try:
        resp = client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "?")
        msg = e.response.get("Error", {}).get("Message", str(e))
        fail(
            f"invoke_model failed ({code}): {msg}\n"
            "   Common causes:\n"
            "   • Operation not allowed       → root credentials (use IAM user)\n"
            "   • AccessDeniedException        → model access not enabled in console\n"
            "   • ValidationException + 'inference profile required'\n"
            f"                                  → use a cross-region profile id\n"
            f"                                    instead of {model_id!r}\n"
            "   • ResourceNotFoundException    → wrong region or model id typo"
        )
    except Exception as e:
        fail(f"invoke_model raised unexpected: {type(e).__name__}: {e}")

    # ── 5. Parse and print the reply ──────────────────────────────
    try:
        payload = json.loads(resp["body"].read())
    except Exception as e:
        fail(f"could not parse response body as JSON: {e}")

    content_blocks = payload.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    usage = payload.get("usage", {}) or {}
    stop = payload.get("stop_reason", "?")

    ok("invoke_model returned 200")
    print("\n────────────────────────────────────────────")
    print(f" reply       : {text!r}")
    print(f" stop_reason : {stop}")
    print(
        f" usage       : {usage.get('input_tokens', '?')} in / "
        f"{usage.get('output_tokens', '?')} out"
    )
    print("────────────────────────────────────────────")
    print("\n✓ Bedrock reachable end-to-end. Ready to wire into the app.\n")


if __name__ == "__main__":
    main()
