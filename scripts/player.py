#!/usr/bin/env python3

import subprocess
import json
import os
import re
import time
import asyncio
import sys
from datetime import datetime
import hashlib

import shutil
from urllib.parse import urlparse, unquote

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

import urllib.request

# --- Configuration Constants ---
CACHE_BASE_DIR = os.path.join(
    os.getenv('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
    'waybar',
    'player' # Changed from 'lyrics' to 'player' for this script
)
LOGS_DIR = os.path.join(CACHE_BASE_DIR, "logs")

ALBUM_COVERS_DIR_NAME = "album_covers"
ALBUM_COVERS_DIR_PATH = os.path.join(CACHE_BASE_DIR, ALBUM_COVERS_DIR_NAME)
CURRENT_ART_SYMLINK_NAME = "current_song_art"
CURRENT_ART_SYMLINK_PATH = os.path.join(ALBUM_COVERS_DIR_PATH, CURRENT_ART_SYMLINK_NAME)

LAST_TRACK_ID_CACHE_FILENAME = "last_track_id.json"
LAST_TRACK_ID_CACHE_PATH = os.path.join(CACHE_BASE_DIR, LAST_TRACK_ID_CACHE_FILENAME)

ART_UPDATE_COOLDOWN_SECONDS = 3

ICON_PLAY = "\uf04c   "
ICON_PAUSE = "\uf04b   "
ICON_STOP = "\uf04d   "
ICON_MUSIC = "\uf001   "
ICON_ART = "" # Example: "\uf03e   " for a picture icon

PLAYERCTL_CMD_BASE = ["playerctl", "--player=playerctld"]
PLAYERCTL_RESTART_DELAY_SECONDS = 5
PLAYERCTL_RESTART_FAILURE_DELAY_SECONDS = 10
# New timeouts for readline and status check
PLAYERCTL_READLINE_TIMEOUT_SECONDS = 5.0
PLAYERCTL_STATUS_CHECK_TIMEOUT_SECONDS = 2.0

PLAYERCTL_DATA_BEGIN_MARKER = "PLAYER_CTL_SCRIPT_BEGIN_METADATA_BLOCK_V1_UNIQUE" # Ensure this is unique if both scripts run
PLAYERCTL_DATA_END_MARKER = "PLAYER_CTL_SCRIPT_END_METADATA_BLOCK_V1_UNIQUE"

PLAYERCTL_CUSTOM_FORMAT_STRING = (
    f"{PLAYERCTL_DATA_BEGIN_MARKER}\n"
    "artist:{{artist}}\n"
    "title:{{title}}\n"
    "album:{{album}}\n"
    "status:{{status}}\n"
    "player:{{playerName}}\n"
    "position:{{position}}\n" # Position in microseconds
    "length:{{mpris:length}}\n" # Length in microseconds
    "volume:{{volume}}\n"
    "artUrl:{{mpris:artUrl}}\n"
    "trackid:{{mpris:trackid}}\n"
    f"{PLAYERCTL_DATA_END_MARKER}\n"
)

# --- Global State Variables ---
last_known_art_url = None
last_art_operation_timestamp = 0
last_processed_mpris_track_id_global = None
current_player_status_cache = "stopped" # Cache current player status to avoid redundant updates

# --- Helper Functions ---

def get_current_log_file_path():
    today_date = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOGS_DIR, f"{today_date}.log")

def debug_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file_path = get_current_log_file_path()
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] DEBUG: {message}\n")
    except IOError as e:
        print(f"ERROR: Could not write to debug log file {log_file_path}: {e}", file=sys.stderr, flush=True)

def output_waybar_json(text, tooltip="", css_class=""):
    output_data = {"text": text, "tooltip": tooltip, "class": css_class}
    print(json.dumps(output_data, ensure_ascii=False), flush=True)

