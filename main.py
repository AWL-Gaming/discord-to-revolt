import json
import asyncio
import time
import random
import re
import os
import sys
from collections import defaultdict, deque
from pathlib import Path

# Third-party libraries
import requests
import stoat as pyvolt
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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

class RawRole:
    def __init__(self, id, data):
        self.id = id
        self.name = data.get("name", "Unknown")
        self.rank = data.get("rank", 0)
        self.color = data.get("colour", None)
        self.hoist = data.get("hoist", False)
        self._raw = data
    def __repr__(self):
        return f"<RawRole id={self.id} name={self.name}>"

def log(text, end="\n"):
    """Instant logging helper that flushes stdout."""
    print(text, end=end)
    sys.stdout.flush()

def revolt_api_json(method: str, url: str, headers: dict, payload: dict | None = None, params: dict | None = None, timeout: int = 30):
    """HTTP helper with SMART retry + 429 handling."""
    for attempt in range(6):
        try:
            resp = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
            
            # Rate Limit Handling
            if resp.status_code == 429:
                retry_after = 1.0 # Default fallback
                
                # Check Standard Header (Seconds)
                if "Retry-After" in resp.headers:
                    try: retry_after = float(resp.headers["Retry-After"])
                    except: pass
                
                # Check Revolt specific JSON body (Usually Milliseconds)
                try:
                    data = resp.json()
                    json_retry = data.get("retry_after")
                    if json_retry:
                        val = float(json_retry)
                        # Heuristic: If value is huge (>1000), it's milliseconds. 
                        if val > 500: 
                            val = val / 1000.0
                        
                        # Take the larger of the two to be safe
                        retry_after = max(retry_after, val)
                except:
                    pass
                
                log(f"    ⏳ Rate limit hit, waiting {retry_after:.2f}s...")
                time.sleep(retry_after + 0.1) # Tiny buffer
                continue
            
            # Server Error Handling
            if resp.status_code >= 500 and attempt < 5:
                time.sleep(1)
                continue
            
            # Client Error Handling
            if resp.status_code >= 400:
                return {"error": resp.text, "status": resp.status_code}
            
            return resp.json()
        except Exception as e:
            if attempt == 5:
                return {"error": str(e), "status": 0}
            time.sleep(1)

def _norm_name(name: str) -> str:
    return (name or "").casefold().strip()

def _strip_name(name: str) -> str:
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
    by_key = defaultdict(deque)
    by_name = defaultdict(deque)
    by_stripped = defaultdict(deque)
    
    for ch in current_channels:
        name_raw = getattr(ch, "name", "")
        n = _norm_name(name_raw)
        s = _strip_name(name_raw)
        k = _revolt_channel_kind(ch)
        
        by_key[(n, k)].append(ch)
        by_name[n].append(ch)
        if s: by_stripped[s].append(ch)
        
    return by_key, by_name, by_stripped

def save_progress(force=False):
    if not hasattr(save_progress, "counter"): save_progress.counter = 0
    save_progress.counter += 1
    if force or save_progress.counter % 10 == 0:
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
    log(("\t" if total else "") + f"[{current}/{total or TEMPLATE_IMPORT_STEPS}] {text}")

def convert_permission(permissions: int):
    d2r_map = {43:4, 40:8, 29:24, 28:3, 26:10, 27:11, 0:25, 1:6, 2:7, 4:0, 5:1, 6:29,10:20,11:22,13:23,14:26,15:27,16:21,20:30,21:31,23:34,22:33,24:35}
    out = 0
    if isinstance(permissions, str): permissions = int(permissions)
    for i in d2r_map:
        if permissions & (1 << i): out |= 1 << d2r_map[i]
    return pyvolt.Permissions(out)

