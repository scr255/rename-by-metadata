# rename-by-metadata

## Goal
Automatically rename photos and videos based on metadata timestamps with duplicate detection and special GoPro handling.

## How it works
Uses ExifTool by Phil Harvey and its Python wrapper pyexiftool to extract metadata timestamps.

**Photos**: Prefers `Composite:SubSecCreateDate` (with subseconds), falls back to `EXIF:CreateDate`. Uses `MakerNotes:ShutterCount` for tie-breaking when multiple photos share the same timestamp.

**Videos**: Uses `QuickTime:CreateDate`. Special GoPro handling via `QuickTime:GoPro_CPIN` sequences.

## Example Naming
- Photos: `2024_03_15_14_30_25_001.arw`
- Videos: `2024_03_15_14_30_25_001.mp4`

## Usage
```bash
# AMD64
docker-compose --profile amd up -d

# ARM64  
docker-compose --profile arm up -d
```
The terminal will output a URL with access token:
```bash
http://localhost:3000/?t=<token>
```
Copy the full URL (including token) into your browser.