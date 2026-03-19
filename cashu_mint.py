"""
DiscordCash — Cashu-style blind token mint backed by Zcash.

Implements simplified BDHKE (Blind Diffie-Hellman Key Exchange) on secp256k1
for a Discord tip bot. Covers NUT-00 through NUT-05 equivalent operations:
  - Blind signatures (mint issuance)
  - Token swaps (for tipping)
  - Melt (burn tokens for withdrawal)

All amounts are in "zats" (1 zat = 0.0001 ZEC), always integers.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import aiosqlite
from coincurve import PrivateKey, PublicKey

DB_PATH = "tipbot.db"

# Supported denominations in zats (powers of 2)
DENOMINATIONS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

# Domain separator for hash_to_curve to avoid collisions
_DOMAIN_SEPARATOR = b"Secp256k1_HashToCurve_Cashu_"


# ── Elliptic Curve Helpers ─────────────────────────────────────────────


def _int_to_bytes_32(n: int) -> bytes:
    return n.to_bytes(32, "big")


def hash_to_curve(message: bytes) -> PublicKey:
    """
    Deterministically map arbitrary bytes to a point on secp256k1.

    Tries successive SHA-256 hashes with an incrementing counter until the
    resulting x-coordinate corresponds to a valid curve point (with even y,
    i.e. compressed prefix 0x02).
    """
    msg_hash = hashlib.sha256(_DOMAIN_SEPARATOR + message).digest()
    for counter in range(2**16):
        candidate = hashlib.sha256(msg_hash + counter.to_bytes(4, "big")).digest()
        # Try interpreting as compressed point with 0x02 prefix (even y)
        compressed = b"\x02" + candidate
        try:
            point = PublicKey(compressed)
            return point
        except Exception:
            continue
    raise ValueError("hash_to_curve: failed to find valid point")


def _point_add(p1: PublicKey, p2: PublicKey) -> PublicKey:
    """Add two secp256k1 points."""
    return PublicKey.combine_keys([p1, p2])


def _point_mul(scalar: PrivateKey, point: PublicKey) -> PublicKey:
    """Multiply a curve point by a scalar (private key)."""
    # coincurve: PublicKey.multiply(scalar_bytes)
    return PublicKey(point.multiply(scalar.secret))


# ── BDHKE Protocol ─────────────────────────────────────────────────────


def step1_alice(secret: bytes, blinding_factor: Optional[PrivateKey] = None):
    """
    Alice (user) blinds her secret.

    Returns:
        B_: PublicKey  — the blinded message
        r:  PrivateKey — the blinding factor (kept secret by Alice)
    """
    Y = hash_to_curve(secret)
    if blinding_factor is None:
        r = PrivateKey(secrets.token_bytes(32))
    else:
        r = blinding_factor
    # B_ = Y + r*G
    rG = r.public_key  # r * G
    B_ = _point_add(Y, rG)
    return B_, r


def step2_bob(B_: PublicKey, private_key: PrivateKey) -> PublicKey:
    """
    Bob (mint) signs the blinded message.

    Returns:
        C_: PublicKey — the blind signature
    """
    # C_ = k * B_
    C_ = _point_mul(private_key, B_)
    return C_


def step3_alice(C_: PublicKey, r: PrivateKey, mint_pubkey: PublicKey) -> PublicKey:
    """
    Alice unblinds the signature.

    C = C_ - r*K  where K is the mint's public key for this denomination.

    Returns:
        C: PublicKey — the unblinded signature (the token)
    """
    # r * K
    rK = _point_mul(r, mint_pubkey)
    # C = C_ - r*K  =>  C = C_ + (-(r*K))
    # Negate rK: flip the y-coordinate by toggling the prefix byte
    rK_bytes = rK.format(compressed=True)
    if rK_bytes[0] == 0x02:
        neg_rK_bytes = b"\x03" + rK_bytes[1:]
    else:
        neg_rK_bytes = b"\x02" + rK_bytes[1:]
    neg_rK = PublicKey(neg_rK_bytes)
    C = _point_add(C_, neg_rK)
    return C


def verify_token(secret: bytes, C: PublicKey, private_key: PrivateKey) -> bool:
    """
    Mint verifies a token: check that C == k * hash_to_curve(secret).
    """
    Y = hash_to_curve(secret)
    expected = _point_mul(private_key, Y)
    return C.format() == expected.format()


# ── Data Classes ───────────────────────────────────────────────────────


@dataclass
class BlindedMessage:
    """User sends this to the mint to request a signature."""
    amount: int
    B_: str  # hex-encoded compressed public key
    keyset_id: str


@dataclass
class BlindSignature:
    """Mint returns this after signing."""
    amount: int
    C_: str  # hex-encoded compressed public key
    keyset_id: str


@dataclass
class Proof:
    """A spendable token (unblinded)."""
    amount: int
    secret: str      # hex-encoded secret bytes
    C: str           # hex-encoded compressed public key
    keyset_id: str

    def to_dict(self) -> dict:
        return {
            "amount": self.amount,
            "secret": self.secret,
            "C": self.C,
            "keyset_id": self.keyset_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Proof:
        return cls(
            amount=d["amount"],
            secret=d["secret"],
            C=d["C"],
            keyset_id=d["keyset_id"],
        )


# ── Keyset ─────────────────────────────────────────────────────────────


def _derive_keyset_id(pubkeys: dict[int, str]) -> str:
    """
    Derive keyset ID from public keys.
    SHA-256 of the sorted concatenated pubkey hex strings,
    take first 7 bytes, hex-encode, prefix with '00'.
    """
    concat = "".join(pubkeys[d] for d in sorted(pubkeys.keys()))
    digest = hashlib.sha256(concat.encode()).digest()
    return "00" + digest[:7].hex()


@dataclass
class MintKeyset:
    id: str
    private_keys: dict[int, PrivateKey]   # denomination -> PrivateKey
    public_keys: dict[int, PublicKey]      # denomination -> PublicKey
    active: bool = True

    @classmethod
    def generate(cls) -> MintKeyset:
        """Generate a fresh keyset with one key per denomination."""
        private_keys = {}
        public_keys = {}
        pubkey_hexes = {}
        for denom in DENOMINATIONS:
            sk = PrivateKey(secrets.token_bytes(32))
            private_keys[denom] = sk
            public_keys[denom] = sk.public_key
            pubkey_hexes[denom] = sk.public_key.format().hex()
        keyset_id = _derive_keyset_id(pubkey_hexes)
        return cls(
            id=keyset_id,
            private_keys=private_keys,
            public_keys=public_keys,
            active=True,
        )

    @classmethod
    def from_private_key_dict(cls, keyset_id: str, pk_dict: dict[str, str], active: bool = True) -> MintKeyset:
        """Reconstruct from stored private key hex values. Keys are denomination strings."""
        private_keys = {}
        public_keys = {}
        for denom_str, sk_hex in pk_dict.items():
            denom = int(denom_str)
            sk = PrivateKey(bytes.fromhex(sk_hex))
            private_keys[denom] = sk
            public_keys[denom] = sk.public_key
        return cls(
            id=keyset_id,
            private_keys=private_keys,
            public_keys=public_keys,
            active=active,
        )

    def serialize_private_keys(self) -> str:
        """Serialize private keys to JSON for DB storage."""
        return json.dumps({
            str(denom): sk.secret.hex() for denom, sk in self.private_keys.items()
        })

    def get_public_keys_hex(self) -> dict[int, str]:
        """Return {denomination: pubkey_hex} for sharing with wallets."""
        return {
            denom: pk.format().hex() for denom, pk in self.public_keys.items()
        }


# ── Amount Decomposition ──────────────────────────────────────────────


def amount_to_denominations(amount: int) -> list[int]:
    """
    Decompose an integer amount into a list of power-of-2 denominations.
    E.g. 13 -> [1, 4, 8]
    """
    if amount <= 0:
        return []
    denoms = []
    for d in sorted(DENOMINATIONS, reverse=True):
        while amount >= d:
            denoms.append(d)
            amount -= d
    if amount > 0:
        raise ValueError(f"Cannot exactly decompose amount; remainder={amount}")
    return sorted(denoms)


# ── Database Setup ─────────────────────────────────────────────────────


async def init_cashu_db():
    """Create tables for the blind token system. Call alongside init_db()."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS spent_secrets (
                secret TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mint_keysets (
                id TEXT PRIMARY KEY,
                private_keys TEXT,
                active BOOLEAN DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                discord_id INTEGER,
                secret TEXT,
                C TEXT,
                amount INTEGER,
                keyset_id TEXT,
                PRIMARY KEY (discord_id, secret)
            )
        """)
        await db.commit()


# ── Mint ───────────────────────────────────────────────────────────────


class Mint:
    """
    The DiscordCash mint. Holds keysets, signs blinded messages,
    validates and swaps proofs, and tracks spent secrets.
    """

    def __init__(self):
        self.keysets: dict[str, MintKeyset] = {}
        self.active_keyset_id: Optional[str] = None

    async def setup(self):
        """Load or generate keyset. Call once at startup after init_cashu_db()."""
        await init_cashu_db()
        await self._load_keysets()
        if not self.keysets:
            keyset = MintKeyset.generate()
            await self._save_keyset(keyset)
            self.keysets[keyset.id] = keyset
            self.active_keyset_id = keyset.id
        else:
            # Use the first active keyset
            for kid, ks in self.keysets.items():
                if ks.active:
                    self.active_keyset_id = kid
                    break
            if self.active_keyset_id is None:
                # All deactivated — generate a new one
                keyset = MintKeyset.generate()
                await self._save_keyset(keyset)
                self.keysets[keyset.id] = keyset
                self.active_keyset_id = keyset.id

    @property
    def active_keyset(self) -> MintKeyset:
        return self.keysets[self.active_keyset_id]

    def get_public_keys(self, keyset_id: Optional[str] = None) -> dict[int, str]:
        """Return public keys for a keyset (default: active)."""
        kid = keyset_id or self.active_keyset_id
        return self.keysets[kid].get_public_keys_hex()

    def get_keyset_ids(self) -> list[str]:
        return list(self.keysets.keys())

    # ── Core operations ────────────────────────────────────────────────

    async def mint_tokens(self, blinded_messages: list[BlindedMessage]) -> list[BlindSignature]:
        """
        Sign blinded messages (NUT-04: mint tokens after deposit).

        The caller is responsible for verifying the deposit matches the total
        amount of the blinded messages before calling this.

        Returns a list of BlindSignatures.
        """
        signatures = []
        for bm in blinded_messages:
            keyset = self.keysets.get(bm.keyset_id)
            if keyset is None:
                raise ValueError(f"Unknown keyset: {bm.keyset_id}")
            if bm.amount not in keyset.private_keys:
                raise ValueError(f"Invalid denomination: {bm.amount}")

            B_ = PublicKey(bytes.fromhex(bm.B_))
            sk = keyset.private_keys[bm.amount]
            C_ = step2_bob(B_, sk)
            signatures.append(BlindSignature(
                amount=bm.amount,
                C_=C_.format().hex(),
                keyset_id=bm.keyset_id,
            ))
        return signatures

    async def swap_tokens(
        self,
        inputs: list[Proof],
        blinded_outputs: list[BlindedMessage],
    ) -> list[BlindSignature]:
        """
        Swap tokens (NUT-03): validate input proofs, mark as spent,
        sign new blinded outputs. Input sum must equal output sum.

        This is the core operation for tipping: sender provides their proofs
        as inputs, and the recipient's blinded messages as outputs.
        """
        input_total = sum(p.amount for p in inputs)
        output_total = sum(bm.amount for bm in blinded_outputs)
        if input_total != output_total:
            raise ValueError(
                f"Input sum ({input_total}) != output sum ({output_total})"
            )

        # Validate all inputs first (atomic: either all succeed or none)
        for proof in inputs:
            keyset = self.keysets.get(proof.keyset_id)
            if keyset is None:
                raise ValueError(f"Unknown keyset: {proof.keyset_id}")
            if proof.amount not in keyset.private_keys:
                raise ValueError(f"Invalid denomination: {proof.amount}")

            secret_bytes = bytes.fromhex(proof.secret)
            C = PublicKey(bytes.fromhex(proof.C))
            sk = keyset.private_keys[proof.amount]

            if not verify_token(secret_bytes, C, sk):
                raise ValueError("Invalid proof: signature verification failed")

        # Check none are already spent
        async with aiosqlite.connect(DB_PATH) as db:
            for proof in inputs:
                cursor = await db.execute(
                    "SELECT 1 FROM spent_secrets WHERE secret = ?",
                    (proof.secret,),
                )
                if await cursor.fetchone():
                    raise ValueError(f"Token already spent: {proof.secret[:16]}...")

            # Mark all as spent
            for proof in inputs:
                await db.execute(
                    "INSERT INTO spent_secrets (secret) VALUES (?)",
                    (proof.secret,),
                )
            await db.commit()

        # Sign outputs
        return await self.mint_tokens(blinded_outputs)

    async def melt_tokens(self, proofs: list[Proof]) -> int:
        """
        Melt tokens (NUT-05): validate proofs, mark as spent, return
        the total amount in zats for withdrawal.
        """
        total = 0
        # Validate
        for proof in proofs:
            keyset = self.keysets.get(proof.keyset_id)
            if keyset is None:
                raise ValueError(f"Unknown keyset: {proof.keyset_id}")
            if proof.amount not in keyset.private_keys:
                raise ValueError(f"Invalid denomination: {proof.amount}")

            secret_bytes = bytes.fromhex(proof.secret)
            C = PublicKey(bytes.fromhex(proof.C))
            sk = keyset.private_keys[proof.amount]

            if not verify_token(secret_bytes, C, sk):
                raise ValueError("Invalid proof: signature verification failed")
            total += proof.amount

        # Check and mark spent
        async with aiosqlite.connect(DB_PATH) as db:
            for proof in proofs:
                cursor = await db.execute(
                    "SELECT 1 FROM spent_secrets WHERE secret = ?",
                    (proof.secret,),
                )
                if await cursor.fetchone():
                    raise ValueError(f"Token already spent: {proof.secret[:16]}...")

            for proof in proofs:
                await db.execute(
                    "INSERT INTO spent_secrets (secret) VALUES (?)",
                    (proof.secret,),
                )
            await db.commit()

        return total

    async def check_spendable(self, secrets_hex: list[str]) -> list[bool]:
        """Check which secrets have NOT been spent (True = spendable)."""
        results = []
        async with aiosqlite.connect(DB_PATH) as db:
            for s in secrets_hex:
                cursor = await db.execute(
                    "SELECT 1 FROM spent_secrets WHERE secret = ?", (s,)
                )
                results.append(await cursor.fetchone() is None)
        return results

    # ── Keyset persistence ─────────────────────────────────────────────

    async def _load_keysets(self):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM mint_keysets")
            rows = await cursor.fetchall()
            for row in rows:
                pk_dict = json.loads(row["private_keys"])
                keyset = MintKeyset.from_private_key_dict(
                    row["id"], pk_dict, bool(row["active"])
                )
                self.keysets[keyset.id] = keyset

    async def mint_from_amount(
        self, amount_zats: int, blinded_messages: list[BlindedMessage]
    ) -> list[BlindSignature]:
        """
        Mint tokens from a known amount (e.g. tip claim or paid quote).

        Verifies the blinded message amounts sum to the given amount,
        then signs them. Used for tip claiming where there are no input proofs.
        """
        output_total = sum(bm.amount for bm in blinded_messages)
        if output_total != amount_zats:
            raise ValueError(
                f"Output sum ({output_total}) != expected amount ({amount_zats})"
            )
        return await self.mint_tokens(blinded_messages)

    async def _save_keyset(self, keyset: MintKeyset):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO mint_keysets (id, private_keys, active) VALUES (?, ?, ?)",
                (keyset.id, keyset.serialize_private_keys(), keyset.active),
            )
            await db.commit()


# ── Wallet (server-side, manages user tokens) ─────────────────────────


class Wallet:
    """Server-side wallet that handles blinding, unblinding, and proof storage."""

    def __init__(self, mint: Mint):
        self.mint = mint

    async def mint_tokens(self, amount_zats: int) -> list[Proof]:
        """Full mint flow: blind, sign, unblind."""
        kid = self.mint.active_keyset_id
        denoms = amount_to_denominations(amount_zats)
        blinded_messages = []
        secrets_and_rs = []

        for d in denoms:
            secret = secrets.token_bytes(32)
            B_, r = step1_alice(secret)
            blinded_messages.append(BlindedMessage(
                amount=d, B_=B_.format().hex(), keyset_id=kid,
            ))
            secrets_and_rs.append((secret, r))

        blind_sigs = await self.mint.mint_tokens(blinded_messages)

        proofs = []
        for sig, (secret, r) in zip(blind_sigs, secrets_and_rs):
            keyset = self.mint.keysets[sig.keyset_id]
            mint_pubkey = keyset.public_keys[sig.amount]
            C_ = PublicKey(bytes.fromhex(sig.C_))
            C = step3_alice(C_, r, mint_pubkey)
            proofs.append(Proof(
                amount=sig.amount, secret=secret.hex(),
                C=C.format().hex(), keyset_id=sig.keyset_id,
            ))
        return proofs

    async def prepare_send(self, proofs: list[Proof], send_amount: int) -> tuple[list[Proof], list[Proof]]:
        """Select proofs, swap for exact change. Returns (send_proofs, keep_proofs)."""
        selected = []
        selected_total = 0
        remaining = sorted(proofs, key=lambda p: p.amount, reverse=True)

        for p in remaining:
            if selected_total >= send_amount:
                break
            selected.append(p)
            selected_total += p.amount

        if selected_total < send_amount:
            raise ValueError(f"Insufficient tokens: have {selected_total}, need {send_amount}")

        keep_from_unselected = [p for p in proofs if p not in selected]
        change_amount = selected_total - send_amount

        kid = self.mint.active_keyset_id
        send_denoms = amount_to_denominations(send_amount)
        change_denoms = amount_to_denominations(change_amount) if change_amount > 0 else []

        send_blinded, send_secrets_rs = [], []
        for d in send_denoms:
            secret = secrets.token_bytes(32)
            B_, r = step1_alice(secret)
            send_blinded.append(BlindedMessage(amount=d, B_=B_.format().hex(), keyset_id=kid))
            send_secrets_rs.append((secret, r))

        change_blinded, change_secrets_rs = [], []
        for d in change_denoms:
            secret = secrets.token_bytes(32)
            B_, r = step1_alice(secret)
            change_blinded.append(BlindedMessage(amount=d, B_=B_.format().hex(), keyset_id=kid))
            change_secrets_rs.append((secret, r))

        all_blinded = send_blinded + change_blinded
        all_secrets_rs = send_secrets_rs + change_secrets_rs

        blind_sigs = await self.mint.swap_tokens(selected, all_blinded)

        all_proofs = []
        for sig, (secret, r) in zip(blind_sigs, all_secrets_rs):
            keyset = self.mint.keysets[sig.keyset_id]
            mint_pubkey = keyset.public_keys[sig.amount]
            C_ = PublicKey(bytes.fromhex(sig.C_))
            C = step3_alice(C_, r, mint_pubkey)
            all_proofs.append(Proof(
                amount=sig.amount, secret=secret.hex(),
                C=C.format().hex(), keyset_id=sig.keyset_id,
            ))

        send_proofs = all_proofs[:len(send_blinded)]
        change_proofs = all_proofs[len(send_blinded):]
        return send_proofs, keep_from_unselected + change_proofs

    async def receive(self, proofs: list[Proof]) -> list[Proof]:
        """Swap received proofs for fresh ones (prevents double-spend by sender)."""
        kid = self.mint.active_keyset_id
        blinded, secrets_rs = [], []
        for p in proofs:
            secret = secrets.token_bytes(32)
            B_, r = step1_alice(secret)
            blinded.append(BlindedMessage(amount=p.amount, B_=B_.format().hex(), keyset_id=kid))
            secrets_rs.append((secret, r))

        blind_sigs = await self.mint.swap_tokens(proofs, blinded)

        new_proofs = []
        for sig, (secret, r) in zip(blind_sigs, secrets_rs):
            keyset = self.mint.keysets[sig.keyset_id]
            mint_pubkey = keyset.public_keys[sig.amount]
            C_ = PublicKey(bytes.fromhex(sig.C_))
            C = step3_alice(C_, r, mint_pubkey)
            new_proofs.append(Proof(
                amount=sig.amount, secret=secret.hex(),
                C=C.format().hex(), keyset_id=sig.keyset_id,
            ))
        return new_proofs

    async def melt(self, proofs: list[Proof]) -> int:
        """Burn tokens for withdrawal. Returns total zats."""
        return await self.mint.melt_tokens(proofs)

    # ── Proof storage ──────────────────────────────────────────────────

    @staticmethod
    async def save_proofs(discord_id: int, proofs: list[Proof]):
        async with aiosqlite.connect(DB_PATH) as db:
            for p in proofs:
                await db.execute(
                    "INSERT OR REPLACE INTO user_tokens "
                    "(discord_id, secret, C, amount, keyset_id) VALUES (?, ?, ?, ?, ?)",
                    (discord_id, p.secret, p.C, p.amount, p.keyset_id),
                )
            await db.commit()

    @staticmethod
    async def load_proofs(discord_id: int) -> list[Proof]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT secret, C, amount, keyset_id FROM user_tokens WHERE discord_id = ?",
                (discord_id,),
            )
            return [
                Proof(amount=row["amount"], secret=row["secret"],
                      C=row["C"], keyset_id=row["keyset_id"])
                for row in await cursor.fetchall()
            ]

    @staticmethod
    async def delete_proofs(discord_id: int, secrets_hex: list[str]):
        async with aiosqlite.connect(DB_PATH) as db:
            for s in secrets_hex:
                await db.execute(
                    "DELETE FROM user_tokens WHERE discord_id = ? AND secret = ?",
                    (discord_id, s),
                )
            await db.commit()

    @staticmethod
    async def get_balance(discord_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM user_tokens WHERE discord_id = ?",
                (discord_id,),
            )
            return int((await cursor.fetchone())[0])
