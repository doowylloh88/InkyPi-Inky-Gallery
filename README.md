# Inky Gallery

An upgrade to the image album plugin.  Now with optional tag filtering, captions, image adjustments, and color presets.

## 🚀 Features

- Connects to image albums on your Raspberry Pi
- Loads album names dynamically
- Loads tag suggestions dynamically
- Optional tag filtering
- Optional caption overlay from:
  - IPTC image metadata
- Image enhancement controls
- Color presets support via `lut.json`

## Screenshot

![screenshot](https://github.com/doowylloh88/InkyPi-Inky-Gallery/blob/main/inky_gallery/docs/images/screenshot.png)

![screenshot](https://github.com/doowylloh88/InkyPi-Inky-Gallery/blob/main/inky_gallery/docs/images/dog.png)

## 🛠️ Installation

3. Install the plugin using the InkyPi CLI, providing the plugin ID & GitHub repository URL:

```bash
inkypi plugin install image_gallery https://github.com/doowylloh88/image_gallery
```

## How it works

1.  Enter your Image Folder Name.  Images / Photos must be located in one of the following folders in the root directory.  You can uses sub-folders too.  Software like [Cyberduck](https://cyberduck.io/download/) is great for transfering photos to a Pi
-   `/Pictures`
    
-   `/images`
    
-   `/media`
    
-   `/photos`
    
   
2.  The plugin searches for albums and tags.  If you add / delete tags just click "Browse" and select the folder again and the plugin will repopulate the tags.

3. Optionally enter a tag filter (Note: png files don't carry over tags)
    
4.  Optionally enable captions.  Note: Captions need to be in [  ] for the plugin to display them.  For example, `[family]` or `[mountans]`
    
5.  Optionally choose a LUT / color preset from the drop-down
    
7.  The plugin fetches a random image in the album, processes it, and returns it for display

## Caveats

- The LUTs / color presets can be edited in the lut.json file
- (Optional) To save space on the Rasberry Pi, pre-process all images with  800 px width and 150 DPI using your favorite photo editing software
- Some of the presets are based on [Inky Photo Frame ](https://github.com/mehdi7129) I highly suggest you tweak them based on your Spectra6's screen
- The sliders for saturation, brightness, etc. will carry over to the main settings screen, but they will not be saved. I haven’t found a way around that yet. They also do not seem to affect other modules
- Speaking of sliding, this plug-in was 100% created using vibe- coding & a lot of yelling at ChatGPT.  An actual coder should take over the project to maintain it
