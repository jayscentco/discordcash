import httpx
import config


class ZcashClient:
    """Wrapper around zcashd JSON-RPC interface."""

    def __init__(self):
        self.url = config.ZCASH_RPC_URL
        self.auth = (config.ZCASH_RPC_USER, config.ZCASH_RPC_PASSWORD)

    async def _rpc(self, method: str, params: list = None) -> dict:
        payload = {
            "jsonrpc": "1.0",
            "id": "tipbot",
            "method": method,
            "params": params or [],
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.url, json=payload, auth=self.auth, timeout=30.0
            )
            data = resp.json()
            if data.get("error"):
                raise Exception(f"Zcash RPC error: {data['error']}")
            return data["result"]

    async def get_new_transparent_address(self) -> str:
        """Generate a new transparent (t-addr) deposit address."""
        return await self._rpc("getnewaddress")

    async def get_new_shielded_address(self) -> str:
        """Generate a new shielded (z-addr)."""
        return await self._rpc("z_getnewaddress", ["sapling"])

    async def get_balance(self) -> float:
        """Get total wallet balance."""
        return await self._rpc("z_gettotalbalance")

    async def send_shielded(self, from_addr: str, to_addr: str, amount: float) -> str:
        """Send ZEC from a shielded address. Returns operation ID."""
        recipients = [{"address": to_addr, "amount": amount}]
        opid = await self._rpc("z_sendmany", [from_addr, recipients, 1, 0.0001])
        return opid

    async def send_transparent(self, to_addr: str, amount: float) -> str:
        """Send ZEC to a transparent address. Returns txid."""
        return await self._rpc("sendtoaddress", [to_addr, amount])

    async def get_operation_status(self, opid: str) -> dict:
        """Check status of an async shielded operation."""
        results = await self._rpc("z_getoperationstatus", [[opid]])
        return results[0] if results else None

    async def list_received_by_address(self, address: str, min_conf: int = 1) -> list:
        """List transactions received by a transparent address."""
        return await self._rpc("listreceivedbyaddress", [min_conf, False, address])

    async def validate_address(self, address: str) -> bool:
        """Check if a zcash address is valid (transparent or shielded)."""
        if address.startswith("z"):
            result = await self._rpc("z_validateaddress", [address])
        else:
            result = await self._rpc("validateaddress", [address])
        return result.get("isvalid", False)


# Use mock client if MOCK_MODE is enabled
if config.MOCK_MODE:
    from zcash_mock import MockZcashClient
    zcash = MockZcashClient()
else:
    zcash = ZcashClient()
