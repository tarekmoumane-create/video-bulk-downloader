#!/bin/bash
# Builds dist/Video Downloader.app and dist/Video-Downloader-mac.zip
set -e
cd "$(dirname "$0")/.."
APP="dist/Video Downloader.app"
rm -rf dist && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp mac/Info.plist "$APP/Contents/"
cp mac/launch "$APP/Contents/MacOS/launch" && chmod +x "$APP/Contents/MacOS/launch"
cp server.py index.html requirements.txt "$APP/Contents/Resources/"
ditto -c -k --keepParent "$APP" dist/Video-Downloader-mac.zip
echo "Built dist/Video-Downloader-mac.zip"
