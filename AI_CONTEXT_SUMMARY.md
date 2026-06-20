# Project Context Summary

## Project Goal
Build a local GUI tool for batch downloading authorized Douyin/TikTok content from share links, with:
- Video mode for Douyin videos via yt-dlp
- Douyin image/live-photo mode for image posts, live photos, and motion posts
- TikTok image mode via gallery-dl
- Chinese UI
- Duplicate detection
- Filename/date-based organization
- Batch author-page import with auto type detection

## Current Workspace
- Repo path: `/Users/liujinrui/Documents/视频提取文字`
- Main entry: `gui.py`
- Web UI: `web/index.html`, `web/app.js`, `web/styles.css`
- Core download backends:
  - `douyin_ytdlp.py`
  - `douyin_note.py`
  - `douyin_gallerydl.py`
  - `douyin_author_batch.py`

## What Is Already Implemented

### GUI
- Local web GUI on `http://127.0.0.1:8765`
- Chinese interface
- Modes:
  - `video`
  - `douyin_media`
  - `image`
  - `douyin_author_auto`
- Author mode has dedicated input fields:
  - author page URL
  - max items to crawl
- Notes and runtime info update dynamically

### Video Mode
- Uses `yt-dlp`
- Downloads Douyin share links/videos
- Filenames now use publish time + title instead of raw numeric IDs
- Output example:
  - `2025-09-27_19-49-08_作品标题.mp4`
- Duplicate skipping supported via output file detection and manifest

### Douyin Image / Live Mode
- Resolves `v.douyin.com` and `share/slides` links
- Fetches detail JSON from Douyin
- Falls back to webapp-style detail endpoint if plain endpoint returns empty `aweme_detail`
- Prefers non-watermark image sources when available
- Downloads:
  - images
  - motion/live-photo MP4s
  - music if available
- Generates preview MP4:
  - single motion -> transcode
  - multiple motions -> merged MP4
  - still images -> slideshow MP4
- Uses publish time-based folder names
- Duplicate detection:
  - by aweme ID
  - remembers already-downloaded works in output directories
  - writes hidden per-item metadata file `.douyin_item.json`

### Author Batch Mode
- New `douyin_author_batch.py`
- Accepts a Douyin author page or author share link
- Resolves `sec_user_id`
- Fetches author post list from Douyin webapp API
- Auto-detects each aweme type:
  - video -> video mode
  - image/live -> `douyin_media`
- Supports duplicate skipping by aweme ID
- Supports resume/continue:
  - writes `.douyin_author_resume.json`
  - stores cursor and seen aweme IDs
  - next run continues from last cursor in same output directory
- Manifests include per-item metadata:
  - source URL
  - author sec_user_id
  - detected mode
  - publish time
  - title
  - status
  - notes

## Key Behavior / Fixes

### Link Handling
- Douyin share-page URLs are accepted in video mode and author mode
- Image/live mode accepts:
  - `v.douyin.com/...`
  - `www.douyin.com/share/slides/...`
  - `www.douyin.com/note/...`

### Duplicate Handling
- Image/live mode:
  - skips already-downloaded aweme IDs
  - uses `.douyin_item.json`
- Author mode:
  - skips already-downloaded aweme IDs
  - resumes from saved cursor

### Error Messaging
- Video mode failures now say whether it looks like:
  - invalid/non-video link
  - login/cookie issue
  - missing/expired content
- Image/live failures now distinguish:
  - missing aweme detail
  - login/cookie issue
  - motion/preview generation problems

### UI Bug Fixed
- Author mode input was being cleared by state refresh
- Fixed by stopping the polling loop from overwriting author input while idle

### Another UI Bug Fixed
- Author mode previously accepted the whole share text as a URL
- Fixed by extracting the real URL first with `extract_url()`

## Important Implementation Details

### Files To Check First
- `gui.py`
- `douyin_author_batch.py`
- `douyin_note.py`
- `douyin_ytdlp.py`
- `web/app.js`
- `web/index.html`

### Important Hidden Files In Output Dirs
- `.douyin_item.json`
- `.douyin_author_resume.json`

### Manifest Files
- Root manifest path defaults to `manifest.csv`
- Each run writes a CSV manifest
- Author mode uses a richer manifest schema than the other modes

## Real Test Results Already Verified
- Douyin image/live share link `https://v.douyin.com/9Jpxc1J6YIE/`
  - downloads 8 images
  - downloads 8 motion files
  - generates merged preview MP4
- Douyin video share link `https://v.douyin.com/onlNSK8_0Ss/`
  - works in video mode
  - now writes `publish_time + title` filename
- Author mode:
  - author share text with extra words around the URL now parses correctly
  - empty response without cookie shows a human-readable error
- Resume logic:
  - second run skips already-seen aweme IDs
  - can continue after a previous partial crawl

## Known Limitations
- Author page list usually needs a logged-in browser cookie
- Resume is based on saved cursor + seen IDs in the same output directory
- The author list API may still return empty without login state
- Only one page / GUI server at a time was used for testing

## Current GUI State
- The GUI server is intended to run on `http://127.0.0.1:8765`
- If the page looks stale, restart `python3 gui.py --no-browser`

## Recommended Next Steps
1. Finish polishing author batch UX:
   - show resume state in the UI
   - add a button to clear resume state
2. Add a “preview author list first” mode
3. Add a small regression test suite for:
   - filename formatting
   - duplicate skipping
   - author resume behavior
   - error-message mapping

