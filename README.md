# YtubeCatcher

A YouTube player and clip extractor in one window: search, watch, mark the parts you want, and save them as **voice only** (background music removed), audio, or MP4 video.

## Quick start (Windows)

1. Install Python 3.10+ (tick "Add to PATH").
2. Double-click **run.bat**. The first run creates `.venv`, installs yt-dlp + ffmpeg + python-mpv and downloads the mpv player library (`libmpv-2.dll`, ~30 MB).
3. Optional, for *Voice only*: run **install_vocals.bat** once (PyTorch + BS-RoFormer, ~3.5 GB).

## Using the app

- Type in the search box (or paste a link / local file path) and press Enter. Results with thumbnails appear on the right; click one to play. No web browser: yt-dlp finds the stream and an embedded **mpv** plays it.
- Quality chip 360p-4K or audio only; speed chip 0.5x-2x.
- Keys: `Space`/`K` play/pause, `Left/Right` +-5 s (`Shift` +-1 s), `J` -10 s, `,` `.` frame step, `M` mute, `F` fullscreen.
- **Clips**: `I` / `O` mark a clip IN / OUT (green on the seek bar; amber line = open IN). `L` or the loop icon loops a clip to check it. Clips are remembered per video.
- **Extract**: pick *Voice only* / *Audio* / *Video*. Audio: the format chip sets MP3/M4A/WAV/FLAC/OPUS, bitrate, filter engine and padding. Video: the *Resolution* chip sets the download resolution (best / 2160p ... 360p). Several clips are always joined into ONE file (0.5 s gaps); without clips the whole video is saved. Then **Extract**. Finished files are listed - click one to preview it in the player, or the folder icon to show it in Explorer.
- **Sign in** (top right): *Open sign-in window* -> log in to YouTube there -> *Save sign-in*. Needed only for age-restricted / members-only videos or when YouTube asks to confirm you're not a bot. Firefox's cookies or a cookies.txt file also work. (Chrome/Edge's own cookies can't be read by other apps.)

`run.bat --classic` opens the older batch window (many URLs at once, playlists, start/end fields, keep-original, waveform preview).

## Engine features (ytubecatcher.py - used by the app, the classic window and the command line)

