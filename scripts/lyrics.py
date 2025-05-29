#!/usr/bin/env python3

import subprocess
import json # Still used for song_info_cache.json and Waybar output
import os
import re
import time
import asyncio
import sys
from datetime import datetime
import hashlib # Not strictly needed in this script after plain text change, but good import

# Assuming qqmusic_api is in the same directory or installed
from qqmusic_api import lyric, search

# --- Configuration Constants ---
CACHE_BASE_DIR = os.path.join(
    os.getenv('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
    'waybar',
    'lyrics'
)
LYRICS_FILES_DIR = os.path.join(CACHE_BASE_DIR, "lyrics")
LOGS_DIR = os.path.join(CACHE_BASE_DIR, "logs")
SONG_INFO_CACHE_FILE = os.path.join(CACHE_BASE_DIR, "song_info_cache.json")

# Playerctl command and constants
PLAYERCTL_CMD_BASE = ["playerctl", "--player=playerctld"] # Added for clarity in status check
PLAYERCTL_RESTART_DELAY_SECONDS = 5
PLAYERCTL_RESTART_FAILURE_DELAY_SECONDS = 10
PLAYERCTL_READLINE_TIMEOUT_SECONDS = 5.0 # Timeout for readline()
PLAYERCTL_STATUS_CHECK_TIMEOUT_SECONDS = 2.0 # Timeout for manual 'playerctl status'

# --- Playerctl Custom Text Format ---
PLAYERCTL_DATA_BEGIN_MARKER = "PLAYERCTL_LYRICS_SCRIPT_BEGIN_METADATA_V2" # Unique marker
PLAYERCTL_DATA_END_MARKER = "PLAYERCTL_LYRICS_SCRIPT_END_METADATA_V2"

PLAYERCTL_CUSTOM_TEXT_FORMAT_STRING = (
    f"{PLAYERCTL_DATA_BEGIN_MARKER}\n"
    "artist:{{artist}}\n"
    "title:{{title}}\n"
    "status:{{status}}\n"
    "position_us:{{position}}\n"    # Position in microseconds
    "length_us:{{mpris:length}}\n"  # Length in microseconds
    "player:{{playerName}}\n"
    "track_id:{{mpris:trackid}}\n"  # Reliable track ID
    f"{PLAYERCTL_DATA_END_MARKER}\n"
)

# --- Global State Variables ---
cached_song_info = {
    "title": None,
    "artist": None,
    "qq_song_mid": None,
    "lyrics_file_path": None,
    "lyrics_parsed_content": [],
    "last_fetched_timestamp": 0,
    "playerctl_track_id": None, # Stores the MPRIS track ID from playerctl
    "status": "stopped" # Add status to cached_song_info
}
last_processed_playerctl_track_id_from_event = None


# --- Helper Functions (mostly unchanged) ---

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

def sanitize_filename(name):
    name = name.replace('/', '_') # Replace slash with underscore for safety
    name = re.sub(r'[\\:*?"<>|]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    # Ensure filename is not excessively long (e.g. max 200 bytes for component)
    byte_limit = 200
    original_len = len(name)
    temp_name = name
    while len(temp_name.encode('utf-8', 'ignore')) > byte_limit:
        temp_name = temp_name[:-1]
    name = temp_name.strip() # Re-strip after potential truncation
    if not name and original_len > 0: # Fallback if sanitization results in empty but original was not
        return hashlib.sha1(name.encode('utf-8')).hexdigest()[:10] # Short hash
    return name if name else "_empty_title_"


def parse_lrc(lrc_content):
    # (Unchanged from your provided version)
    lyrics = []
    global_offset_ms = 0
    if not lrc_content: return lyrics

    lines = lrc_content.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue

        offset_match = re.match(r'\[offset:([+-]?\d+)\]', line)
        if offset_match:
            try: global_offset_ms = int(offset_match.group(1))
            except ValueError: debug_log(f"Invalid offset: {line}")
            continue

        timestamp_tags = re.findall(r'\[(\d{2}):(\d{2})\.?(\d{0,3})?\]', line)
        lyric_text_match = re.search(r'\]([^\[]*)$', line)
        lyric_text = lyric_text_match.group(1).strip() if lyric_text_match else ""

        if lyric_text:
            for tag in timestamp_tags:
                minutes, seconds, milliseconds_str = tag
                minutes = int(minutes)
                seconds = int(seconds)
                milliseconds = int(milliseconds_str.ljust(3, '0')[:3]) if milliseconds_str else 0
                timestamp_ms = (minutes * 60 + seconds) * 1000 + milliseconds - global_offset_ms
                if timestamp_ms < 0: timestamp_ms = 0
                lyrics.append((timestamp_ms, lyric_text))
        elif not timestamp_tags and line and not re.match(r'\[[a-zA-Z]+:.*\]', line):
            if not lyrics or lyrics[-1][1] != line :
                lyrics.append((lyrics[-1][0] if lyrics else 0, line))

    lyrics.sort(key=lambda x: x[0])
    return lyrics

def get_lyrics_file_path(title, artist):
    # (Unchanged from your provided version)
    safe_title = sanitize_filename(title)
    safe_artist = sanitize_filename(artist)
    filename = f"{safe_artist} - {safe_title}.lrc" if safe_artist else f"{safe_title}.lrc"
    if not safe_title and not safe_artist: filename = "unknown_song.lrc"
    return os.path.join(LYRICS_FILES_DIR, filename)

def load_lyrics_from_local_file(title, artist):
    # (Unchanged from your provided version)
    file_path = get_lyrics_file_path(title, artist)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lrc_content = f.read()
            debug_log(f"Loaded lyrics from local: {file_path}")
            return parse_lrc(lrc_content), file_path
        except IOError as e:
            debug_log(f"Error reading {file_path}: {e}")
    return [], None

def save_lyrics_to_local_file(title, artist, lrc_content):
    # (Unchanged from your provided version)
    if not lrc_content or not title: return
    file_path = get_lyrics_file_path(title, artist)
    try:
        os.makedirs(LYRICS_FILES_DIR, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f: f.write(lrc_content)
        debug_log(f"Saved lyrics to: {file_path}")
    except IOError as e:
        debug_log(f"Error saving to {file_path}: {e}")

async def fetch_lyrics_from_qqmusic_api(title, artist):
    # (Unchanged from your provided version)
    debug_log(f"Attempting to fetch lyrics for title='{title}', artist='{artist}' from QQMusic API.")
    try:
        search_query = f"{title} {artist}" if artist else title
        song_list_raw = await search.search_by_type(search_query, search.SearchType.SONG)

        if not isinstance(song_list_raw, list) or not song_list_raw:
            debug_log(f"QQMusic search no results or invalid format for '{search_query}'. Type: {type(song_list_raw)}")
            return [], None, None

        song_list = song_list_raw
        song_mid = None
        for song_data in song_list:
            s_name = song_data.get("name", "").strip().lower()
            s_artist_list = song_data.get("singer", [])
            s_artists = [s.get("name","").strip().lower() for s in s_artist_list if s.get("name")]
            title_lower = title.lower().strip()
            artist_lower = artist.lower().strip()
            if s_name == title_lower and (not artist_lower or artist_lower in s_artists or any(artist_lower in s_a for s_a in s_artists)):
                song_mid = song_data.get("mid")
                debug_log(f"QQMusic: Found good match: '{s_name}' by '{s_artists}', MID: {song_mid}")
                break

        if not song_mid and song_list:
            first_song = song_list[0]
            song_mid = first_song.get("mid")
            debug_log(f"QQMusic: No exact match, using first result MID: {song_mid} ('{first_song.get('name')}')")

        if song_mid:
            lyric_data = await lyric.get_lyric(song_mid)
            lrc_content = ""
            if isinstance(lyric_data, dict):
                lrc_content = lyric_data.get("lrc", {}).get("lyric", "")
                if not lrc_content: lrc_content = lyric_data.get("lyric", "")

            if lrc_content:
                debug_log(f"QQMusic: LRC content retrieved for MID {song_mid}.")
                return parse_lrc(lrc_content), song_mid, lrc_content
            else:
                debug_log(f"QQMusic: LRC content is empty for MID {song_mid}. Raw: {lyric_data if isinstance(lyric_data, dict) else type(lyric_data)}")
        else:
            debug_log("QQMusic: No suitable song MID found.")
    except Exception as e:
        debug_log(f"Error during QQMusic API call for '{title}' by '{artist}': {type(e).__name__} - {e}")
    return [], None, None

def load_song_info_cache():
    # (Unchanged from your provided version, but ensure "status" key is handled)
    if os.path.exists(SONG_INFO_CACHE_FILE):
        try:
            with open(SONG_INFO_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            debug_log("Song info cache loaded.")
            default_keys = {"title": None, "artist": None, "qq_song_mid": None,
                            "lyrics_file_path": None, "lyrics_parsed_content": [],
                            "last_fetched_timestamp": 0, "playerctl_track_id": None,
                            "status": "stopped"} # Added status default
            for key, default_value in default_keys.items():
                if key not in cache_data:
                    cache_data[key] = default_value
            return cache_data
        except (json.JSONDecodeError, FileNotFoundError, IOError) as e:
            debug_log(f"Error loading song info cache '{SONG_INFO_CACHE_FILE}': {e}. Resetting.")
            if os.path.exists(SONG_INFO_CACHE_FILE):
                try: os.remove(SONG_INFO_CACHE_FILE)
                except OSError: pass
    return {"title": None, "artist": None, "qq_song_mid": None, "lyrics_file_path": None,
            "lyrics_parsed_content": [], "last_fetched_timestamp": 0,
            "playerctl_track_id": None, "status": "stopped"}

def save_song_info_cache(cache_data):
    # (Unchanged from your provided version)
    try:
        os.makedirs(CACHE_BASE_DIR, exist_ok=True)
        with open(SONG_INFO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        debug_log("Song info cache saved.")
    except IOError as e:
        debug_log(f"Failed to write song info cache '{SONG_INFO_CACHE_FILE}': {e}")

# --- Main loop function (MODIFIED FOR TIMEOUT AND STATUS CHECK) ---
async def listen_to_playerctl_and_update_waybar():
    global cached_song_info, last_processed_playerctl_track_id_from_event

    os.makedirs(LYRICS_FILES_DIR, exist_ok=True)

    playerctl_listen_cmd = PLAYERCTL_CMD_BASE[:2] + ["metadata", "--format", PLAYERCTL_CUSTOM_TEXT_FORMAT_STRING, "--follow"]
    debug_log(f"Starting playerctl with custom text format: {' '.join(playerctl_listen_cmd)}")

    playerctl_process = None
    try:
        playerctl_process = await asyncio.create_subprocess_exec(
            *playerctl_listen_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        debug_log("playerctl not found.")
        output_waybar_json("Playerctl Not Found", "Please install playerctl", "error")
        return
    debug_log(f"playerctl process started. PID: {playerctl_process.pid if playerctl_process else 'N/A'}")

    cached_song_info = load_song_info_cache()
    last_processed_playerctl_track_id_from_event = cached_song_info.get("playerctl_track_id")

    active_metadata_dict = {}
    is_parsing_metadata_block = False

    while True:
        if playerctl_process is None or playerctl_process.returncode is not None:
            if playerctl_process: debug_log(f"Playerctl dead (code: {playerctl_process.returncode}). Restarting.")
            else: debug_log("Playerctl not running. Starting.")
            # Output disconnected state immediately
            output_waybar_json("Player Disconnected", "Attempting to reconnect playerctl", "offline")
            cached_song_info["status"] = "stopped" # Update cache to reflect disconnected state
            # No need to save cache here as it's a temporary process state

            await asyncio.sleep(PLAYERCTL_RESTART_DELAY_SECONDS)
            try:
                playerctl_process = await asyncio.create_subprocess_exec(
                    *playerctl_listen_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                debug_log(f"Playerctl restarted. PID: {playerctl_process.pid if playerctl_process else 'N/A'}")
            except Exception as e_restart:
                debug_log(f"Failed to restart playerctl: {e_restart}")
                playerctl_process = None
                await asyncio.sleep(PLAYERCTL_RESTART_FAILURE_DELAY_SECONDS)
            is_parsing_metadata_block = False; active_metadata_dict = {}
            continue

        line_bytes = b''
        try:
            # Add timeout to readline
            line_bytes = await asyncio.wait_for(
                playerctl_process.stdout.readline(),
                timeout=PLAYERCTL_READLINE_TIMEOUT_SECONDS
            )

            if not line_bytes:
                debug_log("playerctl stdout EOF. Process likely exited.")
                await playerctl_process.wait()
                playerctl_process = None
                is_parsing_metadata_block = False; active_metadata_dict = {}
                continue

            line = line_bytes.decode('utf-8').strip()
            if not line: continue

            if line == PLAYERCTL_DATA_BEGIN_MARKER:
                active_metadata_dict = {}
                is_parsing_metadata_block = True
                continue

            elif line == PLAYERCTL_DATA_END_MARKER:
                if is_parsing_metadata_block:
                    is_parsing_metadata_block = False

                    title = active_metadata_dict.get("title", "").strip()
                    artist = active_metadata_dict.get("artist", "").strip()
                    status_from_block = active_metadata_dict.get("status", "stopped").lower()
                    current_playerctl_track_id = active_metadata_dict.get("track_id", "")
                    try:
                        position_us_str = active_metadata_dict.get("position_us", "0")
                        position_us = int(position_us_str)
                    except ValueError:
                        position_us = 0
                    position_ms = position_us // 1000

                    cached_song_info["status"] = status_from_block # Update cached status from block

                    song_changed_via_track_id = (current_playerctl_track_id != last_processed_playerctl_track_id_from_event)
                    song_changed_via_meta = (
                            title.lower() != (cached_song_info.get("title") or "").lower() or
                            artist.lower() != (cached_song_info.get("artist") or "").lower()
                    )
                    song_changed = song_changed_via_track_id or (not current_playerctl_track_id and song_changed_via_meta)

                    lyrics_file_exists = os.path.exists(cached_song_info.get("lyrics_file_path", "")) if cached_song_info.get("lyrics_file_path") else False
                    cache_expired = (time.time() - cached_song_info.get("last_fetched_timestamp", 0)) > 3600

                    should_fetch_new_lyrics = (song_changed and title) or \
                                              (title and not cached_song_info.get("lyrics_parsed_content")) or \
                                              (title and not lyrics_file_exists) or \
                                              (title and cache_expired)

                    if should_fetch_new_lyrics:
                        debug_log(f"Processing lyrics for: '{title}' by '{artist}' (MPRIS ID: {current_playerctl_track_id})")
                        parsed_lyrics_from_file, file_path_from_load = load_lyrics_from_local_file(title, artist)
                        if parsed_lyrics_from_file:
                            parsed_lyrics = parsed_lyrics_from_file
                            qq_song_mid = cached_song_info.get("qq_song_mid")
                        else:
                            parsed_lyrics, qq_song_mid, lrc_content_raw = await fetch_lyrics_from_qqmusic_api(title, artist)
                            if parsed_lyrics and lrc_content_raw:
                                save_lyrics_to_local_file(title, artist, lrc_content_raw)
                                file_path_from_load = get_lyrics_file_path(title, artist)

                        cached_song_info["title"] = title
                        cached_song_info["artist"] = artist
                        cached_song_info["qq_song_mid"] = qq_song_mid
                        cached_song_info["lyrics_file_path"] = file_path_from_load
                        cached_song_info["lyrics_parsed_content"] = parsed_lyrics
                        cached_song_info["last_fetched_timestamp"] = time.time()
                        cached_song_info["playerctl_track_id"] = current_playerctl_track_id
                        save_song_info_cache(cached_song_info)

                    lyrics_to_display = cached_song_info["lyrics_parsed_content"]
                    current_lyric_line, next_lyric_line = "", ""
                    display_text, css_class_out = "No Media Playing", "offline"

                    if status_from_block in ["playing", "paused"]:
                        if lyrics_to_display:
                            found_current = False
                            for i in range(len(lyrics_to_display)):
                                ts, text = lyrics_to_display[i]
                                if position_ms >= ts:
                                    current_lyric_line = text
                                    found_current = True
                                    if i + 1 < len(lyrics_to_display): next_lyric_line = lyrics_to_display[i+1][1]
                                else: break
                            if not found_current and lyrics_to_display:
                                current_lyric_line = lyrics_to_display[0][1]
                                if len(lyrics_to_display) > 1: next_lyric_line = lyrics_to_display[1][1]
                            if lyrics_to_display and position_ms >= lyrics_to_display[-1][0]: next_lyric_line = ""
                            display_text = current_lyric_line if current_lyric_line else "..."
                        else:
                            display_text = f"{title} - {artist}" if title else "Lyrics not found"
                        css_class_out = status_from_block

                    tooltip_parts = [f"Song: {title or 'N/A'}", f"Artist: {artist or 'N/A'}", f"Status: {status_from_block.capitalize()}"]
                    if current_lyric_line: tooltip_parts.append(f"Now: {current_lyric_line}")
                    if next_lyric_line: tooltip_parts.append(f"Next: {next_lyric_line}")
                    elif lyrics_to_display and current_lyric_line and current_lyric_line == lyrics_to_display[-1][1]:
                        tooltip_parts.append("Next: (End)")

                    output_waybar_json(display_text, "\n".join(tooltip_parts), css_class_out)
                    last_processed_playerctl_track_id_from_event = current_playerctl_track_id
                    active_metadata_dict = {}
                else:
                    debug_log(f"Warning: Received END_MARKER ('{line}') without active block.")
                continue

            elif is_parsing_metadata_block:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    key, value = parts[0].strip(), parts[1].strip()
                    active_metadata_dict[key] = value
                else:
                    debug_log(f"Warning: Malformed line in metadata block: '{line}'")
                continue

            else: # Standalone line from playerctl
                debug_log(f"Standalone line from playerctl: '{line}'")
                lower_line = line.lower()
                # Check if this implies player stopped/gone, if so, update Waybar & cache
                if "no player is running" in lower_line or \
                        "no players found" in lower_line or \
                        "stopped" in lower_line: # Check if playerctl itself says stopped
                    if cached_song_info.get("status") != "stopped" or cached_song_info.get("title"):
                        debug_log(f"Standalone line indicates player stopped/gone ('{line}'). Updating status.")
                        output_waybar_json("No Media", "Player stopped or not running", "offline")
                        cached_song_info.update({"title": None, "artist": None, "lyrics_parsed_content": [],
                                                 "playerctl_track_id": None, "qq_song_mid": None,
                                                 "lyrics_file_path": None, "status": "stopped"})
                        save_song_info_cache(cached_song_info)
                        last_processed_playerctl_track_id_from_event = None
                continue # Handled standalone line

        except asyncio.TimeoutError:
            debug_log(f"Timeout ({PLAYERCTL_READLINE_TIMEOUT_SECONDS}s) waiting for playerctl update. Checking status manually.")
            current_cached_status = cached_song_info.get("status", "stopped")

            try:
                status_check_cmd = PLAYERCTL_CMD_BASE + ["status"] # e.g., ["playerctl", "--player=playerctld", "status"]
                proc = await asyncio.create_subprocess_exec(
                    *status_check_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=PLAYERCTL_STATUS_CHECK_TIMEOUT_SECONDS
                )

                actual_player_status_stdout = stdout_bytes.decode('utf-8').strip().lower()
                actual_player_status_stderr = stderr_bytes.decode('utf-8').strip().lower()

                player_is_effectively_stopped = False
                if proc.returncode != 0:
                    debug_log(f"Manual status check: playerctl command failed (Code: {proc.returncode}, Stderr: '{actual_player_status_stderr}'). Assuming no player.")
                    player_is_effectively_stopped = True
                elif "no player is running" in actual_player_status_stdout or \
                        "no players found" in actual_player_status_stdout or \
                        actual_player_status_stdout == "stopped":
                    debug_log(f"Manual status check: stdout indicates player stopped or gone ('{actual_player_status_stdout}').")
                    player_is_effectively_stopped = True

                if player_is_effectively_stopped:
                    if current_cached_status != "stopped" or cached_song_info.get("title"):
                        debug_log("Player confirmed stopped/gone by manual check after timeout. Updating Waybar and cache.")
                        output_waybar_json("No Media", "Player stopped or not responding", "offline")
                        cached_song_info.update({
                            "title": None, "artist": None, "lyrics_parsed_content": [],
                            "playerctl_track_id": None, "qq_song_mid": None,
                            "lyrics_file_path": None, "status": "stopped"
                        })
                        save_song_info_cache(cached_song_info)
                        last_processed_playerctl_track_id_from_event = None
                    else:
                        debug_log("Manual status check (timeout): Player already known to be stopped. No update.")
                else: # Player still active according to manual check
                    debug_log(f"Readline timeout, but manual status check: '{actual_player_status_stdout}'. Player still active. No change from timeout.")

            except asyncio.TimeoutError:
                debug_log("Manual 'playerctl status' check command timed out. Assuming player unresponsive.")
                if current_cached_status != "stopped" or cached_song_info.get("title"):
                    output_waybar_json("No Media", "Player unresponsive (status check timeout)", "error")
                    cached_song_info.update({"title": None, "artist": None, "lyrics_parsed_content": [], "playerctl_track_id": None, "qq_song_mid": None, "lyrics_file_path": None, "status": "stopped"})
                    save_song_info_cache(cached_song_info)
                    last_processed_playerctl_track_id_from_event = None
            except Exception as e_status_check:
                debug_log(f"Error during manual status check: {type(e_status_check).__name__} - {e_status_check}")
                # Fallback to show error, but don't change song state without confirmation
                output_waybar_json("Status Check Error", "Error checking player status", "error")

            continue # Go back to the start of the while loop to try readline again

        except Exception as e: # General exception catch for the main loop
            debug_log(f"Unhandled exception in playerctl listen loop: {type(e).__name__}: {e}")
            import traceback
            log_file = get_current_log_file_path()
            with open(log_file, "a", encoding="utf-8") as f:
                traceback.print_exc(file=f)
            output_waybar_json("Script Error", str(e), "error")
            # If playerctl process might be bad, nullify it to trigger restart
            if playerctl_process and playerctl_process.returncode is None:
                try: playerctl_process.kill()
                except ProcessLookupError: pass # Already gone
            playerctl_process = None
            await asyncio.sleep(PLAYERCTL_RESTART_DELAY_SECONDS)


# --- Script Entry Point ---
if __name__ == "__main__":
    try:
        os.makedirs(CACHE_BASE_DIR, exist_ok=True)
        os.makedirs(LYRICS_FILES_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)

        debug_log_path_at_start = get_current_log_file_path()
        with open(debug_log_path_at_start, "w", encoding="utf-8") as f:
            f.write(f"--- Starting lyrics.py debug session at {datetime.now()} ---\n")
        asyncio.run(listen_to_playerctl_and_update_waybar())
    except KeyboardInterrupt:
        debug_log("Script interrupted by KeyboardInterrupt.")
        print("\nScript terminated by user.", file=sys.stderr, flush=True)
    except Exception as e:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fatal_msg = f"[{ts}] FATAL SCRIPT ERROR ({type(e).__name__}): {e}"
        print(fatal_msg, file=sys.stderr, flush=True)
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            log_file = get_current_log_file_path()
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(fatal_msg + "\n")
                import traceback
                traceback.print_exc(file=f)
        except Exception as log_ex:
            print(f"[{ts}] CRITICAL: Could not write fatal error to log file: {log_ex}", file=sys.stderr, flush=True)
        try: output_waybar_json("Fatal Script Error", str(e), "error")
        except: pass
    finally:
        debug_log("lyrics.py script terminated.")