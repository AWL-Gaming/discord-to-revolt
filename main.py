import json
import asyncio
import time
import random
import re
from collections import defaultdict, deque
import requests
import stoat as pyvolt
from pathlib import Path

TEMPLATE_IMPORT_STEPS = 5
PROGRESS_FILE = "import_progress.json"
CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

IDs = {"roles": {}, "channels": {}}
cache = {"roles": {}, "channels": {}}

# --- HELPER: Generate Valid Revolt IDs (ULIDs) ---
def generate_ulid():
    """Generates a valid ULID (26 chars) for Revolt categories"""
    t = int(time.time() * 1000)
    chars = []
    for _ in range(10):
        chars.append(CROCKFORD_BASE32[t & 31])
        t >>= 5
    chars.reverse()
    for _ in range(16):
        chars.append(random.choice(CROCKFORD_BASE32))
    return "".join(chars)

class RawChannel:
    def __init__(self, data):
        self.id = data["_id"]
        self.name = data.get("name", "Unknown")
        self.type = data.get("channel_type", "Text")
        self._raw = data
    def __repr__(self):
        return f"<RawChannel id={self.id} name={self.name}>"

def revolt_api_json(method: str, url: str, headers: dict, payload: dict | None = None, params: dict | None = None, timeout: int = 30):
    """HTTP helper with retry + 429 handling + params support."""
    for attempt in range(6):
        try:
            resp = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
            if resp.status_code == 429:
                try:
                    data = resp.json()
                    retry_after = float(data.get("retry_after", 2.0))
                except:
                    retry_after = 2.0
                print(f"    ⏳ Rate limit hit, waiting {retry_after}s...")
                time.sleep(retry_after + 0.5)
                continue
            if resp.status_code >= 500 and attempt < 5:
                time.sleep(1)
                continue
            if resp.status_code >= 400:
                return {"error": resp.text, "status": resp.status_code}
            return resp.json()
        except Exception as e:
            if attempt == 5:
                return {"error": str(e), "status": 0}
            time.sleep(1)

def _norm_name(name: str) -> str:
    """Basic normalization (lowercase, stripped)."""
    return (name or "").casefold().strip()

def _strip_name(name: str) -> str:
    """Aggressive normalization: removes emojis/symbols to match 'Factorio' with '⚙️Factorio⚙️'."""
    return re.sub(r'[\W_]+', '', name).lower()

def _revolt_channel_kind(ch) -> str:
    t = getattr(ch, "type", None)
    if isinstance(t, str):
        if "voice" in t.lower(): return "voice"
        if "text" in t.lower(): return "text"
    try:
        if t == pyvolt.ChannelType.voice: return "voice"
        if t == pyvolt.ChannelType.text: return "text"
    except: pass
    raw = getattr(ch, "_raw", None)
    if isinstance(raw, dict):
        ct = (raw.get("channel_type") or raw.get("type") or "").lower()
        if "voice" in ct: return "voice"
        if "text" in ct: return "text"
    return "unknown"

def build_existing_queues(current_channels):
    by_key = defaultdict(deque)      # (norm_name, kind)
    by_name = defaultdict(deque)     # norm_name
    by_stripped = defaultdict(deque) # stripped_name (fuzzy match)
    
    for ch in current_channels:
        name_raw = getattr(ch, "name", "")
        n = _norm_name(name_raw)
        s = _strip_name(name_raw)
        k = _revolt_channel_kind(ch)
        
        by_key[(n, k)].append(ch)
        by_name[n].append(ch)
        if s: by_stripped[s].append(ch)
        
    return by_key, by_name, by_stripped

def save_progress():
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(IDs, f, indent=2)
    
def load_progress():
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
    d2r_map = {43:4, 40:8, 29:24, 28:3, 26:10, 27:11, 0:25, 1:6, 2:7, 4:0, 5:1, 6:29,10:20,11:22,13:23,14:26,15:27,16:21,20:30,21:31,23:34,22:33,24:35}
    out = 0
    if isinstance(permissions, str): permissions = int(permissions)
    for i in d2r_map:
        if permissions & (1 << i): out |= 1 << d2r_map[i]
    return pyvolt.Permissions(out)

