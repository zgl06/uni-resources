import os
import tempfile
import yt_dlp


def download_audio(url: str) -> tuple[str, str, str]:
    """
    Download audio from a video URL using yt-dlp.

    Returns:
        (tmp_dir, mp3_path, video_title)

    Raises:
        ValueError: if the video cannot be downloaded (private, geo-blocked, etc.)
    """
    tmp = tempfile.mkdtemp()
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "Unknown")
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(str(e)) from e

    mp3_files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
    if not mp3_files:
        raise ValueError("Audio extraction failed — ffmpeg may not be installed.")

    return tmp, os.path.join(tmp, mp3_files[0]), title
