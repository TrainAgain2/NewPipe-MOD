# NewPipe MOD v1.4.56

## What changed

- Fixes multi-audio HLS playback from the YouTube subscriptions feed: the resident helper no longer replaces the resolver choice with a Portuguese or English dub.
- Subscription videos now select the Kodi audio stream explicitly labelled **original** after the HLS streams become available (for example, **Spanish – original**).
- All alternative language/dub tracks remain available from Kodi's **Audio stream** menu after the one-time original-track selection.
- Trending, Live, Trailers, Random Music and Shorts now obtain fresh public YouTube data whenever opened; Kodi's persistent directory cache is disabled for NewPipe navigation views.
- Random Music receives a fresh query rotation and shuffle each time it is opened.
- Adds a real public YouTube Shorts feed and a dedicated YouTube-inspired Shorts menu icon.
- Trailer defaults: English/original search plus a configurable resolution ceiling up to 4K. Direct playback defaults to 1080p.
- YouTube account linking includes a dedicated fanart QR panel with the QR code, manual code and 60-second expiry countdown, followed by automatic authorization polling and subscription synchronization.

## Install

Download the release ZIP and choose **Add-ons → Install from zip file** in Kodi.

## Attribution

Original upstream: [Twilight0/plugin.video.newpipe](https://github.com/Twilight0/plugin.video.newpipe). MOD: **@TrainAgain2**.
