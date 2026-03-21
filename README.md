
# Inky Gallery — NAS Support Branch

This branch adds local network NAS/SMB drive browsing to the [Inky Gallery](https://github.com/doowylloh88/InkyPi-Inky-Gallery) plugin for [InkyPi](https://github.com/fatihak/InkyPi).

## What's New

-   **Network Drive Discovery** — automatically scans your local network for SMB hosts when you open the folder browser
-   **Credential Modal** — connect to a discovered host with your username and password
-   **Expandable Folder Tree** — browse NAS shares and sub-folders with image counts, matching the local folder browser style
-   **Auto-Mount** — the share is mounted automatically on connect at `/mnt/nas/<share>`
-   **Persistent Selection** — your chosen NAS folder is remembered across page loads




## Installation

```bash
inkypi plugin install inky_gallery https://github.com/doowylloh88/InkyPi-Inky-Gallery/tree/NAS-support

```
Then install the requirements below.

## Requirements

### System Package

```bash
sudo apt install cifs-utils

```

### Python Package

```bash
cd ~/InkyPi
pip install -r src/plugins/inky_gallery/requirements.txt

```

## Usage

1.  Open the Inky Gallery plugin settings in InkyPi
2.  Click **Browse** — the local folder tree loads as normal
3.  Wait a few seconds — if any SMB hosts are found on your network, a **Network Drives** section appears below the local folders
4.  Click a discovered host to open the credential modal
5.  Enter your NAS username and password and click **Connect**
6.  The share mounts automatically and its folder tree appears
7.  Browse to your photos folder and click to select it
8.  Click **Update Now** to display an image

## Notes

-   Credentials are never written to disk — they are held in memory only for the session
-   If the Pi reboots or the mount drops, simply reconnect via the UI — the folder path will be remembered but you will need to re-enter your password
-   macOS SMB shares (shared home folders) are supported out of the box
-   For Synology, QNAP, and Windows shares, the same username/password you use to access the share works here
-   `impacket` can be optionally installed (`pip install impacket`) to enable full share enumeration on servers that support it