async def main():
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
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            resp = requests.get(f"https://discord.com/api/v9/guilds/templates/{template_url}", headers=headers)
            if resp.status_code != 200:
                print(f"❌ API Error: {resp.status_code}")
                template_url = None
                continue
            template = resp.json()["serialized_source_guild"]
        except Exception as error: 
            print(f"❌ Error: {error}")
            template_url = None
            template = None

    print(f"""
    Ready to import template: "{template['name']}"
    {len(template['channels'])} channels and {len(template['roles'])} roles will be imported.
    """)
    
    target_server_id = input("Target Revolt Server ID: ")
    bot_token = input("Revolt Bot Token: ")
    
    async with pyvolt.Client(token=bot_token, bot=True) as client:
        try:
            print(f"Fetching server {target_server_id}...")
            server = await client.fetch_server(target_server_id)
            print(f"✅ Connected to server: {server.name}")
        except Exception as error:
            print(f"❌ Error fetching server: {error}")
            return

        print("\n🔍 Scanning server for existing channels...")
        current_channels = []
        
        # Optimized Bulk Fetch
        try:
            api_headers = {"x-bot-token": bot_token}
            server_data = revolt_api_json(
                "GET", 
                f"https://api.revolt.chat/servers/{target_server_id}", 
                headers=api_headers,
                params={"include_channels": "true"}
            )
            
            if isinstance(server_data, dict):
                if "channels" in server_data and isinstance(server_data["channels"], list):
                    raw_list = server_data["channels"]
                    if len(raw_list) > 0 and isinstance(raw_list[0], dict):
                        for ch_data in raw_list:
                            current_channels.append(RawChannel(ch_data))
                    elif len(raw_list) > 0 and isinstance(raw_list[0], str):
                        print(f"   ℹ️ API returned IDs only. Fetching {len(raw_list)} channels...")
                        for cid in raw_list:
                            ch_data = revolt_api_json("GET", f"https://api.revolt.chat/channels/{cid}", headers=api_headers)
                            if isinstance(ch_data, dict) and ch_data.get("_id"):
                                current_channels.append(RawChannel(ch_data))
        except Exception as e:
            print(f"   ❌ API error: {e}")

        # Fallback to library cache
        if not current_channels:
             if hasattr(server, 'channels'): current_channels = list(server.channels)
             elif hasattr(server, '_channels'): current_channels = list(server._channels.values())

        print(f"✅ Found {len(current_channels)} existing channels on Revolt server")
        if len(current_channels) >= 190:
            print("⚠️  WARNING: Server is nearing the 200 channel limit. Fuzzy matching will try to reuse existing channels to avoid errors.")
        
        server_channel_ids = {ch.id for ch in current_channels}
        existing_by_key, existing_by_name_queue, existing_by_stripped_queue = build_existing_queues(current_channels)
        
        print("\n╔════════════════════════════════════════════════════════╗")
        print("║                    IMPORT MODES                        ║")
        print("╚════════════════════════════════════════════════════════╝")
        print("1. 🚀 CATEGORIES ONLY - Fast! Just organize existing channels")
        print("2. 🔄 SMART MODE     - Reuse existing channels, create missing ones")
        print("3. 🗑️  CLEAN SLATE   - Delete everything and recreate from template")
        mode = input("\nChoose mode (1/2/3): ").strip()

        channels = template["channels"]
        textChannels = list(filter(lambda channel: channel["type"] not in [2, 4], channels))
        voiceChannels = list(filter(lambda channel: channel["type"] == 2, channels))
        categories = list(filter(lambda channel: channel["type"] == 4, channels))

        if mode == "3":
            step(1, text="Deleting channels")
            for ch in current_channels:
                try:
                    if isinstance(ch, RawChannel):
                        requests.delete(f"https://api.revolt.chat/channels/{ch.id}", headers={"x-bot-token": bot_token})
                    else:
                        await ch.close()
                except: pass
            current_channels = []
            existing_by_key, existing_by_name_queue, existing_by_stripped_queue = defaultdict(deque), defaultdict(deque), defaultdict(deque)
            server_channel_ids = set()
            IDs["channels"].clear()
            IDs["roles"].clear()
            save_progress()
            mode = "2"

        if mode == "1" or mode == "2":
            mode_name = "SMART MODE" if mode == "2" else "CATEGORIES ONLY"
            print(f"\n🔄 {mode_name}\n")
            
            step(2, text="Processing channels")
            total = len(textChannels) + len(voiceChannels)
            created = 0
            reused = 0
            skipped = 0
            used_revolt_ids = set()
            
            for i, channel in enumerate(textChannels + voiceChannels):
                channel_name = channel["name"]
                discord_id = channel["id"]
                desired_kind = "voice" if channel.get("type") == 2 else "text"
                
                # 1. Check Previous Mapping
                revolt_id = IDs["channels"].get(discord_id)
                if revolt_id and revolt_id in server_channel_ids and revolt_id not in used_revolt_ids:
                    step(i+1, total, f"{channel_name} ✓ mapped reused")
                    used_revolt_ids.add(revolt_id)
                    reused += 1
                    continue

                # 2. Strict Match
                key = (_norm_name(channel_name), desired_kind)
                q = existing_by_key.get(key)
                chosen = None
                if q:
                    while q and q[0].id in used_revolt_ids: q.popleft()
                    if q: chosen = q.popleft()
                
                # 3. Name Match
                if chosen is None:
                    qn = existing_by_name_queue.get(_norm_name(channel_name))
                    if qn:
                        while qn and qn[0].id in used_revolt_ids: qn.popleft()
                        if qn: chosen = qn.popleft()

                # 4. Fuzzy Match
                if chosen is None:
                    qs = existing_by_stripped_queue.get(_strip_name(channel_name))
                    if qs:
                        while qs and qs[0].id in used_revolt_ids: qs.popleft()
                        if qs: chosen = qs.popleft()

                if chosen:
                    step(i+1, total, f"{channel_name} ✓ exists (fuzzy), reusing")
                    IDs["channels"][discord_id] = chosen.id
                    used_revolt_ids.add(chosen.id)
                    reused += 1
                    save_progress()
                    continue
                
                if mode == "2":
                    try:
                        step(i+1, total, f"{channel_name} → creating...")
                        rChannel = await server.create_channel(
                            name = channel_name, 
                            description = channel.get("topic", ""), 
                            nsfw = channel.get("nsfw", False),
                            type = (pyvolt.ChannelType.voice if channel["type"]==2 else pyvolt.ChannelType.text)
                        )
                        IDs["channels"][discord_id] = rChannel.id
                        used_revolt_ids.add(rChannel.id)
                        server_channel_ids.add(rChannel.id)
                        created += 1
                        save_progress()
                    except pyvolt.HTTPException as e:
                        if "TooManyChannels" in str(e):
                            step(i+1, total, f"{channel_name} ⚠️ SERVER FULL (Limit Reached)")
                            skipped += 1
                        else:
                            print(f" ❌ Error: {e}")
                            skipped += 1
                    except Exception as e:
                        print(f" ❌ Error: {e}")
                        skipped += 1

            print(f"\n  📊 Summary: Created {created} | Reused {reused} | Skipped {skipped}")

            step(3, text="Setting up categories")
            category_list = []
            assigned_in_categories = set()

            for i, category in enumerate(categories):
                cat_channel_ids = []
                for ch in textChannels + voiceChannels:
                    if ch.get("parent_id") != category["id"]: continue
                    r_id = IDs["channels"].get(ch["id"])
                    if not r_id: continue
                    if r_id not in server_channel_ids: continue
                    if r_id in assigned_in_categories: continue
                    
                    assigned_in_categories.add(r_id)
                    cat_channel_ids.append(r_id)

                if cat_channel_ids:
                    print(f"    [Staged {i+1}/{len(categories)}] {category['name'][:32]} ({len(cat_channel_ids)} channels)")
                    category_list.append({
                        "id": generate_ulid(),
                        "title": category["name"][:32],
                        "channels": cat_channel_ids
                    })

            if category_list:
                print(f"  📦 Sending {len(category_list)} categories via Direct API...")
                try:
                    api_headers = {"x-bot-token": bot_token, "Content-Type": "application/json"}
                    payload = {"categories": category_list}
                    resp = requests.patch(
                        f"https://api.revolt.chat/servers/{target_server_id}",
                        headers=api_headers, json=payload, timeout=30
                    )
                    if resp.status_code == 200:
                        print("  ✅ Categories updated successfully!")
                    else:
                        print(f"  ❌ Failed: {resp.status_code} - {resp.text}")
                except Exception as e:
                    print(f"  ❌ Exception: {e}")

            step(4, text="Creating roles")
            template_everyone_id = None
            for r in template["roles"]:
                if r["name"] == "@everyone":
                    template_everyone_id = r["id"]; break
            if template_everyone_id is None and "id" in template: template_everyone_id = template["id"]

            try: existing_roles = await server.fetch_roles()
            except: existing_roles = []
            existing_roles_by_name = {r.name: r for r in existing_roles}
            
            total_roles = len(template["roles"])
            for i, role in enumerate(template["roles"]):
                role_name = role["name"]
                
                if role["id"] == template_everyone_id:
                    print(f"    [{i+1}/{total_roles}] @everyone -> Updating perms")
                    await server.set_default_permissions(convert_permission(role["permissions"]))
                    continue

                rRole = None
                status = "Creating"

                # Check if we should reuse
                if role["id"] in IDs["roles"]:
                    rRole = next((r for r in existing_roles if r.id == IDs["roles"][role["id"]]), None)
                    if not rRole and role["name"] in existing_roles_by_name:
                        rRole = existing_roles_by_name[role["name"]]
                    if rRole: status = "Reusing"
                elif role["name"] in existing_roles_by_name:
                    rRole = existing_roles_by_name[role["name"]]
                    status = "Reusing"
                
                print(f"    [{i+1}/{total_roles}] {role_name} -> {status}")

                if not rRole:
                    rRole = await server.create_role(name=role["name"], rank=role.get("position", 1))
                
                if rRole:
                    IDs["roles"][role["id"]] = rRole.id
                    cache["roles"][rRole.id] = rRole
                    color = "#" + hex(role.get("color", 0))[2:].zfill(6)
                    await rRole.edit(color=color, hoist=role.get("hoist", False))
                    await server.set_role_permissions(rRole, allow=convert_permission(role["permissions"]), deny=pyvolt.Permissions(0))
                    save_progress()

            step(5, text="Permissions")
            channels_with_perms = [ch for ch in textChannels + voiceChannels if ch.get("permission_overwrites") and ch["id"] in IDs["channels"]]
            for i, ch in enumerate(channels_with_perms):
                print(f"    [{i+1}/{len(channels_with_perms)}] {ch['name']} -> Setting overrides")
                rID = IDs["channels"][ch["id"]]
                rChannel = cache["channels"].get(rID)
                if not rChannel or isinstance(rChannel, RawChannel):
                    try: rChannel = await client.fetch_channel(rID); cache["channels"][rID] = rChannel
                    except: continue
                
                for ow in ch["permission_overwrites"]:
                    p = {"allow": convert_permission(ow.get("allow",0)), "deny": convert_permission(ow.get("deny",0))}
                    if ow["id"] == template_everyone_id: await rChannel.set_default_permissions(pyvolt.PermissionOverride(**p))
                    elif ow["id"] in IDs["roles"]: await rChannel.set_role_permissions(d2r("roles", ow["id"]), **p)
            
            print("\n✅ Import complete!")
            if skipped > 0:
                print(f"\n⚠️  {skipped} channels skipped because the server is full (200 limit).")

if __name__ == "__main__":
    asyncio.run(main())