<div align="center">

# YtubeCatcher

**Search, watch and cut YouTube videos, then save just the parts you want: the voice alone (background music removed), the audio, or the video.**

One window, no web browser, no API key. Runs locally on Windows.

<img src="docs/screenshots/hero.png?v=3" alt="YtubeCatcher main window: player with marked clips, search results with thumbnails, and the Extract panel" width="100%">

</div>

## How it works

<p align="center"><img src="docs/workflow.svg?v=3" alt="1 Search, 2 Watch, 3 Mark clips with I and O, 4 Extract as voice only, audio or video" width="100%"></p>

1. **Search** YouTube from the top bar, or paste a link. To play a video or audio file from your computer, click the **folder button** next to search. Results appear on the right with thumbnails, length, channel and views. Your recent searches and recently played videos are one click away.
2. **Watch** in the built-in player (mpv). yt-dlp finds the stream; quality goes up to 4K, and there's an audio-only option. Playback speed goes from 0.1x to 5x (presets, or type your own value).
3. **Mark clips** with <kbd>I</kbd> (in) and <kbd>O</kbd> (out). Each clip shows up green on the seek bar. For exact cuts, pause and step with <kbd>←</kbd> <kbd>→</kbd> one frame at a time, or switch on **frame-by-frame mode** (<kbd>E</kbd>). <kbd>L</kbd> loops a clip so you can check it.
4. **Extract** as *Voice only*, *Audio* or *Video*. Several clips are either merged into **one file** or saved as **separate files**, your choice; with no clips, the whole video is saved.

## Features

### Search results and Recent

<img src="docs/screenshots/sidebar-tabs.png?v=3" alt="Sidebar tabs: search results with recent-search chips, and the Recent list with clip counts" width="80%">

The sidebar has two tabs:

- **Search results**: your *recent searches* appear as chips above the results. Click one to run it again, right-click to remove it.
- **Recent**: the videos you've played, newest first, with each video's clip count and when you watched it. Click one to reopen it with its clips, or use × to remove it.

Both lists are kept between sessions, and thumbnails are cached.

### Play files from your computer

Click the **folder button** next to the search button (or press <kbd>Ctrl</kbd>+<kbd>O</kbd>) and pick a video or audio file: MP4, MKV, WEBM, MOV, AVI, MP3, M4A, WAV, FLAC and more. You can also paste a file path into the search box. Local files work like YouTube videos: mark clips, take screenshots, and extract voice only, audio or video. They also appear in *Recent*, with a thumbnail taken from the video.

### Cache ahead: smooth seeking
The **Cache** chip in the control bar (on by default) keeps downloading the video ahead of where you're watching, and keeps the part you've already watched. The seek bar shows the downloaded part in light grey, and the chip shows how much is ready (`Cached +2:31`, or `Cached all`). Jumping around inside the downloaded part is instant. YouTube sends a normal stream at about playback speed, so the app fetches it in short ranged pieces at full download speed instead. A typical video is fully cached within seconds. The cache is kept in a temporary file, not in memory. Click the chip to turn it off if you only want a short buffer, for example on a metered connection.

### Screenshots of any frame

<img src="docs/screenshots/screenshot-bar.png?v=3" alt="Camera button in the control bar and a saved screenshot in the output list" width="100%">

Pause on the frame you want (step with <kbd>←</kbd> <kbd>→</kbd> or frame-by-frame mode), then click the **camera button** or press <kbd>S</kbd>. The frame is saved as a PNG at the video's own resolution (up to 4K, not the size of the window) in `Screenshots` inside your save folder. It's named after the video and the exact time, for example `Evening skyline … [1-59.5].png`. If the video is playing, it pauses first so you get the frame you see. Saved pictures appear in the list under the panel: click one to open it, or click the folder icon to show it in Explorer.

### Frame-by-frame mode

<img src="docs/screenshots/frame-mode.png?v=3" alt="Frame-by-frame mode: film-strip button active, time shown in milliseconds with the frame number" width="100%">

Press <kbd>E</kbd> or click the film-strip button to turn it on or off. While it's on:

- the video pauses, and <kbd>←</kbd> <kbd>→</kbd> step exactly **one frame** (<kbd>Shift</kbd>: 10 frames);
- the time readout shows milliseconds and the **frame number**;
- IN/OUT marks keep the exact frame time (for example `1:01.367`) instead of rounding to 0.1 s, and the cut uses those exact times.

