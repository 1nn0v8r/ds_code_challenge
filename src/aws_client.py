"""S3 client for the challenge bucket, using the creds the challenge publishes."""
import json
import urllib.request

import boto3
from botocore.config import Config

BUCKET = "cct-ds-code-challenge-input-data"
REGION = "af-south-1"
CREDS_URL = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/ds_code_challenge_creds.json"


def fetch_credentials(url: str = CREDS_URL, timeout: int = 15) -> dict:
    """Download the public dummy credentials at runtime (never stored in the repo)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)["s3"]


def make_s3_client():
    creds = fetch_credentials()
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=creds["access_key"],
        aws_secret_access_key=creds["secret_key"],
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )
