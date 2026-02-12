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
    """Instant output flushing"""
    print(text, end=end)
    sys.stdout.flush()

def revolt_api_json(method: str, url: str, headers: dict, payload: dict | None = None, params: dict | None = None, timeout: int = 30):
    """HTTP helper with retry + 429 handling."""
    for attempt in range(6):
        try:
            resp = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
            if resp.status_code == 429:
                retry_after = 1.0
                if "Retry-After" in resp.headers:
                    try: retry_after = float(resp.headers["Retry-After"])
                    except: pass
                try:
                    data = resp.json()
                    json_retry = data.get("retry_after")
                    if json_retry:
                        val = float(json_retry)
                        if val > 500: val = val / 1000.0
                        retry_after = max(retry_after, val)
                except: pass
                
                log(f"    ⏳ Rate limit hit, waiting {retry_after:.2f}s...")
                time.sleep(retry_after + 0.1)
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
        with open(PROGRESS_FILE, 'w') as f: json.dump(IDs, f, indent=2)
    
def load_progress():
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, 'r') as f:
            loaded = json.load(f)
            IDs["roles"] = loaded.get("roles", {})
            IDs["channels"] = loaded.get("channels", {})
        return True
    return False

def step(current, total=None, text="Something is wrong"):
    log(("\t" if total else "") + f"[{current}/{total or TEMPLATE_IMPORT_STEPS}] {text}")

def convert_permission(permissions: int):
    # Mapping Discord Permissions (Bit) -> Revolt Permissions (Bit)
    # Based on community mappings
    d2r_map = {
        43:4, 40:8, 29:24, 28:3, 26:10, 27:11, 0:25, 1:6, 2:7, 4:0, 5:1,
        6:29, 10:20, 11:22, 13:23, 14:26, 15:27, 16:21, 20:30, 21:31, 23:34, 22:33, 24:35
    }
    out = 0
    if isinstance(permissions, str): permissions = int(permissions)
    for i in d2r_map:
        if permissions & (1 << i): out |= 1 << d2r_map[i]
    return pyvolt.Permissions(out)