def _sanitize_filename(name_str, replacement_char='_'):
    # (Identical to the version in your provided player.py)
    if not name_str: return ""
    sanitized = re.sub(r'[\x00-\x1f<>:"/\\|?*]', replacement_char, name_str)
    sanitized = re.sub(f'{re.escape(replacement_char)}+', replacement_char, sanitized)
    sanitized = sanitized.strip(f' .{replacement_char}')
    byte_limit = 200
    original_len = len(sanitized)
    temp_sanitized = sanitized
    while len(temp_sanitized.encode('utf-8', 'ignore')) > byte_limit:
        temp_sanitized = temp_sanitized[:-1]
    sanitized = temp_sanitized
    if not sanitized and original_len > 0 :
        return hashlib.sha1(name_str.encode('utf-8')).hexdigest()[:16]
    return sanitized if sanitized else "_empty_metadata_"

async def process_album_art(title, artist, art_url_param, current_mpris_track_id):
    # (Identical to the version in your provided player.py)
    # This function handles downloading/caching art and updating symlink
    global last_known_art_url, last_art_operation_timestamp

    task_id_short = hashlib.sha1(str(time.time()).encode()).hexdigest()[:6]
    # debug_log(f"[ArtTask-{task_id_short}] Starting for track '{current_mpris_track_id}', title '{title}', artist '{artist}', URL '{art_url_param}'")

    os.makedirs(ALBUM_COVERS_DIR_PATH, exist_ok=True)

    base_filename_unsafe = ""
    if title and artist: base_filename_unsafe = f"{title} - {artist}"
    elif title: base_filename_unsafe = title
    elif artist: base_filename_unsafe = artist

    sanitized_filename_base = _sanitize_filename(base_filename_unsafe)

    if not sanitized_filename_base or sanitized_filename_base == "_empty_metadata_":
        log_track_ref = current_mpris_track_id or "unknown track"
        debug_log(f"[ArtTask-{task_id_short}] Title/Artist missing for track '{log_track_ref}'. Clearing symlink.")
        if os.path.lexists(CURRENT_ART_SYMLINK_PATH):
            try: os.remove(CURRENT_ART_SYMLINK_PATH); # debug_log(f"[ArtTask-{task_id_short}] Removed symlink.")
            except OSError as e: debug_log(f"[ArtTask-{task_id_short}] Error removing symlink: {e}")
        return

    specific_track_art_path = os.path.join(ALBUM_COVERS_DIR_PATH, sanitized_filename_base)
    current_time = time.time()

    is_current_track_context = (current_mpris_track_id == last_processed_mpris_track_id_global) # Compare with global
    symlink_is_correct_and_valid = False
    if os.path.lexists(CURRENT_ART_SYMLINK_PATH) and os.path.exists(CURRENT_ART_SYMLINK_PATH):
        try:
            if os.path.exists(specific_track_art_path): # Target for samefile must exist
                symlink_is_correct_and_valid = os.path.samefile(os.path.realpath(CURRENT_ART_SYMLINK_PATH), os.path.realpath(specific_track_art_path))
        except FileNotFoundError: pass

    if os.path.exists(specific_track_art_path) and \
            art_url_param == last_known_art_url and \
            is_current_track_context and \
            symlink_is_correct_and_valid:
        return

    if (current_time - last_art_operation_timestamp) < ART_UPDATE_COOLDOWN_SECONDS:
        debug_log(f"[ArtTask-{task_id_short}] Cooldown for '{sanitized_filename_base}'. Skipping fetch for '{art_url_param}'.")
        if not os.path.exists(specific_track_art_path): return

    if not art_url_param:
        debug_log(f"[ArtTask-{task_id_short}] No art_url for '{sanitized_filename_base}'. Clearing symlink.")
        if os.path.lexists(CURRENT_ART_SYMLINK_PATH):
            try: os.remove(CURRENT_ART_SYMLINK_PATH)
            except OSError as e: debug_log(f"[ArtTask-{task_id_short}] Error removing symlink: {e}")
        return

    needs_fetch = True
    if not os.path.exists(specific_track_art_path):
        debug_log(f"[ArtTask-{task_id_short}] Art for '{sanitized_filename_base}' not in cache. Fetching.")
    else:
        if current_mpris_track_id != last_processed_mpris_track_id_global:
            debug_log(f"[ArtTask-{task_id_short}] New song context. Art for '{sanitized_filename_base}' found in cache. Using existing: {specific_track_art_path}")
            needs_fetch = False
        elif art_url_param != last_known_art_url:
            debug_log(f"[ArtTask-{task_id_short}] Art URL for current song '{sanitized_filename_base}' changed. Re-fetching from '{art_url_param}'.")
        else:
            debug_log(f"[ArtTask-{task_id_short}] Art for '{sanitized_filename_base}' (current) cached and art_url unchanged. Using cache.")
            needs_fetch = False

    fetch_success = False
    if needs_fetch:
        debug_log(f"[ArtTask-{task_id_short}] Fetching art for '{sanitized_filename_base}' from URL: {art_url_param}")
        try:
            if art_url_param.startswith("http"):
                if REQUESTS_AVAILABLE:
                    response = await asyncio.to_thread(lambda: requests.get(art_url_param, stream=True, timeout=10))
                    response.raise_for_status()
                    with open(specific_track_art_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192): f.write(chunk)
                    fetch_success = True
                else:
                    request_obj = urllib.request.Request(art_url_param, headers={'User-Agent': 'WaybarMediaPlayerScript/1.0'})
                    with await asyncio.to_thread(lambda: urllib.request.urlopen(request_obj, timeout=10)) as response, \
                            open(specific_track_art_path, 'wb') as out_file:
                        await asyncio.to_thread(lambda: shutil.copyfileobj(response, out_file))
                    fetch_success = True
            elif art_url_param.startswith("file://"):
                parsed_url = urlparse(art_url_param)
                source_image_path = unquote(parsed_url.path)
                if os.path.exists(source_image_path):
                    await asyncio.to_thread(lambda: shutil.copyfile(source_image_path, specific_track_art_path))
                    fetch_success = True
                else: debug_log(f"[ArtTask-{task_id_short}] Local art file not found: {source_image_path}")

            if fetch_success: debug_log(f"[ArtTask-{task_id_short}] Saved/updated art to: {specific_track_art_path}")
            else:
                debug_log(f"[ArtTask-{task_id_short}] Failed to fetch art for '{sanitized_filename_base}'.")
                if os.path.exists(specific_track_art_path):
                    try: os.remove(specific_track_art_path)
                    except OSError as e_rm: debug_log(f"[ArtTask-{task_id_short}] Error removing bad art file {specific_track_art_path}: {e_rm}")
        except Exception as e:
            debug_log(f"[ArtTask-{task_id_short}] Exception during art fetch for '{sanitized_filename_base}': {e}")
            fetch_success = False
            if os.path.exists(specific_track_art_path):
                try: os.remove(specific_track_art_path)
                except OSError as e_rm: debug_log(f"[ArtTask-{task_id_short}] Error removing art file {specific_track_art_path} after exception: {e_rm}")
    else:
        fetch_success = True

    if os.path.lexists(CURRENT_ART_SYMLINK_PATH):
        try: os.remove(CURRENT_ART_SYMLINK_PATH)
        except OSError as e: debug_log(f"[ArtTask-{task_id_short}] Error removing old symlink: {e}")

    if fetch_success and os.path.exists(specific_track_art_path) and os.path.getsize(specific_track_art_path) > 0:
        try:
            target_basename = os.path.basename(specific_track_art_path)
            os.symlink(target_basename, CURRENT_ART_SYMLINK_PATH)
            debug_log(f"[ArtTask-{task_id_short}] Updated symlink: {CURRENT_ART_SYMLINK_PATH} -> {target_basename}")
        except OSError as e:
            debug_log(f"[ArtTask-{task_id_short}] Error creating symlink to '{target_basename}': {e}")
    else:
        debug_log(f"[ArtTask-{task_id_short}] No valid art for '{sanitized_filename_base}'. Symlink not created.")

    last_art_operation_timestamp = current_time
    # Note: last_known_art_url is updated by the calling function _parse_and_display_metadata
    # when it decides to launch this task. This function has processed what it was given.
    debug_log(f"[ArtTask-{task_id_short}] Finished.")


