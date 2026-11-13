import discord
from discord.ext import commands
import qbittorrentapi
import subprocess
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time
import math
from discord.ui import View, Button

# Work both as a package member (import bitbeard.bitbeard, as tests do) and
# as a script (python bitbeard.py, as the Dockerfile runs it — siblings are
# flat on sys.path there).
try:
    from .config import Config, load_config
    from .downloads import DownloadManager
    from .search import search
    from .discord_ui import CategorySelectView, send_error
except ImportError:  # run as a script: no parent package
    from config import Config, load_config
    from downloads import DownloadManager
    from search import search
    from discord_ui import CategorySelectView, send_error

# Set up logging
logging.basicConfig(filename='bitbeard.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Qbittorrent client setup (host and port are always localhost:8080)
def make_qbt_client(config):
    try:
        client = qbittorrentapi.Client(
            host="localhost",
            port=8080,
            username=config.qbit_username,
            password=config.qbit_password
        )
    except Exception as e:
        logging.error(f"Error setting up QBittorrent client: {e}")
        raise

    try:
        client.auth_log_in()
    except qbittorrentapi.LoginFailed as e:
        logging.error(f"Login to qBittorrent failed: {e}")
        print(f"Login to qBittorrent failed: {e}")
    return client


# Configuration, the qBittorrent client, and the DownloadManager are created
# once at startup, by startup() — importing this module must not touch the
# network.
def startup():
    """Load configuration and connect to qBittorrent. Called once from main()."""
    global config, qbt_client, download_manager
    config = load_config()
    qbt_client = make_qbt_client(config)
    download_manager = DownloadManager(
        qbt_client, config, DiscordAnnouncer(bot, config.announce_channel_id)
    )


config = None
qbt_client = None
download_manager = None


def require_config():
    if config is None:
        raise RuntimeError("Configuration not loaded; startup() must run before serving requests.")
    return config


class DiscordAnnouncer:
    """Announcer adapter that posts lifecycle events to the announce channel."""

    def __init__(self, bot, channel_id):
        self._bot = bot
        self._channel_id = channel_id

    async def _send(self, message):
        logging.info(message)
        channel = self._bot.get_channel(self._channel_id)
        if channel is None:
            logging.warning(
                f"Announce channel {self._channel_id} not found; "
                f"skipping announcement: {message}"
            )
            return
        try:
            await channel.send(message)
        except Exception as e:
            logging.error(f"Failed to send announcement: {e}")

    async def announce_completed(self, torrent_name, category):
        await self._send(
            f"{torrent_name} has been added to Bitbeard under {category}."
        )

    async def announce_all_done(self):
        await self._send("All downloads completed.")

# Discord bot setup
intents = discord.Intents.default()
intents.messages = True
intents.reactions = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# FastAPI setup
app = FastAPI()

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # You can restrict this to specific origins if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TorrentRequest(BaseModel):
    magnet_link: str
    category: str

class SearchRequest(BaseModel):
    query: str

@app.post("/download")
async def api_download(request: TorrentRequest):
    try:
        download_manager.submit(request.magnet_link, request.category)
        return {"message": "Download started successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search")
async def api_search(request: SearchRequest):
    results = search(request.query, config=require_config())
    return {"results": [r.as_dict() for r in results]}

@app.get("/progress")
async def api_progress():
    return {"progress": download_manager.progress()}

@app.post("/cancel")
async def api_cancel():
    download_manager.cancel_all()
    return {"message": "All torrents have been cancelled"}

@bot.event
async def on_ready():
    logging.info(f'{bot.user.name} has connected to Discord!')
    print(f'{bot.user.name} has connected to Discord!')

class SearchResultsView(View):
    def __init__(self, results, page=0):
        super().__init__(timeout=300)
        self.results = results
        self.page = page
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        start_idx = self.page * 5
        page_results = self.results[start_idx:start_idx + 5]
        
        for i, result in enumerate(page_results):
            select_button = Button(label=f"{i+1}", style=discord.ButtonStyle.primary, custom_id=f"select_{i}")
            select_button.callback = self.select_callback
            self.add_item(select_button)
            
        if self.page > 0:
            prev_button = Button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="prev")
            prev_button.callback = self.prev_callback
            self.add_item(prev_button)
            
        if (self.page + 1) * 5 < len(self.results):
            next_button = Button(label="Next", style=discord.ButtonStyle.secondary, custom_id="next")
            next_button.callback = self.next_callback
            self.add_item(next_button)
            
        cancel_button = Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel")
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)

    async def select_callback(self, interaction):
        try:
            # First acknowledge the interaction
            await interaction.response.defer(ephemeral=True)
            
            # Get the index from the button's custom_id
            idx = int(interaction.data['custom_id'].split("_")[1])
            selected_result = self.results[self.page * 5 + idx]

            async def submit_download(magnet_link, category):
                download_manager.submit(magnet_link, category)

            category_view = CategorySelectView(
                selected_result.magnet_link, submit_download
            )
            
            # Send the category selection as a followup
            await interaction.followup.send(
                "Select a category:", view=category_view, ephemeral=True
            )
            
        except Exception as e:
            try:
                await send_error(interaction, f"An error occurred: {str(e)}")
            except Exception:
                logging.error(f"Failed to handle select callback: {str(e)}")

    async def prev_callback(self, interaction):
        self.page -= 1
        await self.update_message(interaction)

    async def next_callback(self, interaction):
        self.page += 1
        await self.update_message(interaction)

    async def cancel_callback(self, interaction):
        await interaction.message.delete()

    async def update_message(self, interaction):
        self.update_buttons()
        content = self.format_results()
        await interaction.response.edit_message(content=content, view=self)

    def format_results(self):
        start_idx = self.page * 5
        page_results = self.results[start_idx:start_idx + 5]
        total_pages = math.ceil(len(self.results) / 5)

        table = "```\n"
        table += f"{'#':<3} {'Title':<50} {'Size':<10} {'Seeders':<8}\n"
        table += "-" * 71 + "\n"
        
        for i, result in enumerate(page_results, 1):
            title = result.title[:47] + "..." if len(result.title) > 50 else result.title
            table += f"{i:<3} {title:<50} {result.size_gb:.2f}GB {result.seeders:<8}\n"
        
        table += f"\nPage {self.page + 1}/{total_pages}"
        table += "```"
        return table

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.lower()
    logging.info(f"Received message: {content}")

    if content == 'fuck, panic!!!':
        panic_message = 'this command has been removed due to how stupid it was. ask snow to reset server for you.'
        logging.warning(panic_message)
        await message.channel.send(panic_message)
        return

    if content.startswith('magnet:?'):
        async def submit_download(magnet_link, category):
            download_manager.submit(magnet_link, category)

        view = CategorySelectView(content, submit_download)
        
        await message.channel.send("Select a category:", view=view)
        return

    if content == 'fix perms':
        await message.channel.send('this command is now deprecated and there is no reason to use it. if somethin weird is goin on let snow know')
        return
        
    if content == 'cancel':
        await handle_cancel(message)
    elif content == 'progress':
        await handle_progress(message)
    elif content not in ['yes', 'no', 'next', 'movie', 'tv show', 'other']:
        await message.channel.send(f"Querying trackers for '{content}'. By the way, you look lovely today. Arrr.")
        search_results = search(content, config=config)
        if not search_results:
            await message.channel.send(f"No results found for '{content}'")
            return
            
        view = SearchResultsView(search_results)
        await message.channel.send(view.format_results(), view=view)

