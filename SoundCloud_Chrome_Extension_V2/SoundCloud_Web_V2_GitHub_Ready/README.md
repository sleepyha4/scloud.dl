# SoundCloud Downloader — Web + Chrome Extension

A minimal website and Chrome MV3 extension for exact SoundCloud track and playlist links. No example song is preloaded. The website supports individual tracks, selected playlist tracks, a whole-queue progress bar, embedded metadata/artwork, and a configurable save folder on supported desktop browsers. The extension has a small popup and adds controls near track links and playlist links on SoundCloud pages even when the popup is closed.

**Important:** This is a self-hostable **prototype**, not a deployed website or a published Chrome Web Store extension. Live extraction has not been tested from a cloud server. SoundCloud may prevent download, restrict streams to subscriptions, or use DRM. Use only for content you are authorized to save. There is no Go+ bypass, DRM circumvention or substitution of another upload. Free hosting may be unavailable due to resource and bandwidth quotas.

## Deploy website free on Render

1. Create a new GitHub repository and upload **all the contents of this folder** (including `app.py`, `Dockerfile`, `render.yaml`, `metadata.py`, `requirements.txt`, and `public/`). Do not upload a nested folder alone; the project files should be at the repository root.
2. Open [Render](https://dashboard.render.com/) and choose **New → Blueprint**, selecting that repository. Render reads `render.yaml`. Alternatively choose **New → Web Service → Docker** and select the **Free** plan.
3. Render builds the container and supplies a URL such as `https://your-service.onrender.com`.
4. Find the automatically generated `APP_KEY` in **Render → your service → Environment**. Reveal/copy its value. The website asks for it the first time you load a track on a device.
5. Open the website on your phone, Mac, or other device. Downloads are delivered by the browser into that device's Downloads directory by default.

Render's free web services **spin down after 15 minutes** idle, have an **ephemeral filesystem** and are subject to compute/bandwidth quotas. Audio files expire after roughly 45 minutes or when the service restarts. Avoid public deployments without a strong `APP_KEY`—the packaged Blueprint generates one automatically. Render may block/reschedule resource-intensive usage, and SoundCloud may block extraction from cloud IP ranges. The hosted server is not a guarantee of successful downloads.

Official deployment documentation: https://render.com/docs/free and https://render.com/docs/blueprint-spec

## Save location

The website defaults to the **browser's own Downloads folder**, on the device using the website. **Choose folder** uses the browser's File System Access API where available (Chromium-based desktop browsers on HTTPS). On Safari, iOS and other unsupported browsers, configure a custom download location in the browser's settings. A server cannot silently write to an arbitrary directory on your device.

The extension uses Chrome's configured Downloads directory. Chrome settings control where downloads are saved, including an option to ask where to save each file.

## Install Chrome extension (development)

1. Download the separate `SoundCloud_Chrome_Extension_V2.zip` or use the `extension/` directory in this project. Unzip if needed.
2. Open `chrome://extensions` in desktop Chrome.
3. Enable **Developer mode** and select **Load unpacked**.
4. Choose the unzipped **`extension/`** folder (which contains `manifest.json`).
5. Pin the extension in the Chrome toolbar and open its popup. Enter your Render website URL and its `APP_KEY`. For a custom HTTPS domain, Chrome will ask for permission to access that domain.
6. Visit SoundCloud. Download controls are inserted next to recognized track and playlist links. Track buttons submit downloads to the server; playlist buttons open the website with that playlist loaded for selection. Background alarms poll the queue and hand available audio to Chrome Downloads. DOM insertion depends on SoundCloud's changing layout, so some placements may require adjustment.

**One-click installation limitation:** Chrome stopped allowing direct inline installation from arbitrary websites. To offer a true **Add to Chrome** storefront link, the extension must first be published on the Chrome Web Store. The included site's **Add to Chrome** control currently leads to development-install instructions and an extension ZIP download; it does not secretly install browser software. After publication set `CHROME_WEB_STORE_URL` in Render to the approved extension listing URL; the site will then link directly to that listing.

Chrome reference: https://developer.chrome.com/docs/webstore/publish/ ; https://support.google.com/chrome_webstore/answer/2664769

## Run website locally on Mac

With Homebrew installed:

```bash
brew install ffmpeg
bash run.command
```

Visit http://127.0.0.1:8899. The local app does not require an access key unless you set the `APP_KEY` environment variable. A remote extension cannot reach your local Mac server unless it is running on that same device.

## Notes about downloads

- Uses `yt-dlp` to examine and select the best retrievable unprotected representation for the **exact URL**. An artist-provided original file is preferred when accessible.
- Does not create genuine WAV or 256-kbps quality by transcoding a lower-quality source.
- Uses FFprobe to validate actual files; Mutagen embeds available title, artist, release date, album, genre, source URL and original cover artwork when obtainable. Empty metadata fields are left unset.
- Files are staged briefly on the free server while queued; the browser receives individual files or a ZIP if multiple tracks succeeded. Failed tracks do not block others. A job is limited to 25 selected tracks, up to 100 playlist entries inspected, and 160 MB per track to keep free-tier resource use bounded.
- Overall queue progress is weighted by track count, with per-track progress where reported by yt-dlp. It is not a precise bytes-transferred indicator.
- Source metadata can be ambiguous; all tags reflect the exact SoundCloud upload, not third-party searches.
- The extension is desktop Chrome-specific. The website supports other devices.

## Source layout

```
app.py             FastAPI, yt-dlp pipeline and download APIs
metadata.py        Track tags, filenames, embedded artwork
public/            Responsive website and extension installation page
extension/         Manifest V3 extension, popup, page buttons, queue service worker
Dockerfile         Render deploy container with FFmpeg
render.yaml        Free Render Blueprint; auto-generated APP_KEY
run.command        Local Mac launcher
requirements.txt   Python packages
tests/             Offline API and download tests
```

## Testing

```bash
python3 -m pip install -r requirements.txt pytest httpx
python3 -m pytest -q tests
```

Offline tests exercise URL validation, protected APIs, selected playlist items, real audio-file tagging/packaging and expiration links. Live SoundCloud extraction, deployment on Render, Chrome extension insertion on the current SoundCloud DOM, and Chrome Web Store installation are **not yet verified**.