# ═══════════════════════════════════════════════════════
#  CORE LOGIC: PROCESS ROLES (Responsive)
# ═══════════════════════════════════════════════════════
async def process_roles_logic(server, template, target_server_id, bot_token):
    log("    🔍 Fetching roles via Direct API...")
    raw_roles = []
    try:
        server_data = revolt_api_json("GET", f"https://api.revolt.chat/servers/{target_server_id}", headers={"x-bot-token": bot_token})
        if isinstance(server_data, dict) and "roles" in server_data:
            for r_id, r_data in server_data["roles"].items():
                raw_roles.append(RawRole(r_id, r_data))
        else:
            lib_roles = await server.fetch_roles()
            raw_roles = [RawRole(r.id, {"name": r.name, "rank": r.rank, "colour": r.color, "hoist": r.hoist}) for r in lib_roles]
    except Exception as e:
        log(f"    ❌ Failed: {e}")
        return

    log(f"    ✅ Found {len(raw_roles)} existing roles.")

    # --- 1. DUPLICATE CLEANUP ---
    log("    🧹 Analyzing duplicates...")
    roles_by_name = defaultdict(list)
    for r in raw_roles:
        roles_by_name[_norm_name(r.name)].append(r)
    
    ids_to_delete = []
    cleaned_roles = []
    
    for norm_name, r_list in roles_by_name.items():
        if len(r_list) > 1:
            keep = r_list[0]
            cleaned_roles.append(keep)
            for bad in r_list[1:]:
                if bad.id == target_server_id: continue 
                ids_to_delete.append((bad.id, bad.name))
        else:
            cleaned_roles.append(r_list[0])

    if ids_to_delete:
        log(f"    🗑️  Deleting {len(ids_to_delete)} duplicate roles...")
        
        for i, (rid, rname) in enumerate(ids_to_delete):
            revolt_api_json("DELETE", f"https://api.revolt.chat/servers/{target_server_id}/roles/{rid}", headers={"x-bot-token": bot_token})
            
            # RESPONSIVE LOGGING: Overwrite current line
            sys.stdout.write(f"\r       Deleted {i+1}/{len(ids_to_delete)} duplicates... ({rname})          ")
            sys.stdout.flush()
            
        log(f"\n       ✅ Cleanup finished.        ")

    existing_roles_map = {_norm_name(r.name): r for r in cleaned_roles}
    
    # --- 2. CREATE / REUSE ---
    template_everyone_id = None
    for r in template["roles"]:
        if r["name"] == "@everyone": template_everyone_id = r["id"]; break
    if template_everyone_id is None: template_everyone_id = template.get("id")

    total_roles = len(template["roles"])
    
    log("    ⚙️  Syncing roles...")
    for i, role in enumerate(template["roles"]):
        role_name = role["name"]
        norm_input_name = _norm_name(role_name)
        
        if role["id"] == template_everyone_id:
            log(f"    [{i+1}/{total_roles}] @everyone -> Updating perms")
            await server.set_default_permissions(convert_permission(role["permissions"]))
            continue

        rRole = None
        status = "Creating"

        if role["id"] in IDs["roles"]:
            rid = IDs["roles"][role["id"]]
            found = next((r for r in cleaned_roles if r.id == rid), None)
            if found: 
                rRole = found
                status = "Reusing"
        
        if not rRole and norm_input_name in existing_roles_map:
            rRole = existing_roles_map[norm_input_name]
            status = "Reusing"

        if status == "Creating" or i % 10 == 0:
            log(f"    [{i+1}/{total_roles}] {role_name} -> {status}")

        if not rRole:
            try:
                payload = {
                    "name": role["name"],
                    "rank": role.get("position", 0)
                }
                resp = revolt_api_json("POST", f"https://api.revolt.chat/servers/{target_server_id}/roles", headers={"x-bot-token": bot_token}, payload=payload)
                
                if isinstance(resp, dict) and "id" in resp:
                    new_id = resp["id"]
                    rRole = RawRole(new_id, {"name": role["name"], "rank": 0}) 
                    new_raw = rRole
                    cleaned_roles.append(new_raw)
                    existing_roles_map[_norm_name(role["name"])] = new_raw
                else:
                    log(f"      ❌ API Error creating role: {resp}")
                    continue
            except Exception as e:
                log(f"      ❌ Create Failed: {e}")
                continue
        else:
            # Optimization: Skip if colors match
            target_color = "#" + hex(role.get("color", 0))[2:].zfill(6)
            current_color = getattr(rRole, "color", None)
            
            if current_color and current_color.lower() == target_color.lower():
                IDs["roles"][role["id"]] = rRole.id
                cache["roles"][rRole.id] = rRole
                continue

            if isinstance(rRole, RawRole):
                try: rRole = await server.fetch_role(rRole.id)
                except: continue

        if rRole:
            IDs["roles"][role["id"]] = rRole.id
            cache["roles"][rRole.id] = rRole
            
            try:
                color = "#" + hex(role.get("color", 0))[2:].zfill(6)
                await rRole.edit(color=color, hoist=role.get("hoist", False))
                await server.set_role_permissions(rRole, allow=convert_permission(role["permissions"]), deny=pyvolt.Permissions(0))
            except: pass
            
            save_progress()

    save_progress(force=True)


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
    
    template_url = os.getenv("DISCORD_TEMPLATE_URL")
    template = None
    while not template:
        if not template_url: template_url = input("Template URL: ")
        code = template_url.split("/")[-1]
        if not code:
            try: template = json.load(open("demo_template.json"))["serialized_source_guild"]; break
            except: print("❌ demo_template.json not found."); template_url=None; continue
        try:
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            resp = requests.get(f"https://discord.com/api/v9/guilds/templates/{code}", headers=headers)
            if resp.status_code != 200: print(f"❌ API Error: {resp.status_code}"); template_url=None; continue
            template = resp.json()["serialized_source_guild"]
        except: template_url=None

    print(f"Ready to import: {template['name']}")
    
    target_server_id = os.getenv("REVOLT_SERVER_ID") or input("Target Revolt Server ID: ")
    bot_token = os.getenv("REVOLT_BOT_TOKEN") or input("Revolt Bot Token: ")
    
    async with pyvolt.Client(token=bot_token, bot=True) as client:
        try:
            print(f"Fetching server {target_server_id}...")
            server = await client.fetch_server(target_server_id)
            print(f"✅ Connected to: {server.name}")
        except: return

        print("\n🔍 Scanning server...")
        current_channels = []
        try:
            headers = {"x-bot-token": bot_token}
            data = revolt_api_json("GET", f"https://api.revolt.chat/servers/{target_server_id}", headers=headers, params={"include_channels": "true"})
            if isinstance(data, dict) and "channels" in data:
                for d in data["channels"]:
                    if isinstance(d, dict): current_channels.append(RawChannel(d))
        except: pass
        if not current_channels: 
             if hasattr(server, 'channels'): current_channels = list(server.channels)

        print(f"✅ Found {len(current_channels)} channels")
        server_channel_ids = {ch.id for ch in current_channels}
        existing_by_key, existing_by_name_queue, existing_by_stripped_queue = build_existing_queues(current_channels)
        
        print("\n1. 🚀 CATEGORIES ONLY")
        print("2. 🔄 SMART MODE (Recommended)")
        print("3. 🗑️  CLEAN SLATE")
        print("4. 🎭 ROLES ONLY")
        mode = input("Choose mode (1-4): ").strip()

        channels = template["channels"]
        textChannels = list(filter(lambda channel: channel["type"] not in [2, 4], channels))
        voiceChannels = list(filter(lambda channel: channel["type"] == 2, channels))
        categories = list(filter(lambda channel: channel["type"] == 4, channels))

        if mode == "4":
            step(1, 1, "Processing Roles")
            await process_roles_logic(server, template, target_server_id, bot_token)
            print("\n✅ Role Sync Complete!")
            return

        if mode == "3":
            step(1, text="Deleting channels")
            for ch in current_channels:
                try:
                    if isinstance(ch, RawChannel): requests.delete(f"https://api.revolt.chat/channels/{ch.id}", headers={"x-bot-token": bot_token})
                    else: await ch.close()
                except: pass
            current_channels = []
            existing_by_key = defaultdict(deque); existing_by_name_queue = defaultdict(deque); existing_by_stripped_queue = defaultdict(deque)
            server_channel_ids = set()
            IDs["channels"].clear(); IDs["roles"].clear()
            save_progress(force=True)
            mode = "2"

        if mode == "1" or mode == "2":
            step(2, text="Processing channels")
            total = len(textChannels) + len(voiceChannels)
            created, reused, skipped = 0, 0, 0
            used_revolt_ids = set()
            
            for i, channel in enumerate(textChannels + voiceChannels):
                cname, cid = channel["name"], channel["id"]
                kind = "voice" if channel.get("type")==2 else "text"
                
                revolt_id = IDs["channels"].get(cid)
                if revolt_id and revolt_id in server_channel_ids and revolt_id not in used_revolt_ids:
                    used_revolt_ids.add(revolt_id); reused += 1; continue

                key = (_norm_name(cname), kind)
                chosen = None
                
                q = existing_by_key.get(key)
                if q: 
                    while q and q[0].id in used_revolt_ids: q.popleft()
                    if q: chosen = q.popleft()
                
                if not chosen:
                    qn = existing_by_name_queue.get(_norm_name(cname))
                    if qn:
                        while qn and qn[0].id in used_revolt_ids: qn.popleft()
                        if qn: chosen = qn.popleft()
                
                if not chosen:
                    qs = existing_by_stripped_queue.get(_strip_name(cname))
                    if qs:
                        while qs and qs[0].id in used_revolt_ids: qs.popleft()
                        if qs: chosen = qs.popleft()

                if chosen:
                    if i%5==0: step(i+1, total, f"{cname} ✓ reused")
                    IDs["channels"][cid] = chosen.id; used_revolt_ids.add(chosen.id); reused += 1; save_progress()
                    continue
                
                if mode == "2":
                    try:
                        step(i+1, total, f"{cname} → creating...")
                        rChannel = await server.create_channel(
                            name=cname, description=channel.get("topic",""), nsfw=channel.get("nsfw",False),
                            type=(pyvolt.ChannelType.voice if kind=="voice" else pyvolt.ChannelType.text)
                        )
                        IDs["channels"][cid] = rChannel.id; used_revolt_ids.add(rChannel.id); server_channel_ids.add(rChannel.id)
                        created += 1; save_progress()
                    except pyvolt.HTTPException as e:
                        if "TooManyChannels" in str(e): step(i+1, total, f"{cname} ⚠️ SERVER FULL"); skipped += 1
                        else: log(f" ❌ Error: {e}"); skipped += 1
                    except: skipped += 1

            save_progress(force=True)
            print(f"\n  📊 Summary: Created {created} | Reused {reused} | Skipped {skipped}")

            step(3, text="Categories")
            category_list = []
            assigned = set()
            for i, cat in enumerate(categories):
                ch_ids = []
                for ch in textChannels + voiceChannels:
                    if ch.get("parent_id") != cat["id"]: continue
                    rid = IDs["channels"].get(ch["id"])
                    if rid and rid in server_channel_ids and rid not in assigned:
                        assigned.add(rid); ch_ids.append(rid)
                if ch_ids:
                    log(f"    [Staged {i+1}/{len(categories)}] {cat['name'][:32]}")
                    category_list.append({"id": generate_ulid(), "title": cat["name"][:32], "channels": ch_ids})

            if category_list:
                log(f"  📦 Sending categories...")
                try: requests.patch(f"https://api.revolt.chat/servers/{target_server_id}", headers={"x-bot-token": bot_token}, json={"categories": category_list})
                except: pass

            step(4, text="Processing roles")
            await process_roles_logic(server, template, target_server_id, bot_token)

            step(5, text="Permissions")
            channels_with_perms = [ch for ch in textChannels + voiceChannels if ch.get("permission_overwrites") and ch["id"] in IDs["channels"]]
            for i, ch in enumerate(channels_with_perms):
                if i % 10 == 0: log(f"    Setting perms for batch {i}...", end="\r")
                rID = IDs["channels"][ch["id"]]
                rChannel = cache["channels"].get(rID)
                if not rChannel or isinstance(rChannel, RawChannel):
                    try: rChannel = await client.fetch_channel(rID); cache["channels"][rID] = rChannel
                    except: continue
                
                template_everyone_id = None
                for r in template["roles"]:
                    if r["name"] == "@everyone": template_everyone_id = r["id"]; break
                if not template_everyone_id: template_everyone_id = template.get("id")

                for ow in ch["permission_overwrites"]:
                    p = {"allow": convert_permission(ow.get("allow",0)), "deny": convert_permission(ow.get("deny",0))}
                    if ow["id"] == template_everyone_id: await rChannel.set_default_permissions(pyvolt.PermissionOverride(**p))
                    elif ow["id"] in IDs["roles"]: await rChannel.set_role_permissions(d2r("roles", ow["id"]), **p)
                time.sleep(0.1) # Fast Pacing

            print("\n✅ Import complete!")
            if skipped > 0: print(f"\n⚠️  {skipped} channels skipped (200 limit).")

if __name__ == "__main__":
    asyncio.run(main())