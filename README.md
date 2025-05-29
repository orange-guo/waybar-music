# Waybar Music Modules: Player Info & Lyrics

[Waybar](https://github.com/Alexays/Waybar) custom modules to display MPRIS media player information (`player.py`) and synchronized lyrics (`lyrics.py`).

**(It's highly recommended to add a screenshot of your Waybar with these modules active here)**

## Key Features

### Player Info (`player.py`)

* Shows: Status Icon, Song Title - Artist.
* Tooltip (on hover): Player name, song, artist, album, progress, volume, cover art status.
* Album Art: Fetches and caches locally, with a `current_song_art` symlink.
* Controls: Click for play/pause, right-click for next, scroll for volume, middle-click to view art.

### Lyrics Display (`lyrics.py`)

* Shows synchronized lyrics.
* Source: Local LRC files first, then QQMusic API (via `qqmusic-api`).
* Tooltip (on hover): Song, artist, status, current/next lyric line.

## Requirements

1. **`playerctl`**: For media player interaction.
2. **Python 3**:
    * `requests` (optional for `player.py` album art, falls back to built-in `urllib`).
    * `qqmusic-api` (for `lyrics.py`): `pip install qqmusic-api requests`
3. **Fonts**:
    * Font Awesome (or similar for icons like play/pause).

## Quick Setup

1. Copy `scripts` to `~/.config/waybar/`

2. Edit `~/.config/waybar/config.jsonc`.

```jsonc
// In "modules-center" or your preferred spot:
"custom/lyrics": {
    "format": "{}",
    "return-type": "json",
    "max-length": 80,
    "exec": "~/.config/waybar/scripts/lyrics.py"
},
// In "modules-right" or your preferred spot:
"custom/player": {
    "format": "{}",
    "return-type": "json",
    "max-length": 50,
    "exec": "~/.config/waybar/scripts/player.py",
    "on-scroll-up": "playerctl --player=playerctld volume 0.05+",
    "on-scroll-down": "playerctl --player=playerctld volume 0.05-",
    "on-click": "playerctl --player=playerctld play-pause",
    "on-click-middle": "xdg-open ~/.cache/waybar/player/album_covers/current_song_art || feh ~/.cache/waybar/player/album_covers/current_song_art",
    "on-click-right": "playerctl --player=playerctld next"
}
```

3. Edit `~/.config/waybar/style.css`, for example:

```css
#custom-lyrics {
    padding: 0 4px;
    color: #a3be8c;
}

#custom-lyrics.playing {
    color: #ffffff;
}

#custom-lyrics.paused {
    color: #ebcb8b;
}

#custom-lyrics.no-lyrics {
    color: #bf616a;
}

#custom-lyrics.no-metadata,
#custom-lyrics.offline,
#custom-lyrics.empty {
    color: #6c7086;
}

#custom-lyrics:hover {
    background: inherit;
    box-shadow: inset 0 -3px #ffffff;
}

#custom-player {
    color: #ffffff;
}

#custom-player:hover {
    background: inherit;
    box-shadow: inset 0 -3px #ffffff;
}

#custom-player.offline,
#custom-player.empty {
    color: #6c7086;
}
```

4. Restart waybar

## Troubleshooting

* Check script logs for errors:
    * Player: `~/.cache/waybar/player/logs/YYYY-MM-DD.log`
    * Lyrics: `~/.cache/waybar/lyrics/logs/YYYY-MM-DD.log`