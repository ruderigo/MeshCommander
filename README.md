# MeshCommander
A hardware-agnostic TUI and bridge for sovereign Reticulum mesh networking on handhelds and LoRa modules.

MeshCommander is a specialized interface and hardware bridge designed to turn portable gaming hardware into a resilient, off-grid communication terminal. It leverages the Reticulum Network Stack (RNS) to provide encrypted, peer-to-peer messaging without reliance on centralized infrastructure.

📁 Repository Structure
/R36S: Contains the Python-based Framebuffer UI. This is the "brain" of the unit, handling user input via gamepad, rendering the TUI without an X server, and managing the Reticulum logic.

/HeltecESP32S3V3: Contains the configuration files, firmware notes, and setup scripts for the LoRa transport layer. This transforms the Heltec module into a dedicated RNode.

🛠 Tech StackComponentTechnologyNetwork ProtocolReticulum Network Stack (RNS)TransportLoRa (915MHz) / TCP-IPHardware interfaceUSB-C Serial (RNode API)GraphicsPygame (Framebuffer Rendering)InputSDL2 Joystick Mapping (Go-Super / R36S)

🛰 Core Concepts
Sovereign Networking
The system is designed for maximum autonomy. By using the Heltec V3 as a transport, the R36S becomes a node in a self-healing mesh. Even without a LoRa module, the system can failover to TCP/Internet bridges to stay connected to the global Reticulum network.

Framebuffer UI (FBUI)
To keep the system lightweight and compatible with retro-handheld OSs like ArkOS, MeshCommander renders directly to /dev/fb0. This eliminates the overhead of a desktop environment, saving battery and processing power for the mesh stack.

🛰️ MeshCommander: R36S Setup Guide
🚀 Overview
MeshCommander turns your handheld into a node in a self-healing mesh. Even without a LoRa module, the system can failover to TCP/Internet bridges to stay connected to the global Reticulum network. To keep the system lightweight for ArkOS, MeshCommander renders directly to /dev/fb0.

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
TCP transport retries automatically in background