Even with the mode off, <kbd>←</kbd> <kbd>→</kbd> step one frame whenever the video is paused.

### Clips and one-click extraction

<img src="docs/screenshots/clips-extract.png?v=3" alt="Clips list and the Extract panel" width="100%">

- **Every video keeps its own clips.** Switch to another video and its clips (and an unfinished IN mark) are put aside; switch back and they return. Clips are saved between sessions, and the same video opened through any kind of link shares them.
- **Save as:** *Voice only*, *Audio* (MP3 / M4A / WAV / FLAC / OPUS with your choice of bitrate) or *Video* (MP4).
- **Part:** *Whole video*, or *N clips*. With two or more clips, pick *Merge into 1 file* (joined in order with a 0.5 s gap) or *Separate files* (one file per clip). Each clip is padded (0.3 s by default) so no word gets cut in half.
- Finished files are listed under the panel. Click one to play it back in the player, or click the folder icon to show it in Explorer.

### Voice only: background music removed

<img src="docs/screenshots/extracting.png?v=3" alt="Extracting three clips as voice only, with progress" width="100%">

The **BGM filter** separates the voice from music and effects using the **BS-RoFormer** AI vocal model. Pauses between words come out silent. It's made for building clean voice references, for example for voice cloning or text-to-speech. An optional **Extra clean-up** pass (Mel-RoFormer) also removes room noise, wind and ambience. It runs on an NVIDIA GPU when there is one. Setup is one-time: click *Voice only ↓* and the app offers to install it (about 3.5 GB).

### Video at the resolution you choose

<img src="docs/screenshots/video-resolution.png?v=3" alt="Video mode with the Resolution menu open" width="100%">

In *Video* mode, the **Resolution** menu sets the download resolution: best available, or 2160p down to 360p. Cuts are frame-accurate, and clips are saved as one merged MP4 or one MP4 per clip.

### Sign in with your YouTube account

<img src="docs/screenshots/signin-menu.png?v=3" alt="Sign-in menu" width="60%">

You need to sign in only for age-restricted or members-only videos, or when YouTube asks you to confirm you're not a bot. Choose **Sign in with a browser window**, then log in to YouTube in the Chrome/Edge window that opens. The app notices the login by itself and saves it, and the button changes to *Signed in*. Your password is never typed into the app, and the window keeps its own profile, so you stay logged in next time. Firefox cookies or an exported `cookies.txt` also work.

### Built to look clean

A dark, YouTube-style interface. It's DPI-aware, so text stays sharp on 125–200 % displays. Icons, buttons and knobs are drawn anti-aliased. Titles in Chinese, Japanese and Korean use Microsoft YaHei UI.

## Install (Windows)

