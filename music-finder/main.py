import shutil

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import download_audio
from recognizer import identify_songs

app = FastAPI(title="Music Finder")
app.mount("/static", StaticFiles(directory="static"), name="static")


class DetectRequest(BaseModel):
    url: str


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/api/detect")
async def detect(req: DetectRequest):
    """
    Download audio from the given URL and identify any songs in the video.

    Returns:
        {
            "video_title": str,
            "results": [
                {
                    "timestamp_s": int,
                    "title": str,
                    "artist": str,
                    "album": str,
                    "cover_url": str,
                    "spotify_url": str,
                },
                ...
            ]
        }

    On error, returns { "error": str, "results": [] }.
    """
    tmp_dir = None
    try:
        tmp_dir, mp3_path, video_title = download_audio(req.url)
        songs = await identify_songs(mp3_path)
        return {"video_title": video_title, "results": songs}
    except ValueError as e:
        return {"error": str(e), "results": []}
    except Exception as e:
        return {"error": f"Unexpected error: {e}", "results": []}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
