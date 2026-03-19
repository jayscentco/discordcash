import aiosqlite

DB_PATH = "tipbot.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                deposit_address TEXT,
                zaddress TEXT,
                default_tip REAL DEFAULT 0.01,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tip_counts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user INTEGER,
                to_user INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_or_create_user(discord_id: int, deposit_address: str = None) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        await db.execute(
            "INSERT INTO users (discord_id, deposit_address) VALUES (?, ?)",
            (discord_id, deposit_address),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,))
        return dict(await cursor.fetchone())


async def get_user(discord_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_balance(discord_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount, discord_id)
        )
        await db.commit()


async def set_zaddress(discord_id: int, address: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET zaddress = ? WHERE discord_id = ?", (address, discord_id))
        await db.commit()


async def set_default_tip(discord_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET default_tip = ? WHERE discord_id = ?", (amount, discord_id))
        await db.commit()


async def transfer_balance(from_id: int, to_id: int, amount: float) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE discord_id = ?", (from_id,))
        row = await cursor.fetchone()
        if not row or row[0] < amount:
            return False
        await db.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (amount, from_id))
        await db.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount, to_id))
        await db.execute(
            "INSERT INTO tip_counts (from_user, to_user) VALUES (?, ?)", (from_id, to_id)
        )
        await db.commit()
        return True


async def record_tip_count(from_user: int, to_user: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tip_counts (from_user, to_user) VALUES (?, ?)", (from_user, to_user))
        await db.commit()


async def get_top_tippers_by_count(limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT from_user, COUNT(*) as total FROM tip_counts GROUP BY from_user ORDER BY total DESC LIMIT ?",
            (limit,),
        )
        return await cursor.fetchall()


async def get_top_receivers_by_count(limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT to_user, COUNT(*) as total FROM tip_counts GROUP BY to_user ORDER BY total DESC LIMIT ?",
            (limit,),
        )
        return await cursor.fetchall()
