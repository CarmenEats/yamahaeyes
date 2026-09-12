"""DPI Roblox verification bot. Python 3.11+. Read instructions.txt first."""
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
GROUP_ID = 5008654
OAUTH = 'https://apis.roblox.com/oauth/v1'
# Exact Roblox role IDs checked against group 5008654 on 2026-09-12.
# IDs survive renames; new Roblox ranks need an explicit mapping here.
RANK_ROLES = {
    33408102: 'Torium Trainee', 33408103: 'Torium Aide',
    35436694: 'Torium Helper', 33408381: 'Torium Nanny',
    33408104: 'Torium Carer', 33408376: 'Torium Courier',
    34180888: 'Others',
    45209395: 'Maha Team', 45209404: 'Maha Team',
    45209417: 'Maha Team', 45209444: 'Maha Team', 45209455: 'Maha Team',
    45209503: 'Majora Team', 45209514: 'Majora Team',
    45209520: 'Majora Team', 45209530: 'Majora Team', 45209550: 'Majora Team',
    33408389: 'Others', 33408391: 'Others', 33419131: 'Others',
}
ROLE_NAMES = tuple(dict.fromkeys(RANK_ROLES.values())) + ('Verified',)
log = logging.getLogger('dpi')


class FriendlyError(Exception):
    pass


def normalized(name):
    return ' '.join(re.findall(r'[a-z0-9]+', name.casefold()))


