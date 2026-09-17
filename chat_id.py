"""Finds the group's CHAT_ID. Run once, at setup time.

    1. Create the bot with @BotFather and put BOT_TOKEN in the .env
    2. Add the bot to the group
    3. Send any message in the group
    4. .venv/bin/python chat_id.py

Standard library only — works before installing anything.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

env = Path(__file__).resolve().parent / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

token = os.environ.get("BOT_TOKEN", "").strip()
if not token:
    raise SystemExit("Ponha o BOT_TOKEN no .env primeiro.")

url = f"https://api.telegram.org/bot{token}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
except urllib.error.HTTPError as e:
    # A wrong token comes back as 401, which urlopen raises instead of
    # returning. HTTPError is itself a readable response, so the API's own
    # explanation is right there — much better than a traceback.
    detail = ""
    try:
        detail = json.load(e).get("description", "")
    except ValueError:  # json.JSONDecodeError is a ValueError
        pass
    raise SystemExit(
        f"O Telegram recusou o token (HTTP {e.code}). {detail}\n"
        "Confira se copiou o token inteiro do @BotFather, com os dois-pontos."
    ) from None
except urllib.error.URLError as e:
    raise SystemExit(f"Não consegui falar com o Telegram: {e.reason}") from None

if not data.get("ok"):
    raise SystemExit(f"O Telegram recusou: {data.get('description') or data}")

seen = {}
for u in data.get("result", []):
    msg = u.get("message") or u.get("channel_post") or {}
    chat = msg.get("chat") or {}
    if chat.get("id"):
        name = chat.get("title") or chat.get("username") or ""
        seen[chat["id"]] = f"{chat.get('type')} · {name}"

if not seen:
    raise SystemExit(
        "Nenhuma mensagem encontrada. Mande algo no grupo e rode de novo.\n"
        "(Se o bot está em um grupo, desligue o modo privacidade no @BotFather:\n"
        " /setprivacy -> Disable — ou apenas mencione o bot na mensagem.)"
    )

print("Chats encontrados — copie o id do seu grupo para o .env:\n")
for chat_id, description in seen.items():
    print(f"  CHAT_ID={chat_id}    ({description})")
