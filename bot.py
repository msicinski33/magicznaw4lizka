from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import config
from database import Database
from game import matches_guess, roll_threshold, weighted_creature

logging.basicConfig(level=logging.DEBUG if config.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("magic_suitcase")
RARITY_EMOJI = {"common":"⚪", "uncommon":"🟢", "rare":"🔵", "epic":"🟣", "legendary":"🟠", "mythical":"✨"}
RARITY_COLOR = {"common":0xAAB2BD,"uncommon":0x43B581,"rare":0x3498DB,"epic":0x9B59B6,"legendary":0xE67E22,"mythical":0xF1C40F}
RARITY_LABEL = {"common":"Pospolity", "uncommon":"Niepospolity", "rare":"Rzadki", "epic":"Epicki", "legendary":"Legendarny", "mythical":"Mityczny"}


def game_embed(title: str, description: str | None = None, *, color: int = 0x7654C8, footer: str | None = None) -> discord.Embed:
    """Apply the shared visual style used throughout the game."""
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    if footer:
        embed.set_footer(text=footer)
    return embed


def rarity_name(rarity: str) -> str:
    return RARITY_LABEL.get(rarity.lower(), rarity.title())


class SuitcaseBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = Database(config.database_path)
        self.thresholds: dict[tuple[int,int], int] = {}
        self.last_activity: dict[tuple[int,int], float] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    async def setup_hook(self):
        await self.store.connect()
        if config.creatures_path.exists():
            await self.store.load_creatures(config.creatures_path)
        await self.add_cog(GameCommands(self))
        await self.add_cog(AdminCommands(self))
        if config.guild_id:
            guild = discord.Object(id=config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await self.store.close()
        await super().close()

    async def on_ready(self):
        log.info("Zalogowano jako %s (%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id or not message.guild or not message.content.strip():
            return
        if message.content.startswith("!") or message.content.startswith("/"):
            return
        key = (message.guild.id, message.channel.id)
        encounters = await self.store.all("SELECT h.*,c.name,c.aliases,c.rarity,c.image_path FROM encounter_history h JOIN creatures c ON c.id=h.creature_id WHERE h.guild_id=? AND h.channel_id=? AND h.status='active' ORDER BY h.id DESC", key)
        for encounter in encounters:
            aliases = json.loads(encounter["aliases"])
            if matches_guess(message.content, [encounter["name"], *aliases]):
                if await self.store.catch(encounter["id"], message.author.id):
                    task = self.tasks.pop(encounter["id"], None)
                    if task: task.cancel()
                    embed = game_embed("✨ Stworzenie schwytane!", f"{message.author.mention} schwytał\n## {encounter['name']}", color=RARITY_COLOR.get(encounter["rarity"], 0x7654C8), footer="Nowy okaz trafił do walizki")
                    embed.add_field(name="Rzadkość", value=f"{RARITY_EMOJI.get(encounter['rarity'], '✨')} {rarity_name(encounter['rarity'])}", inline=True)
                    await message.reply(embed=embed)
                    return
        settings = await self.store.settings(message.guild.id)
        if not settings["enabled"]:
            return
        allowed = json.loads(settings["allowed_channels"])
        if allowed and message.channel.id not in allowed:
            return
        now = asyncio.get_running_loop().time()
        # Minimum 2 seconds between counted messages per person/channel.
        last_key = (message.guild.id, message.channel.id, message.author.id)
        if now - self.last_activity.get(last_key, 0) < 2:
            return
        self.last_activity[last_key] = now
        current = self.thresholds.get(key)
        if current is None:
            current = roll_threshold(settings["spawn_min_messages"], settings["spawn_max_messages"])
            self.thresholds[key] = current
        self.thresholds[key] -= 1
        if self.thresholds[key] > 0:
            return
        self.thresholds[key] = roll_threshold(settings["spawn_min_messages"], settings["spawn_max_messages"])
        await self.spawn_encounter(message.channel, message.guild.id, int(settings["encounter_duration"]), bool(settings["allow_parallel"]))

    async def spawn_encounter(self, channel, guild_id: int, duration: int | None = None, allow_parallel: bool | None = None):
        settings = await self.store.settings(guild_id)
        creatures = [dict(row) for row in await self.store.all("SELECT * FROM creatures WHERE enabled=1")]
        rarity = config.rarity_weights or {}
        creature = weighted_creature(creatures, rarity)
        duration = duration or int(settings["encounter_duration"])
        encounter_id = await self.store.spawn(guild_id, channel.id, creature["id"], duration, bool(settings["allow_parallel"]) if allow_parallel is None else allow_parallel)
        if encounter_id is None:
            return False
        path = (config.assets_dir / creature["image_path"]).resolve()
        if not path.is_file():
            await self.store.execute("UPDATE encounter_history SET status='closed' WHERE id=?", (encounter_id,))
            log.error("Brak grafiki stworzenia %s: %s", creature["name"], path)
            return False
        embed = game_embed("🌟 Magiczne spotkanie", "Na wolności pojawiło się stworzenie!\n\n**Rozpoznajesz je?** Wpisz jego nazwę na tym kanale, zanim zniknie.", color=RARITY_COLOR.get(creature["rarity"], 0x7654C8), footer=f"Masz {duration} sekund na odgadnięcie")
        file = discord.File(path, filename=path.name)
        embed.set_image(url=f"attachment://{path.name}")
        try:
            await channel.send(embed=embed, file=file)
        except discord.HTTPException:
            await self.store.execute("UPDATE encounter_history SET status='closed' WHERE id=?", (encounter_id,))
            raise
        self.tasks[encounter_id] = asyncio.create_task(self.expire_encounter(encounter_id, channel, creature["name"], duration))
        return True

    async def expire_encounter(self, encounter_id: int, channel, name: str, duration: int):
        try:
            await asyncio.sleep(duration)
            cur = await self.store.execute("UPDATE encounter_history SET status='expired' WHERE id=? AND status='active'", (encounter_id,))
            if cur.rowcount:
                await channel.send(embed=game_embed("⌛ Ślad zniknął", f"Nikt nie rozpoznał stworzenia. **{name}** wrócił do ukrycia.", color=0x7F8C8D, footer="Spotkanie zakończone"))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Błąd podczas zamykania spotkania %s", encounter_id)
        finally:
            self.tasks.pop(encounter_id, None)


class PageView(discord.ui.View):
    def __init__(self, owner_id: int, pages: list):
        super().__init__(timeout=180)
        self.owner_id, self.pages, self.index = owner_id, pages, 0
        self._buttons()

    def _buttons(self):
        for child in list(self.children): self.remove_item(child)
        prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.index == 0)
        nxt = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.index >= len(self.pages)-1)
        prev.callback = self.previous
        nxt.callback = self.next
        self.add_item(prev); self.add_item(nxt)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Ta paginacja należy do innego gracza.", ephemeral=True)
            return False
        return True

    def current(self):
        page = self.pages[self.index]
        if isinstance(page, tuple):
            return (*page, "image") if len(page) == 2 else page
        return page, None, "image"

    async def send(self, interaction, *, ephemeral=False):
        embed, path, image_style = self.current()
        options = {"embed": embed, "view": self, "ephemeral": ephemeral}
        if path and path.is_file():
            file = discord.File(path, filename=path.name)
            image_url = f"attachment://{path.name}"
            if image_style == "thumbnail":
                embed.set_thumbnail(url=image_url)
            else:
                embed.set_image(url=image_url)
            options["file"] = file
        await interaction.response.send_message(**options)

    async def show_page(self, interaction):
        embed, path, image_style = self.current()
        attachments = []
        if path and path.is_file():
            file = discord.File(path, filename=path.name)
            image_url = f"attachment://{path.name}"
            if image_style == "thumbnail":
                embed.set_image(url=None)
                embed.set_thumbnail(url=image_url)
            else:
                embed.set_thumbnail(url=None)
                embed.set_image(url=image_url)
            attachments.append(file)
        else:
            embed.set_thumbnail(url=None)
            embed.set_image(url=None)
        await interaction.response.edit_message(embed=embed, attachments=attachments, view=self)

    async def previous(self, interaction):
        self.index -= 1; self._buttons(); await self.show_page(interaction)

    async def next(self, interaction):
        self.index += 1; self._buttons(); await self.show_page(interaction)


class GameCommands(commands.Cog):
    def __init__(self, bot: SuitcaseBot): self.bot = bot

    @app_commands.command(name="collection", description="Otwórz swoją magiczną walizkę")
    async def collection(self, interaction: discord.Interaction):
        rows = await self.bot.store.all("SELECT c.*,u.quantity FROM user_creatures u JOIN creatures c ON c.id=u.creature_id WHERE u.discord_id=? ORDER BY c.id", (interaction.user.id,))
        total = await self.bot.store.one("SELECT total_captures FROM users WHERE discord_id=?", (interaction.user.id,))
        if not rows:
            embed = game_embed("🧳 Twoja magiczna walizka", "Jest jeszcze pusta.\n\nWypatruj spotkań i zgaduj nazwy stworzeń, aby rozpocząć kolekcję!", color=0x7654C8)
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        page_size = 8
        page_count = (len(rows) + page_size - 1) // page_size
        pages=[]
        for start in range(0, len(rows), page_size):
            batch = rows[start:start + page_size]
            lines = [
                f"{RARITY_EMOJI.get(r['rarity'], '✨')} **{r['name']}**  `×{r['quantity']}`  ·  {rarity_name(r['rarity'])}"
                for r in batch
            ]
            page_number = len(pages) + 1
            embed = game_embed(
                f"🧳 {interaction.user.display_name} · Magiczna walizka",
                "\n".join(lines),
                color=0x7654C8,
                footer=f"Strona {page_number}/{page_count}  ·  {len(rows)} odkrytych gatunków",
            )
            embed.set_author(name="Twoja kolekcja", icon_url=interaction.user.display_avatar.url)
            embed.add_field(name="Wszystkie schwytania", value=f"**{total['total_captures'] if total else 0}**", inline=True)
            embed.add_field(name="Pozycje na stronie", value=f"**{len(batch)}**", inline=True)
            pages.append((embed, config.assets_dir/batch[0]['image_path'], "thumbnail"))
        await PageView(interaction.user.id,pages).send(interaction, ephemeral=True)

    @app_commands.command(name="dex", description="Przeglądaj bestiariusz")
    @app_commands.describe(rarity="Opcjonalny filtr rzadkości")
    @app_commands.choices(rarity=[app_commands.Choice(name=x.title(),value=x) for x in ("common","uncommon","rare","epic","legendary","mythical")])
    async def dex(self, interaction: discord.Interaction, rarity: app_commands.Choice[str] | None = None):
        sql="SELECT c.*,u.quantity FROM creatures c LEFT JOIN user_creatures u ON u.creature_id=c.id AND u.discord_id=? WHERE c.enabled=1"; args=[interaction.user.id]
        if rarity: sql += " AND c.rarity=?"; args.append(rarity.value)
        sql += " ORDER BY c.id"
        rows=await self.bot.store.all(sql,tuple(args))
        if not rows: await interaction.response.send_message("W tym filtrze nie ma jeszcze gatunków.",ephemeral=True); return
        discovered = sum(1 for r in rows if r["quantity"])
        pages=[]
        for r in rows:
            known = bool(r["quantity"])
            description = f"## {r['name']}" if known else "## Nieodkryty gatunek"
            description += f"\n`Wpis #{r['id']:03}`"
            if known:
                description += f"\n\n{RARITY_EMOJI.get(r['rarity'],'✨')} **{rarity_name(r['rarity'])}**  ·  Posiadane: **×{r['quantity']}**"
            embed=game_embed("📖 Bestiariusz", description, color=RARITY_COLOR.get(r['rarity'],0x7654C8) if known else 0x42405A, footer=f"Wpis {len(pages)+1}/{len(rows)}  ·  Odkryte {discovered}/{len(rows)}")
            embed.set_author(name=f"Kolekcja {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            pages.append((embed, config.assets_dir/r['image_path'] if r['quantity'] else None))
        await PageView(interaction.user.id,pages).send(interaction,ephemeral=True)

    @app_commands.command(name="profile",description="Zobacz profil kolekcjonera")
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None=None):
        user=member or interaction.user
        rows=await self.bot.store.all("SELECT c.name,c.rarity,u.quantity FROM user_creatures u JOIN creatures c ON c.id=u.creature_id WHERE u.discord_id=?",(user.id,))
        total=sum(r['quantity'] for r in rows); rarest=max(rows,key=lambda r:(('common','uncommon','rare','epic','legendary','mythical').index(r['rarity']),r['name']),default=None)
        all_count=(await self.bot.store.one("SELECT COUNT(*) n FROM creatures WHERE enabled=1"))["n"]
        embed=game_embed(f"🪄 Profil kolekcjonera", f"Przegląd postępów **{user.display_name}** w świecie magicznych stworzeń.", color=0x7654C8)
        embed.set_author(name="Karta kolekcjonera", icon_url=user.display_avatar.url)
        embed.add_field(name="🧺 Schwytane stworzenia",value=f"**{total}**",inline=True)
        embed.add_field(name="📚 Odkryte gatunki",value=f"**{len(rows)}** / {all_count}",inline=True)
        embed.add_field(name="🌫️ Do odkrycia",value=f"**{max(0,all_count-len(rows))}**",inline=True)
        embed.add_field(name="💎 Najrzadszy okaz",value=f"**{rarest['name']}** · {rarity_name(rarest['rarity'])}" if rarest else "Jeszcze brak — czas wyruszyć na poszukiwania!",inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard",description="Rankingi kolekcjonerów")
    @app_commands.choices(category=[app_commands.Choice(name="Wszystkie schwytane",value="captures"),app_commands.Choice(name="Odkryte gatunki",value="species"),app_commands.Choice(name="Rzadkie i wyższe",value="rare")])
    async def leaderboard(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        expr={"captures":"u.total_captures","species":"COUNT(DISTINCT uc.creature_id)","rare":"COALESCE(SUM(CASE WHEN c.rarity IN ('rare','epic','legendary','mythical') THEN uc.quantity ELSE 0 END),0)"}[category.value]
        rows=await self.bot.store.all(f"SELECT u.discord_id,{expr} score FROM users u LEFT JOIN user_creatures uc ON uc.discord_id=u.discord_id LEFT JOIN creatures c ON c.id=uc.creature_id GROUP BY u.discord_id ORDER BY score DESC LIMIT 100")
        pages=[]
        for start in range(0,max(1,len(rows)),10):
            batch=rows[start:start+10]; lines=[]
            for i,row in enumerate(batch,start+1):
                member=interaction.guild.get_member(row['discord_id']) if interaction.guild else None
                label=member.display_name if member else f"Kolekcjoner {str(row['discord_id'])[-4:]}"
                medal = {1:"🥇", 2:"🥈", 3:"🥉"}.get(i, f"`{i:02}`")
                lines.append(f"{medal}  **{label}**\n　　{row['score']} {category.name.lower()}")
            page_count=max(1,(len(rows)+9)//10)
            embed=game_embed(f"🏆 Ranking · {category.name}", "\n\n".join(lines) or "Na razie nie ma jeszcze wyników.", color=0xD8A83E, footer=f"Strona {len(pages)+1}/{page_count}  ·  Najlepsi kolekcjonerzy")
            pages.append(embed)
        await interaction.response.send_message(embed=pages[0],view=PageView(interaction.user.id,pages))

    @app_commands.command(name="creature",description="Sprawdź gatunek ze swojej walizki")
    async def creature(self, interaction: discord.Interaction, name: str):
        row=await self.bot.store.one("SELECT c.*,u.quantity FROM creatures c JOIN user_creatures u ON u.creature_id=c.id WHERE u.discord_id=? AND (c.name=? COLLATE NOCASE OR c.aliases LIKE ?)",(interaction.user.id,name,f"%{name}%"))
        if not row: await interaction.response.send_message("Nie masz takiego gatunku w walizce.",ephemeral=True); return
        embed=game_embed(f"✨ {row['name']}", f"`Wpis w bestiariuszu #{row['id']:03}`", color=RARITY_COLOR.get(row['rarity'],0x7654C8))
        embed.add_field(name="Rzadkość",value=f"{RARITY_EMOJI.get(row['rarity'],'✨')} {rarity_name(row['rarity'])}",inline=True)
        embed.add_field(name="W twojej walizce",value=f"**×{row['quantity']}**",inline=True)
        path=config.assets_dir/row['image_path']
        if path.is_file():
            file=discord.File(path,filename=path.name); embed.set_image(url=f"attachment://{path.name}"); await interaction.response.send_message(embed=embed,file=file)
        else: await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats",description="Statystyki gry")
    async def stats(self, interaction: discord.Interaction):
        rows=await self.bot.store.one("SELECT COUNT(*) species FROM creatures WHERE enabled=1")
        users=await self.bot.store.one("SELECT COUNT(*) n FROM users")
        caught=await self.bot.store.one("SELECT COALESCE(SUM(total_captures),0) n FROM users")
        active=await self.bot.store.one("SELECT COUNT(*) n FROM encounter_history WHERE status='active'")
        embed=game_embed("✨ Kroniki Magicznej Walizki", "Oto najnowszy obraz świata kolekcjonerów.", color=0x7654C8)
        embed.add_field(name="📖 Gatunki",value=f"**{rows['species']}**",inline=True)
        embed.add_field(name="🪄 Kolekcjonerzy",value=f"**{users['n']}**",inline=True)
        embed.add_field(name="🎒 Schwytane stworzenia",value=f"**{caught['n']}**",inline=True)
        embed.add_field(name="🌟 Aktywne spotkania",value=f"**{active['n']}**",inline=True)
        await interaction.response.send_message(embed=embed)


class AdminCommands(commands.GroupCog, group_name="admin", group_description="Administracja gry"):
    def __init__(self, bot: SuitcaseBot): self.bot=bot
    async def interaction_check(self, interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Wymagane uprawnienie: Zarządzanie serwerem.",ephemeral=True); return False
        return True

    @app_commands.command(name="spawn",description="Wywołaj testowe spotkanie")
    async def spawn(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok=await self.bot.spawn_encounter(interaction.channel,interaction.guild_id)
        await interaction.followup.send("Spotkanie pojawiło się na kanale." if ok else "Nie udało się uruchomić spotkania (sprawdź aktywne spotkanie i grafiki).",ephemeral=True)

    @app_commands.command(name="add-creature",description="Dodaj lub zaktualizuj gatunek")
    async def add_creature(self, interaction: discord.Interaction, id: app_commands.Range[int,1,100000], name: str, rarity: str, image_path: str, aliases: str=""):
        rarity=rarity.lower()
        if rarity not in ("common","uncommon","rare","epic","legendary","mythical"):
            await interaction.response.send_message("Rzadkość: common, uncommon, rare, epic, legendary lub mythical.",ephemeral=True); return
        image=(config.assets_dir/image_path).resolve()
        if not image.is_file() or not image.is_relative_to(config.assets_dir.resolve()):
            await interaction.response.send_message("Nie znaleziono grafiki w katalogu assets.",ephemeral=True); return
        await self.bot.store.execute("INSERT INTO creatures(id,name,aliases,rarity,image_path,enabled) VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases=excluded.aliases,rarity=excluded.rarity,image_path=excluded.image_path,enabled=1",(id,name,json.dumps([x.strip() for x in aliases.split('|') if x.strip()],ensure_ascii=False),rarity,image_path))
        await interaction.response.send_message(f"Zapisano gatunek **{name}**.",ephemeral=True)

    @app_commands.command(name="remove-creature",description="Wyłącz gatunek")
    async def remove_creature(self, interaction: discord.Interaction, id: int):
        cur=await self.bot.store.execute("UPDATE creatures SET enabled=0 WHERE id=?",(id,))
        await interaction.response.send_message("Gatunek wyłączony." if cur.rowcount else "Nie znaleziono gatunku.",ephemeral=True)

    @app_commands.command(name="reload-creatures",description="Wczytaj ponownie plik creatures.json")
    async def reload_creatures(self, interaction: discord.Interaction):
        count=await self.bot.store.load_creatures(config.creatures_path)
        await interaction.response.send_message(f"Wczytano {count} rekordów.",ephemeral=True)

    @app_commands.command(name="set-channel",description="Ustaw kanały spotkań (puste usuwa ograniczenie)")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None=None):
        ids=[] if channel is None else [channel.id]
        await self.bot.store.execute("INSERT INTO server_settings(guild_id,allowed_channels) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET allowed_channels=excluded.allowed_channels",(interaction.guild_id,json.dumps(ids)))
        await interaction.response.send_message(f"Kanały spotkań: {channel.mention if channel else 'wszystkie kanały tekstowe'}.",ephemeral=True)

    @app_commands.command(name="settings",description="Zmień zakres aktywności i czas spotkania")
    async def settings(self, interaction: discord.Interaction, min_messages: app_commands.Range[int,1,500] | None=None, max_messages: app_commands.Range[int,1,1000] | None=None, duration: app_commands.Range[int,15,3600] | None=None, enabled: bool | None=None, allow_parallel: bool | None=None):
        old=await self.bot.store.settings(interaction.guild_id)
        low=min_messages if min_messages is not None else old['spawn_min_messages']; high=max_messages if max_messages is not None else old['spawn_max_messages']
        if low>high: await interaction.response.send_message("Minimum nie może przekraczać maksimum.",ephemeral=True); return
        await self.bot.store.execute("UPDATE server_settings SET spawn_min_messages=?,spawn_max_messages=?,encounter_duration=?,enabled=?,allow_parallel=? WHERE guild_id=?",(low,high,duration or old['encounter_duration'],int(enabled if enabled is not None else old['enabled']),int(allow_parallel if allow_parallel is not None else old['allow_parallel']),interaction.guild_id))
        await interaction.response.send_message(f"Zapisano: próg {low}–{high} wiadomości, spotkanie {duration or old['encounter_duration']} s.",ephemeral=True)


if __name__ == "__main__":
    if not config.token:
        raise SystemExit("Brak DISCORD_TOKEN. Skopiuj .env.example do .env i uzupełnij token bota.")
    SuitcaseBot().run(config.token, log_handler=None)
