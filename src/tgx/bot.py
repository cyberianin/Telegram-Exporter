from __future__ import annotations

import json
import os
from urllib.parse import quote


async def run_bot() -> None:
    import httpx
    from aiogram import Bot, Dispatcher
    from aiogram.filters import Command
    from aiogram.types import BufferedInputFile, Message

    token = os.environ["TGX_BOT_TOKEN"]
    allowlist = {
        int(item.strip()) for item in os.environ["TGX_BOT_ALLOWLIST"].split(",") if item.strip()
    }
    if not allowlist:
        raise ValueError("Bot allowlist must not be empty")
    base_url = os.getenv("TGX_API_URL", "http://127.0.0.1:8765")
    owner = os.environ["TGX_OWNER_TOKEN"]
    profiles: dict[int, str] = {}
    settings: dict[int, dict] = {}
    dispatcher = Dispatcher()

    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {owner}"}, timeout=120
    ) as client:

        @dispatcher.message(
            Command(
                "start",
                "help",
                "account",
                "accounts",
                "dialogs",
                "inspect",
                "export",
                "jobs",
                "status",
                "pause",
                "resume",
                "cancel",
                "settings",
                "files",
            )
        )
        async def handle(message: Message):
            if not message.from_user or message.from_user.id not in allowlist:
                return
            user = message.from_user.id
            pieces = (message.text or "").split(maxsplit=1)
            command = pieces[0].split("@")[0].lstrip("/")
            argument = pieces[1] if len(pieces) > 1 else ""
            account = profiles.get(user, "main")
            try:
                if command in ("start", "help"):
                    await message.answer(
                        "/account main\n/accounts\n/dialogs\n/inspect PEER\n"
                        '/export PEER\n/settings {"media":"full"}\n'
                        "/jobs\n/status ID\n/pause ID\n/resume ID\n/cancel ID\n/files ID"
                    )
                    return
                if command == "account":
                    from .service import profile_paths

                    profile_paths(argument)
                    profiles[user] = argument
                    await message.answer(f"Profile: {argument}")
                    return
                if command == "settings":
                    from .options import ExportOptions

                    settings[user] = ExportOptions.model_validate_json(argument).model_dump()
                    await message.answer("Settings saved for this bot session")
                    return
                if command == "export":
                    response = await client.post(
                        "/api/v1/exports",
                        json={
                            "account": account,
                            "peer": argument,
                            "options": settings.get(user, {}),
                        },
                    )
                elif command in ("pause", "resume", "cancel"):
                    response = await client.post(
                        f"/api/v1/exports/{quote(argument, safe='')}/{command}"
                    )
                elif command == "files":
                    response = await client.get(
                        f"/api/v1/exports/{quote(argument, safe='')}/artifacts"
                    )
                    response.raise_for_status()
                    for artifact in response.json():
                        if artifact["size"] > 40 * 1024 * 1024:
                            public = os.getenv("TGX_PUBLIC_URL", base_url).rstrip("/")
                            await message.answer(
                                f"{artifact['name']} (valid 15 minutes):\n"
                                + public
                                + artifact["download_url"]
                            )
                            continue
                        downloaded = await client.get(artifact["url"])
                        downloaded.raise_for_status()
                        await message.answer_document(
                            BufferedInputFile(
                                downloaded.content, filename=artifact["name"].split("/")[-1]
                            )
                        )
                    return
                else:
                    path = {
                        "accounts": "accounts",
                        "dialogs": "dialogs",
                        "jobs": "exports",
                        "status": f"exports/{quote(argument, safe='')}",
                        "inspect": "inspect",
                    }[command]
                    params = {"account": account}
                    if command == "inspect":
                        params["peer"] = argument
                    response = await client.get("/api/v1/" + path, params=params)
                response.raise_for_status()
                text = json.dumps(response.json(), ensure_ascii=False, indent=2)
                await message.answer(text[:3900])
            except Exception as error:
                await message.answer(f"{type(error).__name__}: command failed; check server status")

        async with Bot(token) as bot:
            await dispatcher.start_polling(bot)
