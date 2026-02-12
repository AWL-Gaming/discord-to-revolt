import json
import asyncio
import time
from collections import defaultdict, deque
import requests
import stoat as pyvolt
from pathlib import Path

TEMPLATE_IMPORT_STEPS = 5
PROGRESS_FILE = "import_progress.json"

IDs = {"roles": {}, "channels": {}}
cache = {"roles": {}, "channels": {}}

# Helper class to handle channels when the library fails
class RawChannel:
    def __init__(self, data):
        self.id = data["_id"]
        self.name = data.get("name", "Unknown")
        self.type = data.get("channel_type", "Text")
        # Store raw data for debugging
        self._raw = data

    def __repr__(self):
        return f"<RawChannel id={self.id} name={self.name}>"


# ─────────────────────────────────────────────────────────────
# Revolt API helpers (for when the client library is missing data)
# ─────────────────────────────────────────────────────────────
def revolt_api_json(method: str, url: str, headers: dict, payload: dict | None = None, timeout: int = 30):
    """HTTP helper with basic retry + 429 handling."""
    for attempt in range(6):
        resp = requests.request(method, url, headers=headers, json=payload, timeout=timeout)
        # Handle rate limit
        if resp.status_code == 429:
            try:
                data = resp.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.25, retry_after))
            continue
        # Retry transient errors
        if resp.status_code >= 500 and attempt < 5:
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {resp.status_code}: {resp.text}")
        try:
            return resp.json()
        except Exception:
            return resp.text

def _norm_name(name: str) -> str:
    return (name or "").casefold().strip()

def _revolt_channel_kind(ch) -> str:
    """Best-effort mapping of Revolt channel object -> 'text' | 'voice' | 'unknown'."""
    # RawChannel stores a string in .type
    t = getattr(ch, "type", None)
    if isinstance(t, str):
        tl = t.lower()
        if "voice" in tl:
            return "voice"
        if "text" in tl:
            return "text"
    # Library objects may use ChannelType enum
    try:
        if t == pyvolt.ChannelType.voice:
            return "voice"
        if t == pyvolt.ChannelType.text:
            return "text"
    except Exception:
        pass
    # Fallback using raw payload if present
    raw = getattr(ch, "_raw", None)
    if isinstance(raw, dict):
        ct = (raw.get("channel_type") or raw.get("type") or "").lower()
        if "voice" in ct:
            return "voice"
        if "text" in ct:
            return "text"
    return "unknown"

def build_existing_queues(current_channels):
    """Build lookup structures that preserve duplicates (Discord templates can have duplicate names)."""
    by_key = defaultdict(deque)   # (norm_name, kind) -> deque[channel]
    by_name = defaultdict(deque)  # norm_name -> deque[channel]
    for ch in current_channels:
        n = _norm_name(getattr(ch, "name", ""))
        k = _revolt_channel_kind(ch)
        by_key[(n, k)].append(ch)
        by_name[n].append(ch)
    return by_key, by_name

def save_progress():
    """Save current progress to file"""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(IDs, f, indent=2)
    
def load_progress():
    """Load progress from file if it exists"""
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, 'r') as f:
            loaded = json.load(f)
            IDs["roles"] = loaded.get("roles", {})
            IDs["channels"] = loaded.get("channels", {})
        return True
    return False

def d2r(type, id):
    return cache[type][IDs[type][id]]

def step(current, total=None, text="Something is wrong"):
    print(("\t" if total else ""), "[", current, "/", total or TEMPLATE_IMPORT_STEPS, "] ", text, sep='')

def convert_permission(permissions: int):
    d2r_map = {
        43:4, 40:8, 29:24, 28:3, 26:10, 27:11, 0:25, 1:6, 2:7, 4:0, 5:1,
        6:29,10:20,11:22,13:23,14:26,15:27,16:21,20:30,21:31,23:34,22:33,24:35
    }

    out = 0
    if isinstance(permissions, str):
        permissions = int(permissions)
    for i in d2r_map:
        if permissions & (1 << i):
            out |= 1 << d2r_map[i]
    return pyvolt.Permissions(out)