- **Mode: Audio or Video**. Audio: mp3, m4a, wav, flac, opus with selectable bitrate. Video: MP4 (H.264 + AAC) with a max-resolution cap (best / 2160 / 1440 / 1080 / 720 / 480 / 360). The timeline, padding and combine features work in both modes - video clips are cut frame-accurately, and combined videos get a short black gap between clips (clips from different sources are scaled to the first clip's size). The BGM filter is audio-only.
- Several URLs at once, or a whole playlist
- Clip a time range (`1:30` → `2:45`) without processing the rest
- **Timeline box** - paste your time segments separately from the URLs, one or more per line, in any of these forms: `6:57-7:02`, `06:57 ~ 07:02`, `6:57 to 7:02`, `6:57 7:02`, `1:02:03-1:02:30.5`. Each segment becomes its own clip, applied to every URL/file listed (prefix a line with `#2` to apply it only to the 2nd input). Ranges can still be written inline after a URL (`URL 6:57-7:02`) and then override the timeline for that line.
- **Auto padding** - every segment is extended by *Pad (s)* on both sides (default 0.3 s) so a word is never cut in half at the edge.
- **Combine clips into one file** - tick *Combine all clips into ONE file*: every clip from every line is joined in order, with a short silence gap (default 0.5 s) between them, into `<title> (N clips) (voice).mp3`. Ideal for building a clean voice reference from several dialogue moments. Optionally keep the individual clips too.
- **BGM filter (voice only)**: strips background music and other non-voice sound, saving `<title> (voice).mp3`. Run **install_vocals.bat** once to enable it. It runs on an NVIDIA GPU when present (much faster than CPU).
  - Quality `best` (default): **BS-RoFormer** vocal model - pauses between words come out silent, music is gone. ~900 MB model, downloaded on first use into `models\`.
  - Quality `fast`: Demucs htdemucs - quicker, but leaves audible music bleed
  - Optional **extra clean-up** stage (Mel-RoFormer denoise, ~600 MB model on first use): removes ambience, wind, room noise and effects that a vocal model keeps. Turn it on when the voice-only file still has non-music background
  - Optional "also save original mix"
  - Streams the audio in 60-second blocks, so RAM stays flat (~1.5 GB) even for hours-long videos; falls back to CPU automatically if the GPU runs out of memory
  - Works on **local audio/video files** too: click *Add local files...*; originals are never overwritten
- **Preview panel**: every output file is listed after a run - click one to see it, double-click to open it in your default player. Audio files show their **waveform**, videos show the **picture**; Play/Pause/Stop plays the file with sound right inside the app, and clicking anywhere on the preview seeks to that spot. *Preview file...* previews any audio/video file on disk (handy for checking a voice-only result against the original).
- **YouTube search tab**: type what you are looking for, get title / channel / length / views, select rows and add them to the download list (or double-click one). No API key needed.
- **YouTube account (same tab)**: sign the app in with your own YouTube account so searches and downloads see what you see (age-restricted, members-only, private/unlisted videos, fewer bot checks). Pick *cookies of my browser* (be logged in to youtube.com in that browser; Firefox works best - Chrome/Edge on current Windows may refuse to hand over cookies) or an exported *cookies.txt* file (browser extension "Get cookies.txt LOCALLY"). For Chrome/Edge use **1. Open sign-in window** -> log in to YouTube in the window that opens (its own profile, remembered next time) -> **2. Save sign-in**: the app asks the running browser for its cookies, writes `youtube_cookies.txt` and switches to the file option. This avoids the *Could not copy Chrome cookie database* / encryption errors you get when reading Chrome's cookies directly. *Test sign-in* checks that YouTube really treats you as signed in. Your password is never entered in the app.
- Title/artist metadata embedded
- **Auto-save**: every input (URLs, folder, format, clip times, filter settings) is saved to `settings.json` next to the app and restored on the next launch - it survives updates of `ytubecatcher.py`

## Command line

```bat
run.bat "https://www.youtube.com/watch?v=XXXX" -f mp3 -b 320
run.bat URL --video --res 1080                    (download the video as MP4, max 1080p)
run.bat URL --video --timeline "6:57-7:02, 12:10-12:30" --combine
run.bat URL --start 1:00 --end 2:30 -f wav
run.bat URL --filter-bgm -f flac
run.bat URL --filter-bgm --denoise            (BGM filter + extra clean-up)
run.bat URL --filter-bgm --quality best --keep-original
run.bat "D:\clips\interview.mp4" --filter-bgm
run.bat URL --timeline "6:57-7:02, 12:10-12:30, 20:05-20:40" --pad 0.3 --filter-bgm --combine
run.bat URL --timeline @segments.txt --filter-bgm --combine
run.bat "URL1 1:00-1:20" "URL2 3:30-3:50" --filter-bgm --combine --keep-clips
run.bat PLAYLIST_URL --playlist -o D:\audio
run.bat --search "interview elon musk" --search-limit 10     (search YouTube, print URLs)
run.bat URL --cookies-from-browser firefox --filter-bgm       (download signed in with your account)
run.bat URL --cookies D:\cookies.txt
```

## If YouTube downloads fail

YouTube changes often. `run.bat` upgrades yt-dlp on every launch. Recent yt-dlp also needs a JavaScript runtime for full YouTube support. If you see "JS runtime" / "n challenge" warnings, install **Deno** (`winget install DenoLand.Deno`) and relaunch.

## Use responsibly

Only download content you own or have permission to use. Downloading may be against YouTube's Terms of Service, and the audio stays under its owner's copyright.
