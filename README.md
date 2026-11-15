# Bulk Video Downloader

Download videos from TikTok, Instagram, and YouTube in bulk with no watermark, saved in high quality to your Downloads folder. Optionally save them to Google Drive.

## Features

- Paste TikTok, Instagram, or YouTube links directly
- Load links from a Google Sheet
- Upload a CSV or Excel file with links
- Download videos without watermarks in the highest available quality
- Organize downloads by date and time
- Optional: Save to Google Drive automatically

## Requirements

- Python 3.9+
- `yt-dlp`, `requests`, `openpyxl`, `imageio-ffmpeg` (see requirements.txt)
- `rclone` (optional, only needed for Google Drive uploads)
- ffmpeg (bundled automatically through imageio-ffmpeg)

## Mac app

Download `Video-Downloader-mac.zip` from the latest release, unzip it, and open **Video Downloader**.
The first time, right-click the app and choose Open (it isn't signed by Apple).
On first open it sets itself up (needs Python 3.9+, about a minute), then opens in your browser.
No API keys or accounts are needed.

## Pricing

Pasting links and uploading files is free. Importing straight from a Google Sheet is $4.99 a month,
paid through Stripe Checkout from the Google Sheet tab. The app stores only a subscription key in
`~/Library/Application Support/Video Downloader/license.json` and checks it against the website's
`/api/license` endpoint. Set `VBD_API` to point the app at a different backend.

Build the app yourself with `./mac/build.sh`.

## Run from source

### 1. Clone this project

```bash
git clone https://github.com/tarekmoumane-create/video-bulk-downloader.git
cd video-bulk-downloader
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up Google Drive access (optional)

If you want to save downloads to Google Drive, install and configure rclone:

```bash
# Install rclone
brew install rclone  # macOS
# or download from https://rclone.org/downloads/

# Configure Google Drive
rclone config

# Choose 'n' for new remote
# Name it: gdrive
# Choose option 15 (Google Drive)
# Follow the prompts to authorize with your Google account
# When asked about "Use auto config?", choose 'n' if running headless
```

Verify the configuration works:

```bash
rclone about gdrive:
```

## Usage

### Start the server

```bash
python3 server.py
```

Then open http://127.0.0.1:8765 in your browser.

### Three ways to add videos

**Paste links:** Copy/paste TikTok, Instagram, or YouTube URLs directly, one per line. Examples:
- TikTok: `https://www.tiktok.com/@username/video/123456789`
- Instagram: `https://www.instagram.com/p/ABC123DEF/` or `https://www.instagram.com/reel/ABC123DEF/`
- YouTube: `https://www.youtube.com/watch?v=dQw4w9WgXcQ` or `https://youtu.be/dQw4w9WgXcQ`

**Google Sheet:** Paste a Google Sheets link (must be shared publicly or with your Google account)

**Upload file:** Upload a CSV or Excel file containing video URLs

### Download videos

Click "Download videos" to start. Videos will be saved to `~/Downloads/Videos/<date time>/`

### Save to Google Drive (optional)

After downloading, click "Save to Google Drive" to upload all videos to your Drive.

## Notes

- Downloads are saved with no watermark in the highest available quality
- Video filenames include the creator name and TikTok ID
- Requires active internet connection
- Videos are downloaded sequentially to avoid rate limiting
- If TikTok rate-limits you, wait a minute and try again

## Troubleshooting

### Google Sheet not loading
- Make sure the sheet is shared ("Anyone with the link" or shared with your Google account)
- Check that the URL is a valid Google Sheets link

### Google Drive upload fails
- Run `rclone about gdrive:` to verify your Google Drive connection
- Check your internet connection

### Video download fails
- Some videos may be unavailable (deleted, private, age-restricted, or region-locked)
- Try again in a minute if TikTok rate-limits you
- Make sure you're using valid TikTok video links

## License

MIT
