# Music Finder

Paste a YouTube, Instagram, TikTok, or any other video link and the app identifies every song playing in the background. Results include song title, artist, album art, the timestamp it appears in the video, and a Spotify link.

## How it works

1. Audio is downloaded via **yt-dlp** (supports 1,700+ sites)
2. The audio is sliced into 15-second chunks using **pydub**
3. Each chunk is sent to **Shazam** via the **ShazamIO** library (free, no API key)
4. Detected songs are deduplicated and returned with Spotify links

---

## Setup on Windows

### 1. Install Python 3.10+

Download from https://www.python.org/downloads/ — check **"Add Python to PATH"** during install.

### 2. Install ffmpeg

Open **PowerShell** or **Command Prompt** and run one of:

```powershell
# Option A — Windows Package Manager (built into Windows 10/11)
winget install ffmpeg

# Option B — Chocolatey (if you have it)
choco install ffmpeg
```

After installing, open a **new** terminal and verify:

```powershell
ffmpeg -version
```

If it prints a version number, you're good.

### 3. Install Python dependencies

```powershell
cd music-finder
pip install -r requirements.txt
```

### 4. (Optional) Spotify exact track links

Without this, the Spotify button opens a pre-filled search — still useful, just not a direct link to the track.

To get exact track links:
1. Go to https://developer.spotify.com/dashboard and create a free app (takes 2 minutes)
2. Copy your **Client ID** and **Client Secret**
3. Set them before running:

```powershell
$env:SPOTIFY_CLIENT_ID     = "your_client_id"
$env:SPOTIFY_CLIENT_SECRET = "your_client_secret"
```

### 5. Start the app

```powershell
uvicorn main:app --reload --port 8000
```

Then open **http://localhost:8000** in your browser.

---

## Quick start (Windows batch script)

Double-click `run.bat` in the `music-finder` folder — it installs dependencies and starts the server automatically.

---

## Usage

1. Copy a YouTube, Instagram, or TikTok URL
2. Paste it into the input field and click **Detect**
3. Wait 30–90 seconds while the audio is downloaded and analysed
4. Songs appear with cover art, artist, album, timestamp, and a Spotify link

## Notes

- **Instagram**: Public Reels work. Private accounts and Stories require login and may fail.
- **Long videos**: Sampled every 60 seconds, capped at 15 samples to keep things fast.
- **No music found**: Either the music was too quiet relative to speech, or Shazam doesn't have it in its database.