def rank_target(rows):
    """Use highest actual group rank; no invented ranges or substring grants."""
    if not rows:
        return None, 'Not in the group'
    role = max(rows, key=lambda r: int(r['rank']))
    return RANK_ROLES.get(int(role['id'])), role['name']


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS links (
              guild INTEGER, discord INTEGER, roblox INTEGER,
              PRIMARY KEY(guild,discord), UNIQUE(guild,roblox));
            CREATE TABLE IF NOT EXISTS settings (guild INTEGER PRIMARY KEY, data TEXT);
        ''')

    def config(self, guild):
        row = self.db.execute('SELECT data FROM settings WHERE guild=?', (guild,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_config(self, guild, data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                            (guild, json.dumps(data)))

    def linked(self, guild, user):
        row = self.db.execute('SELECT roblox FROM links WHERE guild=? AND discord=?',
                              (guild, user)).fetchone()
        return row[0] if row else None

    def link(self, guild, user, roblox):
        # Do not use REPLACE: it would silently steal another person's Roblox link.
        try:
            with self.db:
                self.db.execute('INSERT INTO links VALUES (?,?,?) ON CONFLICT(guild,discord) '
                                'DO UPDATE SET roblox=excluded.roblox', (guild, user, roblox))
        except sqlite3.IntegrityError:
            raise FriendlyError('That Roblox account is already linked to another Discord '
                                'member in this server. Ask that member to /unlink first.')


async def reply_error(interaction, error):
    if isinstance(error, FriendlyError):
        message = str(error)
    elif isinstance(error, discord.Forbidden):
        message = 'Discord blocked a change. Ask an admin to check the bot permissions and role order, then retry.'
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f'Please wait {error.retry_after:.0f} seconds before trying again.'
    elif isinstance(error, app_commands.CheckFailure):
        message = 'This command is unavailable here or you lack its required server permission.'
    else:
        log.error('Operation failed: %s', type(error).__name__)
        message = 'The service is temporarily unavailable. Try again in a minute. An admin can check the bot console.'
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await reply_error(interaction, error)


class VerifyPanel(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Verify with Roblox', style=discord.ButtonStyle.success,
                       custom_id='dpi:verify:v1', emoji='✅')
    async def verify(self, interaction, button):
        await self.bot.begin_verification(interaction)


class ConnectView(SafeView):
    def __init__(self, bot, state, url):
        super().__init__(timeout=600)
        self.bot, self.state = bot, state
        self.add_item(discord.ui.Button(label='Sign in with Roblox', url=url))

    @discord.ui.button(label='I have signed in', style=discord.ButtonStyle.success)
    async def finish(self, interaction, button):
        pending = self.bot.get_pending(self.state, interaction)
        if not pending.get('roblox'):
            raise FriendlyError('Complete Roblox sign-in in your browser first, then press this button again.')
        name = discord.utils.escape_markdown(pending['name'])
        await interaction.response.send_message(
            f'Connect **{name}** (Roblox ID `{pending["roblox"]}`) to your Discord account? '
            'Only confirm if this is your account. This replaces any previous link.',
            view=ConfirmView(self.bot, self.state), ephemeral=True)


class ConfirmView(SafeView):
    def __init__(self, bot, state):
        super().__init__(timeout=600)
        self.bot, self.state = bot, state

    @discord.ui.button(label='Confirm my account', style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.mutation_lock:
            pending = self.bot.get_pending(self.state, interaction)
            roblox = pending['roblox']
            # Validate configuration and current API data before persisting the link.
            member = await interaction.guild.fetch_member(interaction.user.id)
            self.bot.config_roles(interaction.guild)
            data = await self.bot.roblox_data(roblox)
            self.bot.store.link(interaction.guild_id, member.id, roblox)
            self.bot.pending.pop(self.state, None)
            try:
                result = await self.bot.apply_roles(member, data)
            except Exception:
                raise FriendlyError('Your account link was saved, but Discord could not finish '
                                    'updating your roles. Ask an admin to check permissions, then use /getrole.')
        await interaction.followup.send(result, ephemeral=True)
        self.stop()


class DPIBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, member_cache_flags=discord.MemberCacheFlags.none(),
                         allowed_mentions=discord.AllowedMentions.none(), max_messages=None)
        self.tree = app_commands.CommandTree(self)
        self.store = Store(os.getenv('DATABASE_PATH', str(Path(__file__).with_name('data') / 'bot.db')))
        self.guild_id = int(os.environ['DISCORD_GUILD_ID'])
        self.client_id = os.environ['ROBLOX_CLIENT_ID']
        self.client_secret = os.environ['ROBLOX_CLIENT_SECRET']
        self.redirect = os.environ['ROBLOX_REDIRECT_URI']
        parsed = urlparse(self.redirect)
        if parsed.scheme != 'https' or parsed.path != '/oauth/callback' or parsed.query or parsed.fragment:
            raise ValueError('ROBLOX_REDIRECT_URI must be https://YOUR-HOST/oauth/callback')
        self.pending = {}
        self.mutation_lock = asyncio.Lock()
        self.runner = None
        self.roblox_http = None

    async def setup_hook(self):
        self.roblox_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.add_view(VerifyPanel(self))
        app = web.Application(client_max_size=8192)
        app.router.add_get('/', self.home)
        app.router.add_get('/health', self.health)
        app.router.add_get('/oauth/callback', self.callback)
        self.runner = web.AppRunner(app, access_log=None)  # Never log OAuth codes/URLs.
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', int(os.getenv('PORT', '8080'))).start()
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.auto_sync.start()

    async def close(self):
        self.auto_sync.cancel()
        if self.runner:
            await self.runner.cleanup()
        if self.roblox_http:
            await self.roblox_http.close()
        self.store.db.close()
        await super().close()

    async def on_ready(self):
        log.info('Connected as %s. Run /setup in your verify channel.', self.user)

    async def home(self, request):
        return web.Response(text='DPI Roblox verification is online. Start verification in the Discord server.')

    async def health(self, request):
        return web.json_response({'ready': self.is_ready()}, status=200 if self.is_ready() else 503)

    async def api(self, method, url, **kwargs):
        for attempt in range(3):
            try:
                async with self.roblox_http.request(method, url, allow_redirects=False, **kwargs) as response:
                    if method == 'GET' and (response.status == 429 or response.status >= 500) and attempt < 2:
                        try:
                            delay = min(10, max(1, float(response.headers.get('Retry-After', '2'))))
                        except ValueError:
                            delay = 2
                        await asyncio.sleep(delay * (attempt + 1))
                        continue
                    if response.status != 200:
                        raise FriendlyError(f'Roblox returned HTTP {response.status}. Please retry in a minute.')
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                raise FriendlyError('Roblox could not be reached or returned invalid data. Please retry shortly.')

    async def roblox_data(self, user_id):
        profile = await self.api('GET', f'https://users.roblox.com/v1/users/{user_id}')
        groups = await self.api('GET', f'https://groups.roblox.com/v2/users/{user_id}/groups/roles')
        try:
            if int(profile['id']) != user_id or not isinstance(profile['name'], str):
                raise ValueError()
            if not isinstance(groups['data'], list):
                raise ValueError()
            roles = []
            for entry in groups['data']:
                if int(entry['group']['id']) == GROUP_ID:
                    role = entry['role']
                    int(role['id']), int(role['rank'])
                    if not isinstance(role['name'], str):
                        raise ValueError()
                    roles.append(role)
            target, rank = rank_target(roles)
            return profile['name'], target, rank
        except (KeyError, TypeError, ValueError):
            raise FriendlyError('Roblox returned an unexpected response. No roles were changed.')

    def config_roles(self, guild):
        if guild.id != self.guild_id:
            raise FriendlyError('This bot is configured for a different server.')
        config = self.store.config(guild.id)
        if not config:
            raise FriendlyError('An administrator needs to run /setup first.')
        roles = {}
        for name, role_id in config['roles'].items():
            role = guild.get_role(role_id)
            if not role or not role.is_assignable():
                raise FriendlyError(f'The bot cannot manage "{name}". Check role order and run /setup again.')
            if role.permissions.administrator or role.permissions.manage_roles or role.permissions.manage_guild:
                raise FriendlyError(f'Remove administrative permissions from the automatic role "{name}".')
            roles[name] = role
        if not guild.me.guild_permissions.manage_roles:
            raise FriendlyError('The bot needs Manage Roles permission.')
        return roles

    async def apply_roles(self, member, data):
        name, target, rank = data
        roles = self.config_roles(member.guild)
        desired = [roles['Verified']] + ([roles[target]] if target else [])
        managed = [role for key, role in roles.items() if key != 'Unverified']
        remove = [role for role in managed if role in member.roles and role not in desired]
        if 'Unverified' in roles and roles['Unverified'] in member.roles:
            remove.append(roles['Unverified'])
        # Remove stale ranks first. Targeted calls preserve unrelated server roles.
        if remove:
            await member.remove_roles(*remove, reason='Roblox group rank sync', atomic=True)
        add = [role for role in desired if role not in member.roles]
        if add:
            await member.add_roles(*add, reason='Verified Roblox account and group rank', atomic=True)
        warning = ''
        if member.nick != name:
            try:
                await member.edit(nick=name[:32], reason='Use verified Roblox username')
            except discord.HTTPException:
                warning = '\nNickname could not be changed; check Manage Nicknames and role hierarchy (server owners cannot be renamed).'
        safe_name = discord.utils.escape_markdown(name)
        safe_rank = discord.utils.escape_markdown(rank)
        return (f'✅ Verified as **{safe_name}**!\nRoblox rank: **{safe_rank}**.\n'
                f'Discord roles: **Verified**' + (f' + **{target}**.' if target else
                '. This group rank has no configured Discord rank role.') + warning)

    def get_pending(self, state, interaction):
        item = self.pending.get(state)
        if not item or item['expires'] < time.time():
            self.pending.pop(state, None)
            raise FriendlyError('This verification expired or the bot restarted. Press Verify with Roblox again.')
        if item['user'] != interaction.user.id or item['guild'] != interaction.guild_id:
            raise FriendlyError('This verification belongs to another Discord member.')
        return item

    async def begin_verification(self, interaction):
        if not interaction.guild or interaction.guild_id != self.guild_id:
            raise FriendlyError('Verify inside the configured Discord server.')
        self.config_roles(interaction.guild)
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v['expires'] > now}
        previous = [v for v in self.pending.values() if v['user'] == interaction.user.id]
        if previous and now - max(v['created'] for v in previous) < 20:
            raise FriendlyError('Use your existing private verification message, or wait 20 seconds to restart.')
        if len(self.pending) >= 1000:
            raise FriendlyError('Verification is busy. Try again in a few minutes.')
        self.pending = {k: v for k, v in self.pending.items() if v['user'] != interaction.user.id}
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.pending[state] = {'guild': interaction.guild_id, 'user': interaction.user.id,
                               'created': now, 'expires': now + 600, 'verifier': verifier}
        url = OAUTH + '/authorize?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect,
            'scope': 'openid profile', 'response_type': 'code', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256'})
        await interaction.response.send_message(
            '1. Open **Sign in with Roblox** and approve on Roblox.\n'
            '2. Return here, click **I have signed in**, and confirm your username.\n'
            'Your nickname, Verified role and group rank will then update. Expires in 10 minutes. '
            'Keep this private link to yourself. We store your Discord and Roblox IDs; /unlink removes the link.',
            view=ConnectView(self, state, url), ephemeral=True)

    async def callback(self, request):
        state = request.query.get('state', '')
        item = self.pending.get(state)
        text = 'This link expired or was already used. Start verification again in Discord.'
        status = 400
        if item and item['expires'] > time.time() and not item.get('used'):
            item['used'] = True  # Claim before awaiting; callback cannot be replayed.
            if request.query.get('error'):
                text = 'Roblox sign-in was declined. You can start again in Discord.'
            elif request.query.get('code'):
                try:
                    tokens = await self.api('POST', OAUTH + '/token', data={
                        'grant_type': 'authorization_code', 'code': request.query['code'],
                        'client_id': self.client_id, 'client_secret': self.client_secret,
                        'redirect_uri': self.redirect, 'code_verifier': item['verifier']})
                    identity = await self.api('GET', OAUTH + '/userinfo',
                                              headers={'Authorization': 'Bearer ' + tokens['access_token']})
                    roblox = int(identity['sub'])
                    if roblox <= 0:
                        raise ValueError()
                    item['roblox'] = roblox
                    item['name'] = str(identity.get('preferred_username', roblox))
                    text = f'Signed in as {item["name"]}. Return to Discord, click "I have signed in", and confirm this account.'
                    status = 200
                    # No access, refresh or ID tokens are persisted.
                except Exception as error:
                    log.warning('OAuth exchange failed: %s', type(error).__name__)
                    text = 'Sign-in failed. Start again in Discord. If it persists, ask the admin to check OAuth settings.'
        return web.Response(text='<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width">'
                            '<title>Roblox verification</title><h1>Roblox verification</h1><p>'
                            + html.escape(text) + '</p></html>', content_type='text/html', status=status,
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                     'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'"})

    async def sync_member(self, guild, user):
        async with self.mutation_lock:
            roblox = self.store.linked(guild.id, user)
            if not roblox:
                raise FriendlyError('Verify your Roblox account first using /verify or the verification button.')
            member = await guild.fetch_member(user)
            data = await self.roblox_data(roblox)
            return await self.apply_roles(member, data)

    async def on_member_join(self, member):
        if member.guild.id == self.guild_id and self.store.linked(member.guild.id, member.id):
            try:
                await self.sync_member(member.guild, member.id)
            except Exception as error:
                log.warning('Join sync failed (%s); member can use /getrole.', type(error).__name__)

    @tasks.loop(minutes=30)
    async def auto_sync(self):
        guild = self.get_guild(self.guild_id)
        if not guild:
            return
        rows = self.store.db.execute('SELECT discord FROM links WHERE guild=?', (guild.id,)).fetchall()
        for (user,) in rows:
            try:
                await self.sync_member(guild, user)
            except discord.NotFound:
                pass  # Keep verification so returning members can be synced.
            except Exception as error:
                log.warning('Scheduled role sync failed: %s', type(error).__name__)
            await asyncio.sleep(2)

    @auto_sync.before_loop
    async def before_sync(self):
        await self.wait_until_ready()


def register_commands(bot):
    @bot.tree.command(name='verify', description='Securely connect your Roblox account')
    @app_commands.guild_only()
    async def verify(interaction: discord.Interaction):
        await bot.begin_verification(interaction)

    @bot.tree.command(name='getrole', description='Refresh your Roblox group rank and server nickname')
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
    async def getrole(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot.sync_member(interaction.guild, interaction.user.id)
        await interaction.followup.send(result, ephemeral=True)

    @bot.tree.command(name='unlink', description='Delete your account link and remove bot-managed roles')
    @app_commands.guild_only()
    async def unlink(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with bot.mutation_lock:
            member = await interaction.guild.fetch_member(interaction.user.id)
            roles = bot.config_roles(interaction.guild)
            remove = [r for n, r in roles.items() if n != 'Unverified' and r in member.roles]
            if remove:
                await member.remove_roles(*remove, reason='Member unlinked Roblox', atomic=True)
            if 'Unverified' in roles:
                await member.add_roles(roles['Unverified'], reason='Member unlinked Roblox', atomic=True)
            with bot.store.db:
                bot.store.db.execute('DELETE FROM links WHERE guild=? AND discord=?',
                                     (interaction.guild_id, member.id))
            bot.pending = {k: v for k, v in bot.pending.items() if v['user'] != member.id}
        await interaction.followup.send('Your link and verification/rank roles were removed. Your nickname was left as it is.', ephemeral=True)

    @bot.tree.command(name='setup', description='Admin: prepare roles and post the verification panel')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        channel = channel or interaction.channel
        if guild.id != bot.guild_id or not isinstance(channel, discord.TextChannel):
            raise FriendlyError('Run this in a text channel in the configured server.')
        if not guild.me.guild_permissions.manage_roles:
            raise FriendlyError('Give the bot Manage Roles first.')
        perms = channel.permissions_for(guild.me)
        if not all((perms.view_channel, perms.send_messages, perms.embed_links, perms.read_message_history)):
            raise FriendlyError('Give the bot View Channel, Send Messages, Embed Links and Read Message History here.')
        async with bot.mutation_lock:
            old = bot.store.config(guild.id) or {}
            mapping = {}
            for name in ROLE_NAMES + ('Unverified',):
                saved_id = old.get('roles', {}).get(name)
                role = guild.get_role(saved_id) if saved_id else None
                matches = [r for r in guild.roles if normalized(r.name) == normalized(name)]
                if not role and len(matches) > 1:
                    raise FriendlyError(f'Multiple roles match "{name}". Rename duplicates and rerun /setup.')
                if not role:
                    role = matches[0] if matches else None
                if not role and name != 'Unverified':
                    role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
                                                   reason='DPI verification setup')
                if role:
                    mapping[name] = role.id
            config = {**old, 'roles': mapping}
            bot.store.save_config(guild.id, config)
            bot.config_roles(guild)
            embed = discord.Embed(title='Connect your Roblox account', color=0x39B979,
                description='Click the green button below to verify with Roblox.\n\n'
                '✅ Receive your **Verified** role\n'
                '🏷️ Get the role matching your rank in group **5008654**\n'
                '✨ Use your Roblox username as your server nickname\n\n'
                'Changed rank? Use **/getrole** to refresh.\n'
                'Sign-in happens on Roblox. Never send anyone your password or Roblox cookie.')
            embed.set_footer(text='DPI • Private verification • /unlink to remove your connection')
            message = None
            if old.get('channel') == channel.id and old.get('message'):
                try:
                    message = await channel.fetch_message(old['message'])
                    await message.edit(embed=embed, view=VerifyPanel(bot))
                except discord.NotFound:
                    message = None
            if message is None:
                message = await channel.send(embed=embed, view=VerifyPanel(bot))
            config.update(channel=channel.id, message=message.id)
            bot.store.save_config(guild.id, config)
        await interaction.followup.send('Verification panel is ready. Test it with an ordinary member account. '
                                        'Members must be able to view this channel before they are verified.', ephemeral=True)

    @bot.tree.command(name='status', description='Admin: check configuration and rank mappings')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(interaction: discord.Interaction):
        roles = bot.config_roles(interaction.guild)
        body = '\n'.join(f'{name}: <@&{role.id}>' for name, role in roles.items())
        count = bot.store.db.execute('SELECT COUNT(*) FROM links WHERE guild=?', (interaction.guild_id,)).fetchone()[0]
        await interaction.response.send_message(f'Group: `{GROUP_ID}` • Linked accounts: {count}\n{body}\n'
                                               'Automatic rank refresh: every 30 minutes, plus on rejoin.', ephemeral=True)

    @bot.tree.error
    async def on_error(interaction, error):
        await reply_error(interaction, getattr(error, 'original', error))


def main():
    required = ('DISCORD_TOKEN', 'DISCORD_GUILD_ID', 'ROBLOX_CLIENT_ID',
                'ROBLOX_CLIENT_SECRET', 'ROBLOX_REDIRECT_URI')
    missing = [key for key in required if not os.getenv(key) or os.getenv(key, '').startswith('CHANGE_ME')]
    if missing:
        raise SystemExit('Fill in hosting environment variables or .env: ' + ', '.join(missing))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    bot = DPIBot()
    register_commands(bot)
    bot.run(os.environ['DISCORD_TOKEN'], log_handler=None)


if __name__ == '__main__':
    main()