async def _parse_and_display_metadata(
        metadata_dict,
        current_script_last_processed_mpris_id
):
    global last_known_art_url, current_player_status_cache

    # --- 1. Parse all metadata fields (This part remains unchanged) ---
    artist = metadata_dict.get("artist", "").strip()
    title = metadata_dict.get("title", "").strip()
    album = metadata_dict.get("album", "").strip()
    status = metadata_dict.get("status", "stopped").lower()
    player_name = metadata_dict.get("player", "").strip()
    mpris_track_id = metadata_dict.get("trackid", "").strip()

    art_url_str = metadata_dict.get("artUrl", "")
    art_url = art_url_str if art_url_str else None

    try: position_us = int(metadata_dict.get("position", "0"))
    except ValueError: position_us = 0
    try: length_us = int(metadata_dict.get("length", "0"))
    except ValueError: length_us = 0

    volume_raw_str = metadata_dict.get("volume", "0.0")

    current_player_status_cache = status

    # --- 2. Format text for Waybar main display and tooltip details ---

    # Details for Tooltip (position, length, volume)
    position_str = time.strftime('%M:%S', time.gmtime(position_us // 1000000)) if position_us > 0 else "00:00"
    length_str = time.strftime('%M:%S', time.gmtime(length_us // 1000000)) if length_us > 0 else "00:00"
    volume_percent_str = "N/A"
    try:
        volume_float = float(volume_raw_str)
        volume_percent_str = f"{volume_float * 100:.0f}%"
    except ValueError: pass

    # Main display text construction
    display_text_main_content = "" # This will be "Song - Artist" or similar
    if title and artist:
        display_text_main_content = f"{title} - {artist}"
    elif title:
        display_text_main_content = title
    elif player_name and status != "stopped":
        display_text_main_content = f"{ICON_MUSIC}{player_name}" # Show player if no title/artist but active
    else:
        display_text_main_content = f"{ICON_MUSIC}No Title"

    current_status_icon = ICON_STOP
    current_css_class = "stopped"
    if status == "playing":
        current_status_icon = ICON_PLAY
        current_css_class = "playing"
    elif status == "paused":
        current_status_icon = ICON_PAUSE
        current_css_class = "paused"

    final_text_parts = []
    if status == "stopped" and not artist and not title and not album:
        final_text_parts.append(f"{current_status_icon}No Media")
        current_css_class = "empty"
    else:
        # MODIFICATION: Only icon and "Song - Artist" (or equivalent) for main display
        final_text_parts.append(f"{current_status_icon}{display_text_main_content}")

        # Album art icon can optionally be part of the main display
        if ICON_ART.strip() and os.path.exists(CURRENT_ART_SYMLINK_PATH):
            final_text_parts.append(ICON_ART.strip())

    final_text = " ".join(filter(None, final_text_parts)).strip()
    final_text = ' '.join(final_text.split()) # Normalize spaces

    # Tooltip construction (contains all details, including progress and volume)
    tooltip_parts = [f"Player: {player_name or 'N/A'}", f"Status: {status.capitalize() or 'N/A'}"]
    if title: tooltip_parts.append(f"Song: {title}")
    if artist: tooltip_parts.append(f"Artist: {artist}")
    if album: tooltip_parts.append(f"Album: {album}")
    # MODIFICATION: Progress and Volume are now only in tooltip if track is active
    if status in ["playing", "paused"]:
        if length_us > 0 :
            tooltip_parts.append(f"Progress: {position_str} / {length_str}")
        if volume_percent_str != "N/A":
            tooltip_parts.append(f"Volume: {volume_percent_str}")
    elif status == "stopped" and (artist or title): # If stopped but there was a song, show its full length
        if length_us > 0:
            tooltip_parts.append(f"Length: {length_str}")


    if os.path.exists(CURRENT_ART_SYMLINK_PATH): tooltip_parts.append("Cover Art: Available")
    elif art_url: tooltip_parts.append("Cover Art: URL provided (processing)")
    else: tooltip_parts.append("Cover Art: N/A")
    final_tooltip = "\n".join(tooltip_parts)

    if not player_name and status == "stopped" and not (artist or title or album) :
        current_css_class = "offline"

    output_waybar_json(final_text, final_tooltip, current_css_class)

    # --- 3. Decide if album art processing task needs to be launched (Unchanged) ---
    should_launch_art_task = False
    if mpris_track_id:
        if mpris_track_id != current_script_last_processed_mpris_id:
            should_launch_art_task = True
        elif art_url and art_url != last_known_art_url:
            should_launch_art_task = True
    elif not mpris_track_id and current_script_last_processed_mpris_id:
        should_launch_art_task = True

    if should_launch_art_task:
        debug_log(f"Requesting background art processing. MPRIS ID: '{mpris_track_id}', Title: '{title}', Art URL: '{art_url}'")
        asyncio.create_task(process_album_art(title, artist, art_url, mpris_track_id))
        last_known_art_url = art_url

        # --- 4. Update and persist last processed mpris_track_id (Unchanged) ---
    new_global_mpris_id = mpris_track_id if mpris_track_id else None
    if new_global_mpris_id != current_script_last_processed_mpris_id:
        debug_log(f"MPRIS track ID changed from '{current_script_last_processed_mpris_id}' to '{new_global_mpris_id}'. Updating cache.")
        os.makedirs(os.path.dirname(LAST_TRACK_ID_CACHE_PATH), exist_ok=True)
        try:
            with open(LAST_TRACK_ID_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump({"track_id": new_global_mpris_id}, f)
        except IOError as e:
            debug_log(f"Error writing {LAST_TRACK_ID_CACHE_PATH}: {e}")

    return new_global_mpris_id


async def _launch_playerctl_process(cmd_list):
    # (Identical to previous version)
    try:
        process = await asyncio.create_subprocess_exec(*cmd_list, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        debug_log(f"playerctl process started/restarted. PID: {process.pid}")
        return process
    except FileNotFoundError:
        debug_log(f"{cmd_list[0]} not found.")
        output_waybar_json(f"{ICON_STOP} {cmd_list[0]} error", f"{cmd_list[0]} not found", "error")
    except Exception as e:
        debug_log(f"Failed to start/restart {cmd_list[0]}: {e}")
        output_waybar_json(f"{ICON_STOP} {cmd_list[0]} error", f"Failed to start {cmd_list[0]}: {e}", "error")
    return None

async def main_loop_async():
    global last_processed_mpris_track_id_global, current_player_status_cache

    if os.path.exists(LAST_TRACK_ID_CACHE_PATH):
        try:
            with open(LAST_TRACK_ID_CACHE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_processed_mpris_track_id_global = data.get("track_id")
            debug_log(f"Loaded last_processed_mpris_track_id_global from cache: {last_processed_mpris_track_id_global}")
        except (json.JSONDecodeError, IOError) as e:
            debug_log(f"Error loading {LAST_TRACK_ID_CACHE_PATH}: {e}.")
            last_processed_mpris_track_id_global = None

    playerctl_cmd_full = PLAYERCTL_CMD_BASE + ["metadata", "--format", PLAYERCTL_CUSTOM_FORMAT_STRING, "--follow"]
    playerctl_process = await _launch_playerctl_process(playerctl_cmd_full)

    active_metadata_dict = {}
    is_parsing_metadata_block = False

    while True:
        if playerctl_process is None or playerctl_process.returncode is not None:
            if playerctl_process is not None:
                debug_log(f"Playerctl process dead (code: {playerctl_process.returncode}).")
                if playerctl_process.stderr and not playerctl_process.stderr.at_eof():
                    stderr_bytes = await playerctl_process.stderr.read()
                    debug_log(f"Final stderr from dead playerctl: {stderr_bytes.decode(errors='ignore')}")

            if current_player_status_cache != "offline": # Avoid spamming if already offline
                output_waybar_json(f"{ICON_STOP} Player Disconnected", "Playerctl process stopped", "offline")
                current_player_status_cache = "offline"
                # Also clear art and last track ID as player process is gone
                asyncio.create_task(process_album_art(None, None, None, last_processed_mpris_track_id_global))
                if os.path.exists(LAST_TRACK_ID_CACHE_PATH):
                    try:
                        with open(LAST_TRACK_ID_CACHE_PATH, 'w', encoding='utf-8') as f: json.dump({"track_id": None}, f)
                    except IOError: pass # Best effort
                last_processed_mpris_track_id_global = None


            await asyncio.sleep(PLAYERCTL_RESTART_DELAY_SECONDS)
            playerctl_process = await _launch_playerctl_process(playerctl_cmd_full)
            if playerctl_process is None:
                await asyncio.sleep(PLAYERCTL_RESTART_FAILURE_DELAY_SECONDS)
            is_parsing_metadata_block = False; active_metadata_dict = {}
            continue

        line_bytes = b''
        try:
            line_bytes = await asyncio.wait_for(
                playerctl_process.stdout.readline(),
                timeout=PLAYERCTL_READLINE_TIMEOUT_SECONDS
            )

            if not line_bytes:
                exit_code = await playerctl_process.wait()
                stderr_output_bytes = await playerctl_process.stderr.read() if playerctl_process.stderr else b''
                debug_log(f"playerctl stdout EOF. Code: {exit_code}. Stderr: '{stderr_output_bytes.decode(errors='ignore').strip()}'")
                playerctl_process = None; is_parsing_metadata_block = False; active_metadata_dict = {}
                continue

            line = line_bytes.decode('utf-8').strip()
            if not line: continue

            if line == PLAYERCTL_DATA_BEGIN_MARKER:
                active_metadata_dict = {}; is_parsing_metadata_block = True
                continue

            elif line == PLAYERCTL_DATA_END_MARKER:
                if is_parsing_metadata_block:
                    is_parsing_metadata_block = False
                    new_mpris_id = await _parse_and_display_metadata(active_metadata_dict, last_processed_mpris_track_id_global)
                    last_processed_mpris_track_id_global = new_mpris_id
                    active_metadata_dict = {}
                else: debug_log(f"Warning: END_MARKER ('{line}') without active block.")
                continue

            elif is_parsing_metadata_block:
                parts = line.split(":", 1)
                if len(parts) == 2: active_metadata_dict[parts[0].strip()] = parts[1].strip()
                else: debug_log(f"Warning: Malformed line in block: '{line}'")
                continue

            else:
                debug_log(f"Standalone line from playerctl: '{line}'")
                lower_line = line.lower()
                if "no player is running" in lower_line or "no players found" in lower_line:
                    if current_player_status_cache != "offline":
                        output_waybar_json(f"{ICON_STOP} No Player", "No player is running", "offline")
                        current_player_status_cache = "offline"
                        asyncio.create_task(process_album_art(None, None, None, last_processed_mpris_track_id_global))
                        if os.path.exists(LAST_TRACK_ID_CACHE_PATH):
                            try:
                                with open(LAST_TRACK_ID_CACHE_PATH, 'w', encoding='utf-8') as f: json.dump({"track_id": None}, f)
                            except IOError: pass
                        last_processed_mpris_track_id_global = None
                    active_metadata_dict = {}; is_parsing_metadata_block = False
                # Other standalone lines like "Playing", "Paused" are less critical if metadata blocks are comprehensive
                # If needed, specific handling can be added here.

        except asyncio.TimeoutError:
            debug_log(f"Timeout ({PLAYERCTL_READLINE_TIMEOUT_SECONDS}s) waiting for playerctl update. Checking status manually.")

            try:
                status_check_cmd = PLAYERCTL_CMD_BASE + ["status"]
                proc = await asyncio.create_subprocess_exec(
                    *status_check_cmd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=PLAYERCTL_STATUS_CHECK_TIMEOUT_SECONDS)

                actual_player_status_stdout = stdout_bytes.decode('utf-8').strip().lower()
                actual_player_status_stderr = stderr_bytes.decode('utf-8').strip().lower()

                player_is_effectively_stopped = False
                if proc.returncode != 0:
                    debug_log(f"Manual status check: playerctl command failed (Code: {proc.returncode}, Stderr: '{actual_player_status_stderr}'). Assuming no player.")
                    player_is_effectively_stopped = True
                elif "no player is running" in actual_player_status_stdout or \
                        "no players found" in actual_player_status_stdout or \
                        actual_player_status_stdout == "stopped": # "stopped" from stdout of playerctl status
                    debug_log(f"Manual status check: stdout indicates player stopped/gone ('{actual_player_status_stdout}').")
                    player_is_effectively_stopped = True

                if player_is_effectively_stopped:
                    if current_player_status_cache != "offline": # Only update if state truly changed
                        debug_log("Player confirmed stopped/gone by manual check after timeout. Updating Waybar and cache.")
                        output_waybar_json(f"{ICON_STOP} No Media", "Player stopped or not responding", "offline")
                        current_player_status_cache = "offline" # Update our cached status
                        asyncio.create_task(process_album_art(None, None, None, last_processed_mpris_track_id_global))
                        if os.path.exists(LAST_TRACK_ID_CACHE_PATH):
                            try:
                                with open(LAST_TRACK_ID_CACHE_PATH, 'w', encoding='utf-8') as f: json.dump({"track_id": None}, f)
                            except IOError: pass
                        last_processed_mpris_track_id_global = None
                else:
                    debug_log(f"Readline timeout, but manual status check: '{actual_player_status_stdout}'. Player still active ({current_player_status_cache}). No change from timeout.")

            except asyncio.TimeoutError:
                debug_log("Manual 'playerctl status' check command timed out. Assuming player unresponsive.")
                if current_player_status_cache != "error": # Avoid spamming if already error
                    output_waybar_json(f"{ICON_STOP} No Media", "Player unresponsive (status check timeout)", "error")
                    current_player_status_cache = "error"
                    asyncio.create_task(process_album_art(None, None, None, last_processed_mpris_track_id_global))
                    if os.path.exists(LAST_TRACK_ID_CACHE_PATH):
                        try:
                            with open(LAST_TRACK_ID_CACHE_PATH, 'w', encoding='utf-8') as f: json.dump({"track_id": None}, f)
                        except IOError: pass
                    last_processed_mpris_track_id_global = None
            except Exception as e_status_check:
                debug_log(f"Error during manual status check: {type(e_status_check).__name__} - {e_status_check}")
                if current_player_status_cache != "error":
                    output_waybar_json(f"{ICON_STOP} Status Error", "Error checking player status", "error")
                    current_player_status_cache = "error"

            continue # Go back to the start of the while loop

        except BrokenPipeError: # Should be rare with EOF handling
            debug_log("BrokenPipeError. Will attempt restart.")
            if current_player_status_cache != "error":
                output_waybar_json(f"{ICON_STOP} Player Error", "Connection lost", "error")
                current_player_status_cache = "error"
            if playerctl_process and playerctl_process.returncode is None: await playerctl_process.wait()
            playerctl_process = None; is_parsing_metadata_block = False; active_metadata_dict = {}
        except Exception as e:
            debug_log(f"Unhandled exception in main loop: {type(e).__name__}: {e}")
            with open(get_current_log_file_path(), "a", encoding="utf-8") as log_f:
                import traceback; traceback.print_exc(file=log_f)
            if current_player_status_cache != "error":
                output_waybar_json(f"{ICON_STOP} Script Error", str(e), "error")
                current_player_status_cache = "error"
            if playerctl_process and playerctl_process.returncode is None:
                try: playerctl_process.kill(); await playerctl_process.wait()
                except ProcessLookupError: pass
            playerctl_process = None
            await asyncio.sleep(PLAYERCTL_RESTART_DELAY_SECONDS)


# --- Script Entry Point ---
if __name__ == "__main__":
    try:
        os.makedirs(CACHE_BASE_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)

        debug_log_path_at_start = get_current_log_file_path()
        with open(debug_log_path_at_start, "w", encoding="utf-8") as f:
            f.write(f"--- Starting player.py debug session at {datetime.now()} ---\n") # Script name in log
        asyncio.run(main_loop_async())
    except KeyboardInterrupt:
        debug_log("Script interrupted by user."); print("\nScript terminated by user.", file=sys.stderr, flush=True)
    except Exception as e:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fatal_msg = f"[{ts}] FATAL SCRIPT ERROR: {type(e).__name__}: {e}"
        print(fatal_msg, file=sys.stderr, flush=True)
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            log_file = get_current_log_file_path()
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(fatal_msg + "\n")
                import traceback; traceback.print_exc(file=f)
        except Exception as log_ex:
            print(f"[{ts}] CRITICAL: Could not write fatal error to log file: {log_ex}", file=sys.stderr, flush=True)
        try: output_waybar_json(f"{ICON_STOP} Fatal Script Error", str(e), "error")
        except: pass
    finally:
        debug_log("player.py script terminated.")