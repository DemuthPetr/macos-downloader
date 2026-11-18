# macos-downloader

A terminal UI for downloading full macOS / OS X installers directly from Apple's CDN.
It lists the latest release of every major version from Lion (10.7) up to the newest macOS and downloads the ones you pick.

- Python 3 standard library only, no dependencies
- Works on macOS and Linux
- Interrupted downloads resume, and completed files are skipped
- New releases are picked up automatically from Apple's software update catalog

## Usage

```bash
./macos-downloader.py
```

| Key | Action |
| --- | --- |
| `↑` / `↓` (or `j` / `k`) | Move |
| `space` | Select / deselect |
| `a` | Select all / none |
| `enter` | Start downloading |
| `q` | Cancel / quit |

### Options

```bash
./macos-downloader.py --list            # print available releases and their URLs
./macos-downloader.py --all             # download everything without the TUI
./macos-downloader.py -d ~/Installers   # output directory (default: ./macOS-installers)
```

Each release goes into its own folder, e.g. `macOS-installers/OS X El Capitan 10.11.6 (15G31)/`.

## Where the files come from

| Versions | Source | What you get |
| --- | --- | --- |
| Big Sur (11) and newer | Apple software update catalog (`swscan.apple.com`) | `InstallAssistant.pkg`. Open it on a Mac to get *Install macOS ….app* in /Applications |
| High Sierra (10.13) to Catalina (10.15) | Apple software update catalog | The separate installer packages (`InstallESDDmg.pkg`, `BaseSystem.dmg`, `InstallAssistantAuto.pkg`, …), which need to be assembled into the installer app |
| Lion (10.7) to Sierra (10.12) | Fixed links from Apple's [How to download macOS](https://support.apple.com/en-us/102662) support page | `InstallMacOSX.dmg` / `InstallOS.dmg` containing an installer package |

Only the newest public release of each major version is listed; betas are skipped.
Mavericks (10.9) and Snow Leopard (10.6) are not offered by Apple as direct downloads.

## Notes

- Installers are large: downloading everything takes about 150 GB.
- The legacy DMG installer packages refuse to run on Macs with a newer macOS. On Linux you can unpack them with `7z` or `xar`.
- All downloads come straight from Apple's servers; nothing is mirrored or modified.

## License

[MIT](LICENSE). This covers the script only; the macOS installers it downloads are Apple's software and remain under Apple's terms.
