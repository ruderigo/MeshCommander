# MeshCommander
A hardware-agnostic TUI and bridge for sovereign Reticulum mesh networking on handhelds and LoRa modules.

MeshCommander is a specialized interface and hardware bridge designed to turn portable gaming hardware into a resilient, off-grid communication terminal. It leverages the Reticulum Network Stack (RNS) to provide encrypted, peer-to-peer messaging without reliance on centralized infrastructure.

🤖 Hardware 
R36S-v21
Heltec ESP32-S3 V3
Powered USB-Hub

🛰️ MeshCommander: R36S Setup Guide

🚀 Overview

MeshCommander turns your handheld into a node in a self-healing mesh. To keep the system lightweight for ArkOS, MeshCommander renders directly to /dev/fb0.

📦 1. Dependencies
pip install RNS pygame numpy

⚙️ 2. Reticulum Config
File location: ~/.reticulum/config

[reticulum]
enable_transport = False
share_instance = Yes
instance_name = default

[logging]
loglevel = 4

[interfaces]
[[Heltec_LoRa]]
type = RNodeInterface
interface_enabled = True
port = /dev/ttyUSB0
frequency = 915000000
bandwidth = 125000
txpower = 7
spreadingfactor = 8
codingrate = 5

🛠️ 3. Permissions & Hardware
sudo usermod -a -G dialout $USER
sudo chmod 666 /dev/ttyUSB0

Flash RNode firmware:
pip install rns rnodeconf
rnodeconf --autoinstall

📂 4. Deploy MeshCommander
cp MeshCommander.py /roms/ports/
cp MeshCommander.sh /roms/ports/
chmod +x /roms/ports/MeshCommander.sh
chmod +x /roms/ports/MeshCommander.py

Launcher (MeshCommander.sh):
#!/bin/bash
export SDL_VIDEODRIVER=offscreen
export SDL_NOMOUSE=1
python3 /roms/ports/MeshCommander.py

🎮 5. Button Map (R36S / GO-Super)
START: Cycle panels
SELECT: Toggle transport
A: Confirm / add char
B: Delete / back
Y: Send message
D-pad: Navigate / scroll

Notes

Tested on dArkOSRE (Debian Trixie, kernel 4.4.189) https://github.com/southoz/dArkOSRE-R36 
Input reads directly from /dev/input/event2 via evdev
Framebuffer writes to /dev/fb0 via background thread
