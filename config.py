import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TIP_EMOJI = os.getenv("TIP_EMOJI", "💰")
DEFAULT_TIP_AMOUNT = float(os.getenv("DEFAULT_TIP_AMOUNT", "0.01"))

ZCASH_RPC_URL = os.getenv("ZCASH_RPC_URL", "http://127.0.0.1:8232")
ZCASH_RPC_USER = os.getenv("ZCASH_RPC_USER", "")
ZCASH_RPC_PASSWORD = os.getenv("ZCASH_RPC_PASSWORD", "")

MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "0.001"))
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
