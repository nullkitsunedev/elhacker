# Elhacker Downloader
<p align="center">
  <img src="https://github.com/nullkitsunedev/elhacker/raw/main/assets/Screenshot1.png" alt="Dashboard" width="1500">
</p>
A simple Windows app for downloading files from a directory-style website. I made this for dowloading courses from a specific site and manual work is not for me. It only works if all files ar like web directory archive.

## How to Use

1. Open `Elhacker.exe`.
2. Paste the website folder URL into the `URL` box.
3. Choose where the files should be saved.
4. Change the options only if needed.
5. Click `Start`.

The app will scan the folder, find files, and download them to your selected output folder.

## Options

- `Files at once`: How many different files download at the same time.
- `Speed parts`: Splits large files into parts to download faster.
- `Boost files over MB`: Only files bigger than this size use `Speed parts`.
- `Retry attempts`: How many times the app tries again if a download fails.
- `Pause seconds`: Wait time between downloads, useful for slower servers.

## Buttons

- `Browse`: Choose the download folder.
- `Start`: Begin scanning and downloading.
- `Stop`: Cancel the current download run.

## Notes

- Already downloaded files are skipped.
- Progress appears in the `Downloads` and `Activity` tabs.
- The app works best with websites that show normal folder/file links.