async def main():
    # Check for existing progress
    has_progress = load_progress()
    if has_progress:
        print(f"📂 Found previous progress: {len(IDs['channels'])} channels, {len(IDs['roles'])} roles")
        resume = input("Resume from previous run? (Y/n): ").strip().lower()
        if resume != 'n':
            print("✅ Resuming from saved progress...\n")
        else:
            IDs["roles"].clear()
            IDs["channels"].clear()
            print("🔄 Starting fresh...\n")
    
    # 1. Get the Template URL
    template_url = None
    template = None
    while not template:
        template_url = input("Template URL: ").split("/")[-1]
        if template_url == "":
            try:
                template = json.load(open("demo_template.json"))["serialized_source_guild"]
                break
            except:
                print("❌ demo_template.json not found.")
                continue

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://discord.com/",
                "Origin": "https://discord.com"
            }
            
            resp = requests.get(f"https://discord.com/api/v9/guilds/templates/{template_url}", headers=headers)
            
            if resp.status_code != 200:
                print(f"❌ API Error: {resp.status_code}")
                print(f"Response: {resp.text[:200]}") 
                template_url = None
                continue
            
            try:
                template = resp.json()["serialized_source_guild"]
            except (json.JSONDecodeError, KeyError) as e:
                print(f"❌ Failed to parse response: {e}")
                print(f"Response preview: {resp.text[:200]}")
                template_url = None
                continue
                
        except Exception as error: 
            print(f"❌ Error: {error}")
            template_url = None
            template = None

    print(f"""
    Ready to import template: "{template['name']}"
    {len(template['channels'])} channels and {len(template['roles'])} roles will be imported.
    """)
    
    # 2. Get Target Server Info
    target_server_id = input("Target Revolt Server ID: ")

    # 3. Initialize Client with proper async context manager
    bot_token = input("Revolt Bot Token: ")
    
    async with pyvolt.Client(token=bot_token, bot=True) as client:
        try:
            print(f"Fetching server {target_server_id}...")
            server = await client.fetch_server(target_server_id)
            print(f"✅ Connected to server: {server.name}")
        except Exception as error:
            print(f"❌ Error fetching server: {error}")
            print("Make sure the Bot is in the server and the ID is correct.")
            return

        # ═══════════════════════════════════════════════════════
        #     CRITICAL: FETCH ALL EXISTING CHANNELS (ROBUST)
        # ═══════════════════════════════════════════════════════
        print("\n🔍 Scanning server for existing channels...")
        
        current_channels = []
        
        # Method 1: Try standard library methods
        try:
            current_channels = await server.fetch_channels()
        except:
            pass
            
        if not current_channels and hasattr(server, 'channels'):
            current_channels = list(server.channels)
            
        if not current_channels and hasattr(server, '_channels'):
            current_channels = list(server._channels.values())

        # Method 2: DIRECT API FALLBACK (If library fails)
        if not current_channels:
            print("   ⚠️ Library failed to list channels. Attempting direct API fetch...")
            try:
                # Revolt does NOT provide GET /servers/{id}/channels.
                # Instead: GET /servers/{id} and then resolve its `channels` IDs.
                api_headers = {"x-bot-token": bot_token}

                server_data = revolt_api_json(
                    "GET",
                    f"https://api.revolt.chat/servers/{target_server_id}",
                    headers=api_headers,
                )

                channel_ids = server_data.get("channels", []) if isinstance(server_data, dict) else []

                for cid in channel_ids:
                    try:
                        ch_data = revolt_api_json(
                            "GET",
                            f"https://api.revolt.chat/channels/{cid}",
                            headers=api_headers,
                        )
                        if isinstance(ch_data, dict) and ch_data.get("_id"):
                            current_channels.append(RawChannel(ch_data))
                    except Exception:
                        # Ignore individual channel failures; keep scanning
                        pass

                print(f"   ✅ Direct API found {len(current_channels)} channels")
            except Exception as e:
                print(f"   ❌ Direct API error: {e}")

        print(f"✅ Found {len(current_channels)} existing channels on Revolt server\n")
        
        # Create a simple dict mapping channel name to channel object
        existing_by_name = {ch.name: ch for ch in current_channels}

        # Keep duplicates for safe matching (templates can contain same names)
        server_channel_ids = {ch.id for ch in current_channels}
        existing_by_key, existing_by_name_queue = build_existing_queues(current_channels)

        # Keep duplicates for safe matching (templates can contain same names)
        server_channel_ids = {ch.id for ch in current_channels}
        existing_by_key, existing_by_name_queue = build_existing_queues(current_channels)
        
        # Ask user for import mode
        print("╔════════════════════════════════════════════════════════╗")
        print("║                    IMPORT MODES                        ║")
        print("╚════════════════════════════════════════════════════════╝")
        print("1. 🚀 CATEGORIES ONLY - Fast! Just organize existing channels")
        print("2. 🔄 SMART MODE     - Reuse existing channels, create missing ones")
        print("3. 🗑️  CLEAN SLATE   - Delete everything and recreate from template")
        mode = input("\nChoose mode (1/2/3): ").strip()

        if mode == "1":
            # ═══════════════════════════════════════════════════════
            #           CATEGORIES ONLY MODE
            # ═══════════════════════════════════════════════════════
            print("\n🚀 CATEGORIES ONLY MODE\n")
            
            channels = template["channels"]
            textChannels = list(filter(lambda channel: channel["type"] not in [2, 4], channels))
            voiceChannels = list(filter(lambda channel: channel["type"] == 2, channels))
            categories = list(filter(lambda channel: channel["type"] == 4, channels))

            step(1, 2, "Mapping existing channels to template")

            mapped = 0
            not_found = 0
            used_revolt_ids = set()

            # Go through each channel in the template.
            # IMPORTANT: do NOT match solely by name -> templates can have duplicate channel names.
            for channel in textChannels + voiceChannels:
                channel_name = channel["name"]
                discord_id = channel["id"]
                desired_kind = "voice" if channel.get("type") == 2 else "text"

                # 1) Keep saved mapping if it's still valid and not already used
                revolt_id = IDs["channels"].get(discord_id)
                if revolt_id and revolt_id in server_channel_ids and revolt_id not in used_revolt_ids:
                    used_revolt_ids.add(revolt_id)
                    mapped += 1
                    continue

                # 2) Match by (name + kind) preserving duplicates
                key = (_norm_name(channel_name), desired_kind)
                q = existing_by_key.get(key)
                chosen = None
                if q:
                    while q and q[0].id in used_revolt_ids:
                        q.popleft()
                    if q:
                        chosen = q.popleft()

                # 3) Fallback: match by name only (still unique)
                if chosen is None:
                    qn = existing_by_name_queue.get(_norm_name(channel_name))
                    if qn:
                        while qn and qn[0].id in used_revolt_ids:
                            qn.popleft()
                        if qn:
                            chosen = qn.popleft()

                if chosen:
                    IDs["channels"][discord_id] = chosen.id
                    used_revolt_ids.add(chosen.id)
                    mapped += 1
                else:
                    not_found += 1

            print(f"    ✅ Mapped {mapped} channels")
            save_progress()

            step(2, 2, "Creating categories")
            
            # Build category structure (validated + de-duplicated)
            category_list = []
            assigned_in_categories = set()

            for category in categories:
                cat_channel_ids = []

                # Keep template order: iterate in the same order as the template channels list
                for ch in textChannels + voiceChannels:
                    if ch.get("parent_id") != category["id"]:
                        continue
                    discord_id = ch["id"]
                    revolt_id = IDs["channels"].get(discord_id)
                    if not revolt_id:
                        continue
                    # Validate: channel must actually exist in this Revolt server
                    if revolt_id not in server_channel_ids:
                        continue
                    # Revolt category structure expects each channel to appear at most once overall
                    if revolt_id in assigned_in_categories:
                        continue
                    assigned_in_categories.add(revolt_id)
                    cat_channel_ids.append(revolt_id)

                if cat_channel_ids:
                    category_list.append(
                        pyvolt.Category(
                            id=str(category["id"]),
                            title=(category["name"][:32]),
                            channels=cat_channel_ids
                        )
                    )

            if category_list:
                print(f"\n    📦 Applying {len(category_list)} categories...")
                try:
                    await server.edit(categories=category_list)
                    print(f"    ✅ Categories created successfully!")
                except Exception as e:
                    # Fallback: direct PATCH to the server edit endpoint
                    print(f"    ⚠️ server.edit failed ({e}). Trying direct PATCH...")
                    try:
                        api_headers = {"x-bot-token": bot_token, "Content-Type": "application/json"}
                        payload = {
                            "categories": [
                                {"id": c.id, "title": c.title, "channels": list(c.channels)}
                                for c in category_list
                            ]
                        }
                        resp = requests.patch(
                            f"https://api.revolt.chat/servers/{target_server_id}",
                            headers=api_headers,
                            json=payload,
                            timeout=30,
                        )
                        if resp.status_code == 200:
                            print("    ✅ Categories updated successfully via direct PATCH!")
                        else:
                            print(f"    ❌ Direct PATCH failed: {resp.status_code} - {resp.text}")
                    except Exception as e2:
                        print(f"    ❌ Direct PATCH exception: {e2}")
            else:
                print("    ⚠️  No categories created (did you map any channels?)")

        elif mode == "3":
            # ═══════════════════════════════════════════════════════
            #                CLEAN SLATE MODE
            # ═══════════════════════════════════════════════════════
            step(1, text="Deleting all existing channels")
            deleted = 0
            total = len(current_channels)
            for channel in current_channels:
                step(deleted+1, total, channel.name)
                try:
                    # If it's a RawChannel, we need to fetch the real one or use ID
                    if isinstance(channel, RawChannel):
                        # Try to use requests delete for raw channel
                        requests.delete(
                            f"https://api.revolt.chat/channels/{channel.id}",
                            headers={"x-bot-token": bot_token}
                        )
                    else:
                        await channel.close()
                except Exception as e:
                    print(f"    ⚠️  Failed to delete {channel.name}")
                deleted += 1
            
            current_channels = []
            existing_by_name = {}
            IDs["channels"].clear()
            IDs["roles"].clear()
            save_progress()
            mode = "2"  # Continue with smart mode

        if mode == "2" or mode == "3":
            # ═══════════════════════════════════════════════════════
            #                   SMART MODE
            # ═══════════════════════════════════════════════════════
            print("\n🔄 SMART MODE - Reusing existing channels\n")

            channels = template["channels"]
            textChannels = list(filter(lambda channel: channel["type"] not in [2, 4], channels))
            voiceChannels = list(filter(lambda channel: channel["type"] == 2, channels))
            categories = list(filter(lambda channel: channel["type"] == 4, channels))

            step(2, text="Processing channels")
            
            total = len(textChannels) + len(voiceChannels)
            created = 0
            reused = 0
            skipped = 0
            
            for i, channel in enumerate(textChannels + voiceChannels):
                channel_name = channel["name"]
                discord_id = channel["id"]
                
                # 1. Already processed?
                if discord_id in IDs["channels"]:
                    step(i+1, total, f"{channel_name} ✓ already done")
                    reused += 1
                    continue
                
                # 2. Exists on server?
                if channel_name in existing_by_name:
                    step(i+1, total, f"{channel_name} ✓ exists, reusing")
                    rChannel = existing_by_name[channel_name]
                    IDs["channels"][discord_id] = rChannel.id
                    # If it's a real object, cache it
                    if not isinstance(rChannel, RawChannel):
                        cache["channels"][rChannel.id] = rChannel
                    reused += 1
                    save_progress()
                    continue
                
                # 3. Create it
                try:
                    step(i+1, total, f"{channel_name} → creating...")
                    rChannel = await server.create_channel(
                        name = channel_name,
                        description = channel.get("topic", ""), 
                        nsfw = channel.get("nsfw", False),
                        type = (
                            pyvolt.ChannelType.voice 
                            if channel["type"] == 2 else 
                            pyvolt.ChannelType.text
                        )
                    )
                    IDs["channels"][discord_id] = rChannel.id
                    cache["channels"][rChannel.id] = rChannel
                    existing_by_name[channel_name] = rChannel
                    created += 1
                    save_progress()
                    
                except pyvolt.HTTPException as e:
                    if "TooManyChannels" in str(e):
                        step(i+1, total, f"{channel_name} ⚠️ LIMIT REACHED")
                        skipped += 1
                    else:
                        print(f"\n    ❌ Error creating {channel_name}: {e}")
                        skipped += 1
                except Exception as e:
                    print(f"\n    ❌ Unexpected error: {e}")
                    skipped += 1

            print(f"\n  📊 Summary: Created {created} | Reused {reused} | Skipped {skipped}")

            step(3, text="Setting up categories")

            category_list = []
            for category in categories:
                category_channels = [
                    IDs["channels"][channel["id"]]
                    for channel in textChannels + voiceChannels
                    if channel.get("parent_id") == category["id"] and channel["id"] in IDs["channels"]
                ]
                
                if category_channels:
                    category_list.append(
                        pyvolt.Category(
                            id = str(category["id"]),
                            title = category["name"],
                            channels = category_channels
                        )
                    )

            if category_list:
                await server.edit(categories=category_list)
                print(f"  ✅ Created {len(category_list)} categories")

            step(4, text="Creating/updating roles")
            
            # FIX: Find the @everyone role ID safely
            template_everyone_id = None
            for r in template["roles"]:
                if r["name"] == "@everyone":
                    template_everyone_id = r["id"]
                    break
            if template_everyone_id is None and "id" in template:
                template_everyone_id = template["id"]

            try:
                existing_roles = await server.fetch_roles()
            except:
                existing_roles = []
            
            existing_roles_by_name = {role.name: role for role in existing_roles}
            
            total_roles = len(template["roles"])
            for i, role in enumerate(template["roles"]):
                role_name = role["name"]
                discord_id = role["id"]
                step(i+1, total_roles, role_name)
                
                if discord_id == template_everyone_id:
                    await server.set_default_permissions(
                        convert_permission(role["permissions"])
                    )
                    continue
                
                if discord_id in IDs["roles"]:
                    rRole = next((r for r in existing_roles if r.id == IDs["roles"][discord_id]), None)
                    if not rRole and role_name in existing_roles_by_name:
                        rRole = existing_roles_by_name[role_name]
                elif role_name in existing_roles_by_name:
                    rRole = existing_roles_by_name[role_name]
                else:
                    rRole = await server.create_role(name=role_name, rank=role.get("position", 1))
                
                if rRole:
                    IDs["roles"][discord_id] = rRole.id
                    cache["roles"][rRole.id] = rRole
                    
                    color_hex = "#" + hex(role.get("color", 0))[2:].zfill(6)
                    await rRole.edit(color=color_hex, hoist=role.get("hoist", False))
                    await server.set_role_permissions(
                        rRole,
                        allow=convert_permission(role["permissions"]),
                        deny=pyvolt.Permissions(0)
                    )
                    save_progress()

            step(5, text="Setting channel permissions")

            channels_with_perms = [
                ch for ch in textChannels + voiceChannels
                if ch.get("permission_overwrites") and ch["id"] in IDs["channels"]
            ]
            
            for i, channel in enumerate(channels_with_perms):
                step(i+1, len(channels_with_perms), channel["name"])
                revolt_id = IDs["channels"][channel["id"]]
                
                # We need a REAL object to set permissions
                rChannel = cache["channels"].get(revolt_id)
                
                # If it's not in cache or is a RawChannel, we must fetch the real object
                if not rChannel or isinstance(rChannel, RawChannel):
                    try:
                        rChannel = await client.fetch_channel(revolt_id)
                        cache["channels"][revolt_id] = rChannel
                    except:
                        print(f"    ⚠️ Could not fetch channel {channel['name']} for permissions")
                        continue

                for overwrite in channel["permission_overwrites"]:
                    ow = {
                        "allow": convert_permission(overwrite.get("allow", 0)),
                        "deny": convert_permission(overwrite.get("deny", 0)),
                    }

                    if overwrite["id"] == template_everyone_id:
                        await rChannel.set_default_permissions(pyvolt.PermissionOverride(**ow))
                    elif overwrite["id"] in IDs["roles"]:
                        await rChannel.set_role_permissions(d2r("roles", overwrite["id"]), **ow)
            
            print("\n✅ Import complete!")
            if skipped > 0:
                print(f"\n⚠️  {skipped} channels skipped (200 channel limit reached)")

if __name__ == "__main__":
    asyncio.run(main())