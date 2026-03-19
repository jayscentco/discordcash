import uuid
import logging

log = logging.getLogger("tipbot.mock")


class MockZcashClient:
    """Fake Zcash client for testing without a node. All operations succeed instantly."""

    def __init__(self):
        self._addr_counter = 0
        self._ops = {}
        log.info("Running in MOCK MODE — no real ZEC will be sent")

    async def get_new_transparent_address(self) -> str:
        self._addr_counter += 1
        return f"t1MockAddr{self._addr_counter:06d}xxxxxxxxxxxxxxxxxx"

    async def get_new_shielded_address(self) -> str:
        self._addr_counter += 1
        return f"zs1mock{self._addr_counter:06d}{'x' * 60}"

    async def get_balance(self) -> dict:
        return {"transparent": "100.0", "private": "100.0", "total": "200.0"}

    async def send_shielded(self, from_addr: str, to_addr: str, amount: float) -> str:
        opid = f"opid-mock-{uuid.uuid4().hex[:12]}"
        self._ops[opid] = {
            "status": "success",
            "result": {"txid": f"mock-txid-{uuid.uuid4().hex[:16]}"},
        }
        log.info(f"[MOCK] Shielded send {amount} ZEC to {to_addr} — op: {opid}")
        return opid

    async def send_transparent(self, to_addr: str, amount: float) -> str:
        txid = f"mock-txid-{uuid.uuid4().hex[:16]}"
        log.info(f"[MOCK] Transparent send {amount} ZEC to {to_addr} — txid: {txid}")
        return txid

    async def get_operation_status(self, opid: str) -> dict | None:
        return self._ops.get(opid)

    async def list_received_by_address(self, address: str, min_conf: int = 1) -> list:
        # No mock deposits — use /mockdeposit command instead
        return []

    async def validate_address(self, address: str) -> bool:
        # Accept anything that looks vaguely like a zcash address
        return address.startswith("t") or address.startswith("z")
