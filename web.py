from aiohttp import web

import database as db


async def serve_landing(request):
    return web.FileResponse("landing/index.html")


async def serve_commands(request):
    return web.FileResponse("landing/commands.html")


async def serve_leaderboard(request):
    return web.FileResponse("landing/leaderboard.html")


async def serve_tech(request):
    return web.FileResponse("landing/tech.html")


async def leaderboard_api(request):
    tippers = await db.get_top_tippers_by_count(10)
    receivers = await db.get_top_receivers_by_count(10)

    return web.json_response({
        "tippers": [
            {"id": row[0], "name": f"User#{str(row[0])[-4:]}", "count": row[1]}
            for row in tippers
        ],
        "receivers": [
            {"id": row[0], "name": f"User#{str(row[0])[-4:]}", "count": row[1]}
            for row in receivers
        ],
    })


def create_app():
    app = web.Application()
    app.router.add_get("/", serve_landing)
    app.router.add_get("/commands", serve_commands)
    app.router.add_get("/leaderboard", serve_leaderboard)
    app.router.add_get("/tech", serve_tech)
    app.router.add_get("/api/leaderboard", leaderboard_api)
    return app


async def start_web(port=8080):
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