Download **`YtubeCatcher-Setup-<version>.exe`** from the [Releases](https://github.com/folgerwang/YtubeCatcher/releases) page and run it. You don't need Python or admin rights. The installer puts the app in `%LOCALAPPDATA%\Programs\YtubeCatcher`, with its own Python, yt-dlp, ffmpeg and the mpv player, and adds a Start menu shortcut (and a desktop shortcut if you want one).

- yt-dlp updates itself in the background, at most once a day; the new version is used from the next start.
- *Voice only* works the same way as below: click *Voice only ↓* in the app to install the BGM filter.
- To remove it, use *Settings > Apps > Installed apps > YtubeCatcher*. This also deletes its settings, sign-in and downloaded voice models. Your saved clips in `Music\YtubeCatcher` are kept.

To build the installer yourself, double-click **`make_package.bat`** (or run `make_package.bat 1.2.0` to set the version; the default is today's date). The first time, it sets up what the build needs: the Python environment, `libmpv-2.dll` and [Inno Setup 6](https://jrsoftware.org/isinfo.php) (through winget). The installer is written to `dist\`.

## Run from source (Windows)

1. Install **Python 3.10+** (tick *Add python.exe to PATH*).
2. Double-click **`run.bat`**. The first run sets everything up next to the app: a Python virtual environment, yt-dlp, ffmpeg, the mpv player library (`libmpv-2.dll`), Pillow, and **Deno** (the JavaScript runtime yt-dlp needs for YouTube).
3. Optional, for *Voice only*: click *Voice only ↓* in the app, or run **`install_vocals.bat`**.

Later launches start straight away. `run.bat` also updates yt-dlp each time, because YouTube changes often.

## Keyboard shortcuts

| Key | Action | Key | Action |
|---|---|---|---|
| <kbd>Space</kbd> / <kbd>K</kbd> | Play / pause | <kbd>I</kbd> | Mark clip IN |
| <kbd>←</kbd> <kbd>→</kbd> | Back / forward 5 s; **one frame** when paused or in frame mode | <kbd>O</kbd> | Mark clip OUT |
| <kbd>Shift</kbd>+<kbd>←</kbd> <kbd>→</kbd> | Back / forward 1 s (frame mode: 10 frames) | <kbd>L</kbd> | Loop the selected clip (again: stop) |
| <kbd>J</kbd> | Back 10 s | <kbd>Del</kbd> | Delete the selected clip |
| <kbd>,</kbd> <kbd>.</kbd> | Previous / next frame | <kbd>M</kbd> | Mute |
| <kbd>F</kbd> / double-click | Full screen: player only (<kbd>Esc</kbd> to leave) | <kbd>Enter</kbd> | Search / open the pasted link |
| <kbd>E</kbd> | Frame-by-frame mode on / off | <kbd>S</kbd> | Screenshot of the current frame |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | Play a file from your computer | | |

## Output

Files go to `Music\YtubeCatcher` in your user folder unless you pick another folder. Names include the video title and what was saved:

```
Evening skyline — relaxing mountain timelapse (4K) (3 clips) (voice).mp3
Evening skyline — relaxing mountain timelapse (4K) (3 clips).mp4
Evening skyline — relaxing mountain timelapse (4K).m4a
Screenshots\Evening skyline — relaxing mountain timelapse (4K) [1-59.5].png
```

Title and artist metadata are embedded in audio files. Screenshots go to the `Screenshots` subfolder.

## More ways to use it

**Batch window:** `run.bat --classic` opens the older YtubeCatcher window. It handles many URLs at once, whole playlists, a free-form timeline box (`6:57-7:02`, `06:57 ~ 07:02`, `#2 1:00-1:20` for input no. 2), start/end limits, keeping the original mix, and a waveform/picture preview panel.

**Command line:**

```bat
run.bat "https://www.youtube.com/watch?v=XXXX" -f mp3 -b 320
run.bat URL --video --res 1080                                 (MP4, max 1080p)
run.bat URL --timeline "6:57-7:02, 12:10-12:30" --filter-bgm --combine
run.bat URL --filter-bgm --denoise -f flac                     (voice only + extra clean-up)
run.bat "D:\clips\interview.mp4" --filter-bgm                   (local files work too)
run.bat PLAYLIST_URL --playlist -o D:\audio
run.bat --search "piano practice" --search-limit 10            (search, print URLs)
run.bat URL --cookies youtube_cookies.txt                      (download signed in)
```

`run.bat --help` lists every option.

## Troubleshooting

| You see | What to do |
|---|---|
| *Requested format is not available* | This usually happens while you're signed in, before Deno is installed. The app then plays the video signed out and sets Deno up automatically. If Deno can't be downloaded, run `winget install DenoLand.Deno`. |
| *Could not copy Chrome cookie database* | Chrome and Edge lock and encrypt their cookies, so other apps can't read them. Use **Sign in → Sign in with a browser window** instead. |
| *Voice only ↓* | The BGM filter isn't installed yet. Click it to install (one time, about 3.5 GB). |
| Playback or downloads suddenly fail | YouTube changed something. Relaunch with `run.bat`, which updates yt-dlp. |

## Project layout

```
ytubeviewer.py      the app (player, clips, extract, sign-in)
ytubecatcher.py     the engine: download, cut, BGM filter, combine; also the classic window and the CLI
run.bat             setup + launcher        install_vocals.bat   optional BGM filter (PyTorch, BS-RoFormer, Demucs)
make_package.bat    builds the Windows installer into dist\ (uses installer/)
installer/          build_installer.ps1, YtubeCatcher.iss (Inno Setup), app icon
docs/               README screenshots and diagram
```

The screenshots use generated demo artwork and invented titles.

## Use responsibly

Download only content you own or have permission to use. Downloading may break YouTube's Terms of Service, and the audio and video remain under their owners' copyright.
