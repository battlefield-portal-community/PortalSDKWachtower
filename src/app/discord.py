from datetime import datetime, UTC

from discord_webhook import AsyncDiscordWebhook, DiscordEmbed

from app.config import env
from app.humanbytes import HumanBytes


async def send_discord_webhook(version: str, file_size: float, old_version: str, old_size: float) -> None:
    file_size_readable = HumanBytes.format(file_size, metric=True)
    size_change = HumanBytes.format(file_size - old_size, metric=True, precision=3)
    if old_size < file_size:
        size_change = f"+{size_change}"

    allowed_mentions = {
        "roles": ["916729041002852363", "916779659239239691"]
    }
    content = "New Portal SDK Version Available! <@&916729041002852363>"
    webhook = AsyncDiscordWebhook(url=env.discord_webhook_url, allowed_mentions=allowed_mentions, content=content )
    embed = DiscordEmbed(username="Portal SDK Watchtower", color=0x00ff00)
    embed.set_thumbnail(url="https://lis.bfportal.gg/portal-animation-logo.gif")
    embed.add_embed_field(name="New Version", value=f"`{old_version} -> {version}`")
    embed.add_embed_field(name="File Size", value=f"{file_size_readable}")
    embed.add_embed_field(name="", value="[Download](https://download.portal.battlefield.com/PortalSDK.zip)", inline=False)
    if file_size - old_size != 0:
        embed.add_embed_field(name="Size Change", value=f"`{size_change}`")
    embed.set_timestamp(datetime.now(UTC))
    embed.set_footer(text="Portal SDK Watchtower")

    webhook.add_embed(embed)
    await webhook.execute()
    print("Notification send to discord....")