async def handle_cancel(message):
    confirm_view = View(timeout=300)
    confirm_button = Button(label="Confirm", style=discord.ButtonStyle.danger)
    cancel_button = Button(label="Abort", style=discord.ButtonStyle.secondary)

    async def confirm_callback(interaction):
        cancelled = download_manager.cancel_all()
        if cancelled:
            content = "All torrents have been cancelled."
        else:
            content = "No active torrents to cancel."
        await interaction.response.edit_message(content=content, view=None)

    async def cancel_callback(interaction):
        await interaction.response.edit_message(content="Cancellation aborted.", view=None)

    confirm_button.callback = confirm_callback
    cancel_button.callback = cancel_callback
    confirm_view.add_item(confirm_button)
    confirm_view.add_item(cancel_button)

    await message.channel.send("Are you sure you want to cancel all downloads?", view=confirm_view)

async def handle_progress(message):
    rows = download_manager.progress()
    if not rows:
        await message.channel.send("No active downloads.")
        return

    progress_table = "```\n"
    progress_table += f"{'Name':<40} {'Progress':<10} {'ETA':<15}\n"
    progress_table += "-" * 65 + "\n"
    
    for row in rows:
        name = row["name"][:37] + "..." if len(row["name"]) > 40 else row["name"]
        progress_table += f"{name:<40} {row['progress']:>10} {row['eta']:<15}\n"
    
    progress_table += "```"
    await message.channel.send(progress_table)

# Run both the Discord bot and FastAPI server
async def main():
    # Load configuration and connect to qBittorrent once, at startup
    startup()

    # Start FastAPI server
    uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=9870)
    server = uvicorn.Server(uvicorn_config)
    server_task = asyncio.create_task(server.serve())

    # Start Discord bot
    await bot.start(config.discord_bot_token)

    # Wait for both tasks
    await server_task

if __name__ == "__main__":
    asyncio.run(main())
