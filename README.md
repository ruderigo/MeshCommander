# MeshCommander

<img width="2992" height="2992" alt="20260601_130615" src="https://github.com/user-attachments/assets/631748d5-5861-4b9d-8a21-f9764392c531" />


A hardware-agnostic TUI for sovereign Reticulum mesh networking on handhelds and LoRa modules.

MeshCommander turns portable gaming hardware into a resilient, off-grid communication terminal. It leverages the Reticulum Network Stack (RNS) to provide encrypted, peer-to-peer messaging without reliance on centralized infrastructure. The UI renders directly to `/dev/fb0` — no X server, no desktop environment required.

---

## 🤖 Hardware

| Component | Details |
|-----------|---------|
| R36S v21 | Running dArkOSRE (Debian Trixie, kernel 4.4.189) |
| Heltec ESP32-S3 V3 | Flashed as RNode — LoRa transport layer |
| Powered USB Hub | Required — bus power insufficient for stable Heltec operation |

**dArkOSRE:** https://github.com/southoz/dArkOSRE-R36  
**Community fork:** https://github.com/ruderigo/dArkOSRE-R36

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| OS | dArkOSRE (Debian Trixie, kernel 4.4.189 aarch64) |
| Mesh network | Reticulum Network Stack (RNS) 1.3.4 |
| UI | pygame 2.6.1 — offscreen + manual fb0 write |
| Input | Direct evdev from `/dev/input/event2` |
| Radio | RNode firmware on Heltec V3 via `/dev/ttyUSB0` |

---

## 🚀 Installation

### 1. Dependencies

```bash
pip install RNS pygame numpy
```

### 2. Reticulum Config

Create `~/.reticulum/config`:

```ini
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
```

### 3. Permissions

```bash
sudo usermod -a -G dialout $USER
sudo chmod 666 /dev/ttyUSB0
```

### 4. Flash Heltec as RNode

Run once from any PC:

```bash
pip install rns
rnodeconf --autoinstall
# Select Heltec V3, set frequency for your region (915MHz US / 868MHz EU)
```

### 5. Deploy

```bash
cp MeshCommander.py /roms/ports/
cp MeshCommander.sh /roms/ports/
chmod +x /roms/ports/MeshCommander.sh
chmod +x /roms/ports/MeshCommander.py
```

`MeshCommander.sh`:

```bash
#!/bin/bash
export SDL_VIDEODRIVER=offscreen
export SDL_NOMOUSE=1
python3 /roms/ports/MeshCommander.py
```

---

## 🎮 Button Map (R36S / GO-Super Gamepad)

All keycodes verified via evdev on this hardware.

| Button | Keycode | Action |
|--------|---------|--------|
| L1 | 310 | Previous panel |
| R1 | 311 | Next panel |
| START | 705 | Next panel (forward only) |
| SELECT + START | 704 + 705 | Exit to EmulationStation |
| A | 304 | Confirm / add character |
| B | 305 | Delete / back |
| Y | 308 | Announce to mesh (STATUS/MESSAGES) · Save friend (PEERS) |
| X | 307 | Send message |
| D-pad ↕ | 544/545 | Scroll / navigate within panel |
| D-pad ◄► | 546/547 | Cycle transport (STATUS) · Move char cursor (COMPOSE) |
| L2 | 312 | Page up in messages |
| R2 | 313 | Page down in messages |

---

## 🛰️ Core Concepts

### Sovereign Networking

No accounts, no servers, no pairing. Each node generates a keypair on first run stored in `~/.reticulum/storage/`. Peers discover each other via LoRa announces. All messages are end-to-end encrypted automatically by Reticulum.

### Framebuffer UI

pygame on this build has no fbcon driver compiled in. MeshCommander uses `SDL_VIDEODRIVER=offscreen` and writes frames to `/dev/fb0` via a background thread, keeping the event loop free for input.

### Friend List

Press X on a peer in the PEERS panel to save them as a friend (★). Custom names persist to `~/.reticulum/friends.json` across reboots.

---

## ⚠️ Known Issues

**Powered hub required** — the Heltec ESP32-S3 V3 draws more current than the R36S OTG port provides. A bus-powered hub causes intermittent disconnects. Use a powered hub.

**ALSA audio hang** — `pygame.init()` hangs on this hardware. The app uses `pygame.display.init()` only. Never call `pygame.init()`.

**Heltec parameter mismatch on cold boot** — Reticulum retries automatically and recovers within ~10 seconds. If persistent, reset radio params manually:
```bash
rnodeconf /dev/ttyUSB0 --freq 915000000 --bw 125000 --txp 7 --sf 8 --cr 5
```

**Fresh flash** — after flashing a new OS image, re-run serial permissions and Reticulum config setup. The SD card is the entire system; no internal storage is used for boot.

---

## 📋 Notes

- Tested on dArkOSRE (Debian Trixie, kernel 4.4.189 aarch64)
- Input reads directly from `/dev/input/event2` via evdev — SDL joystick unreliable under offscreen SDL on this hardware
- Framebuffer writes to `/dev/fb0` via `FB0Writer` background thread
- Signal handler suppression applied only during `RNS.Reticulum()` init and immediately restored
- The R36S is the brain. The Heltec is just the radio pipe. Swap the Heltec for any RNode-compatible hardware and nothing in MeshCommander changes.