# ═══════════════════════════════════════════════════════
#  ROLE PROCESSING (Centralized)
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
        log(f"    ❌ Failed: {e}"); return

    # Duplicate Cleanup
    log(f"    ✅ Found {len(raw_roles)} existing roles. Checking duplicates...")
    roles_by_name = defaultdict(list)
    for r in raw_roles: roles_by_name[_norm_name(r.name)].append(r)
    
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
            sys.stdout.write(f"\r       Deleted {i+1}/{len(ids_to_delete)} duplicates... ({rname})          ")
            sys.stdout.flush()
        log(f"\n       ✅ Cleanup finished.        ")

    existing_roles_map = {_norm_name(r.name): r for r in cleaned_roles}
    
    # Sync Logic
    template_everyone_id = None
    for r in template["roles"]:
        if r["name"] == "@everyone": template_everyone_id = r["id"]; break
    if template_everyone_id is None: template_everyone_id = template.get("id")

    log("    ⚙️  Syncing roles...")
    for i, role in enumerate(template["roles"]):
        role_name = role["name"]
        norm_input_name = _norm_name(role_name)
        
        if role["id"] == template_everyone_id:
            await server.set_default_permissions(convert_permission(role["permissions"]))
            continue

        rRole = None
        status = "Creating"

        if role["id"] in IDs["roles"]:
            rid = IDs["roles"][role["id"]]
            found = next((r for r in cleaned_roles if r.id == rid), None)
            if found: rRole = found; status = "Reusing"
        
        if not rRole and norm_input_name in existing_roles_map:
            rRole = existing_roles_map[norm_input_name]
            status = "Reusing"

        if status == "Creating" or i % 10 == 0:
            log(f"    [{i+1}/{len(template['roles'])}] {role_name} -> {status}")

        if not rRole:
            try:
                payload = {"name": role["name"], "rank": role.get("position", 0)}
                resp = revolt_api_json("POST", f"https://api.revolt.chat/servers/{target_server_id}/roles", headers={"x-bot-token": bot_token}, payload=payload)
                if isinstance(resp, dict) and "id" in resp:
                    rRole = RawRole(resp["id"], {"name": role["name"], "rank": 0}) 
                    cleaned_roles.append(rRole)
                    existing_roles_map[_norm_name(role["name"])] = rRole
                else:
                    log(f"      ❌ API Error: {resp}"); continue
            except: continue
        else:
            # Optimization: Skip if colors match
            target_color = "#" + hex(role.get("color", 0))[2:].zfill(6)
            current_color = getattr(rRole, "color", None)
            if current_color and current_color.lower() == target_color.lower():
                IDs["roles"][role["id"]] = rRole.id
                continue

        if rRole:
            IDs["roles"][role["id"]] = rRole.id
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
            IDs["roles"].clear(); IDs["channels"].clear()
            print("🔄 Starting fresh...\n")
    
    template_url = os.getenv("DISCORD_TEMPLATE_URL")
    template = None
    while not template:
        if not template_url: template_url = input("Template URL: ")
        code = template_url.split("/")[-1]
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
            data = revolt_api_json("GET", f"https://api.revolt.chat/servers/{target_server_id}", headers={"x-bot-token": bot_token}, params={"include_channels": "true"})
            if isinstance(data, dict) and "channels" in data:
                for d in data["channels"]:
                    if isinstance(d, dict): current_channels.append(RawChannel(d))
        except: pass
        
        server_channel_ids = {ch.id for ch in current_channels}
        existing_by_key, existing_by_name_queue, existing_by_stripped_queue = build_existing_queues(current_channels)
        
        print("\n1. 🚀 CATEGORIES ONLY\n2. 🔄 SMART MODE (Recommended)\n3. 🗑️  CLEAN SLATE\n4. 🎭 ROLES ONLY")
        mode = input("Choose mode (1-4): ").strip()

        channels = template["channels"]
        textChannels = list(filter(lambda channel: channel["type"] not in [2, 4], channels))
        voiceChannels = list(filter(lambda channel: channel["type"] == 2, channels))
        categories = list(filter(lambda channel: channel["type"] == 4, channels))

        if mode == "4":
            step(1, 1, "Processing Roles")
            await process_roles_logic(server, template, target_server_id, bot_token)
            print("\n✅ Role Sync Complete!"); return

        if mode == "3":
            step(1, text="Deleting channels")
            for ch in current_channels:
                try: requests.delete(f"https://api.revolt.chat/channels/{ch.id}", headers={"x-bot-token": bot_token})
                except: pass
            current_channels = []; server_channel_ids = set()
            existing_by_key = defaultdict(deque); existing_by_name_queue = defaultdict(deque); existing_by_stripped_queue = defaultdict(deque)
            IDs["channels"].clear(); IDs["roles"].clear()
            save_progress(force=True); mode = "2"

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
                    if i%10==0: step(i+1, total, f"{cname} ✓ reused")
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
                    category_list.append({"id": generate_ulid(), "title": cat["name"][:32], "channels": ch_ids})

            if category_list:
                log(f"  📦 Sending categories...")
                try: requests.patch(f"https://api.revolt.chat/servers/{target_server_id}", headers={"x-bot-token": bot_token}, json={"categories": category_list})
                except: pass

            step(4, text="Processing roles")
            await process_roles_logic(server, template, target_server_id, bot_token)

            # --- STEP 5: PERMISSIONS (WITH INHERITANCE) ---
            step(5, text="Permissions")
            
            # Map Discord Category ID -> Permission Overwrites (For Inheritance)
            discord_cat_perms = {cat["id"]: cat.get("permission_overwrites", []) for cat in categories}
            
            channels_to_process = [ch for ch in textChannels + voiceChannels if ch["id"] in IDs["channels"]]
            
            for i, ch in enumerate(channels_to_process):
                if i % 10 == 0: log(f"    Setting perms for batch {i}...", end="\r")
                
                rID = IDs["channels"][ch["id"]]
                
                # Check for explicit overrides, otherwise inherit
                overwrites = ch.get("permission_overwrites", [])
                if not overwrites and ch.get("parent_id") in discord_cat_perms:
                    overwrites = discord_cat_perms[ch["parent_id"]] # Inherit!

                if not overwrites: continue

                template_everyone_id = None
                for r in template["roles"]:
                    if r["name"] == "@everyone": template_everyone_id = r["id"]; break
                if not template_everyone_id: template_everyone_id = template.get("id")

                for ow in overwrites:
                    p = {"allow": convert_permission(ow.get("allow",0)).value, "deny": convert_permission(ow.get("deny",0)).value}
                    role_id_to_set = None
                    
                    if ow["id"] == template_everyone_id: role_id_to_set = template_everyone_id # Special flag or handle
                    elif ow["id"] in IDs["roles"]: role_id_to_set = IDs["roles"][ow["id"]]
                    
                    if role_id_to_set:
                        # Direct API Put to avoid Library complexity
                        # URL: /channels/{channel}/permissions/{role} or /permissions/default
                        try:
                            if role_id_to_set == template_everyone_id:
                                url = f"https://api.revolt.chat/channels/{rID}/permissions/default"
                                load = {"permissions": p["allow"] | p["deny"]} # Simplification? No, Revolt sends allow/deny objects
                                # Actually Revolt uses set_default_permission which takes an object {permissions: X} 
                                # But we want overrides.
                                # Let's use the library for the actual complex bit logic if possible, or construct raw.
                                # Raw payload for role override: { "permissions": { "allow": ..., "deny": ... } }
                                url = f"https://api.revolt.chat/channels/{rID}/permissions/default"
                                revolt_api_json("PUT", url, headers={"x-bot-token": bot_token}, payload={"permissions": p})
                            else:
                                url = f"https://api.revolt.chat/channels/{rID}/permissions/{role_id_to_set}"
                                revolt_api_json("PUT", url, headers={"x-bot-token": bot_token}, payload={"permissions": p})
                        except: pass
                
                time.sleep(0.05) 

            print("\n✅ Import complete!")
            if skipped > 0: print(f"\n⚠️  {skipped} channels skipped (200 limit).")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n🛑 Exiting."); sys.exit(0)