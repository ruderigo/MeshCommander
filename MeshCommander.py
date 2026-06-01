#!/usr/bin/env python3
"""
MeshCommander v3 — R36S Mesh Communicator
Framebuffer UI (no X required). Dual transport: LoRa (RNode/Heltec) or TCP/Internet.

Button map:
  START        — cycle panels
  A  (btn 0)   — confirm / add char / select peer
  B  (btn 1)   — back / delete char
  Y  (btn 3)   — SEND (compose panel only)
  SELECT(btn 6)— toggle transport on STATUS panel

Panels:  STATUS → MESSAGES → PEERS → COMPOSE

Setup:
  pip install RNS pygame
  rnodeconf --autoinstall          # flash Heltec V3 as RNode (LoRa transport)

Reticulum config  ~/.reticulum/config  must contain:
  [interfaces]
    [[Heltec_LoRa]]
      type = RNodeInterface
      interface_enabled = True
      port = /dev/ttyUSB0
      frequency = 915000000
      bandwidth = 125000
      txpower = 7

    [[TCP_Internet]]
      type = TCPClientInterface
      interface_enabled = True
      target_host = reticulum.betweentheborders.com
      target_port = 4242
"""

import os, sys, time, threading, collections
import pygame
import RNS

# ── Framebuffer env ────────────────────────────────────────────────────────
# This pygame build has no fbcon driver compiled in.
# We render offscreen and flush each frame manually to /dev/fb0.
os.environ["SDL_VIDEODRIVER"]     = "offscreen"
os.environ["SDL_NOMOUSE"]         = "1"
# Ensure joystick events work without a display server
os.environ["SDL_JOYSTICK_DEVICE"] = "/dev/input/js0"
FB0_PATH = "/dev/fb0"

# ── Palette ────────────────────────────────────────────────────────────────
BG      = (10,  12,  18)
PANEL   = (18,  22,  32)
BORDER  = (40,  60,  90)
ACCENT  = (0,  200, 140)
ACCENT2 = (0,  160, 220)
SEL_BG  = (18,  45,  38)
WARN    = (220, 100,  40)
TEXT    = (210, 220, 230)
DIM     = (90,  100, 120)
GREEN   = (60,  220,  80)

# ── Layout ─────────────────────────────────────────────────────────────────
W, H      = 640, 480
TOPBAR    = 28
BOTBAR    = 24
PADX      = 10
CONTENT_Y = TOPBAR + 6

# ── Panels ─────────────────────────────────────────────────────────────────
PANEL_STATUS, PANEL_MESSAGES, PANEL_PEERS, PANEL_COMPOSE = 0, 1, 2, 3
PANEL_NAMES = ["STATUS", "MESSAGES", "PEERS", "COMPOSE"]
NUM_PANELS  = 4

# ── Compose charset ────────────────────────────────────────────────────────
CHARSET = list("abcdefghijklmnopqrstuvwxyz0123456789 .,!?-_/"
               "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
COLS    = 18

# ── Transport modes ────────────────────────────────────────────────────────
TRANSPORT_LORA = "LoRa (RNode)"
TRANSPORT_TCP  = "TCP (Internet)"
TRANSPORTS     = [TRANSPORT_LORA, TRANSPORT_TCP]


# ═══════════════════════════════════════════════════════════════════════════
class MeshNode:
    """
    Reticulum wrapper with dual-transport support.
    All RNS callbacks run in background threads; shared state uses self._lock.
    """
    APP_NAME = "meshcmd"
    ASPECT   = "msg"

    def __init__(self, transport: str):
        self.transport   = transport
        self.reticulum   = None
        self.identity    = None
        self.dest        = None
        self.peers       = {}   # hash_str → {name, dest_hash, identity, link, last_seen}
        self.inbox       = collections.deque(maxlen=80)
        self.link_state  = "INIT"
        self.has_unread  = False
        self.boot_error  = None
        self._lock       = threading.Lock()
        self._thread     = threading.Thread(target=self._start, daemon=True)
        self._thread.start()

    # ── Boot ────────────────────────────────────────────────────────────────
    def _start(self):
        try:
            # Suppress signal registration — RNS tries to register signal
            # handlers from this background thread which Python disallows.
            # Patching it out here is safe; we handle shutdown via pygame.quit.
            import signal as _sig
            _orig_signal = _sig.signal
            _sig.signal = lambda *a, **kw: None
            # RNS.Reticulum() reads ~/.reticulum/config automatically.
            # Both interfaces (Heltec_LoRa + TCP_Internet) are defined there.
            # We enable/disable the right one by patching the config at startup,
            # OR — simpler — we just let both load and filter in software.
            # The selected transport is shown in STATUS; both are active in RNS.
            self.reticulum = RNS.Reticulum()
            self.identity  = RNS.Identity()
            self.dest = RNS.Destination(
                self.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                self.APP_NAME,
                self.ASPECT,
            )
            self.dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
            self.dest.register_incoming_link_callback(self._link_established)
            RNS.Transport.register_announce_handler(self._on_announce)
            with self._lock:
                self.link_state = "LISTENING"
            # Announce with our display name as app_data
            self.dest.announce(app_data=b"R36S")
        except Exception as exc:
            err = str(exc)
            # Ignore the signal-in-thread warning — not a real failure
            if "signal" not in err.lower():
                with self._lock:
                    self.link_state = "ERROR"
                    self.boot_error = err

    # ── Incoming link (we are the server side) ──────────────────────────────
    def _link_established(self, link: RNS.Link):
        link.set_packet_callback(self._on_packet)
        link.set_resource_callback(lambda r: r.accept())
        link.set_link_closed_callback(self._link_closed)
        with self._lock:
            self.link_state = "LINKED"

    def _link_closed(self, link: RNS.Link):
        with self._lock:
            for peer in self.peers.values():
                if peer.get("link") is link:
                    peer["link"] = None
                    break
            still_active = any(
                p.get("link") and p["link"].status == RNS.Link.ACTIVE
                for p in self.peers.values()
            )
            if not still_active:
                self.link_state = "LISTENING"

    # ── Incoming packet ─────────────────────────────────────────────────────
    def _on_packet(self, message: bytes, packet: RNS.Packet):
        text = message.decode("utf-8", errors="replace")
        try:
            # remote_identity = the peer on the far end of the link handshake
            sender_hash  = RNS.prettyhexrep(packet.link.remote_identity.hash)
            sender_label = self._peer_name(sender_hash)
        except Exception:
            sender_hash  = RNS.prettyhexrep(packet.destination_hash)
            sender_label = sender_hash[:8]
        with self._lock:
            self.inbox.append((time.strftime("%H:%M"), sender_label, text, sender_hash))
            self.has_unread = True

    # ── Peer announced itself ───────────────────────────────────────────────
    def _on_announce(self, dest_hash: bytes, identity, app_data):
        h    = RNS.prettyhexrep(dest_hash)
        name = app_data.decode("utf-8", errors="replace") if app_data else h[:8]
        with self._lock:
            existing = self.peers.get(h, {})
            self.peers[h] = {
                "name":      name,
                "dest_hash": dest_hash,
                "identity":  identity,
                "link":      existing.get("link"),
                "last_seen": time.strftime("%H:%M"),
            }
            self.inbox.append((time.strftime("%H:%M"), "NET",
                               f"Peer: {name}", h))
            self.has_unread = True

    # ── Outbound send ───────────────────────────────────────────────────────
    def send_to(self, peer_hash: str, text: str):
        """
        Send text to a specific peer.
        Reuses an active Link if one exists; opens a new one via the
        established_callback pattern (async-safe) otherwise.
        """
        with self._lock:
            peer = self.peers.get(peer_hash)
        if not peer:
            return

        link = peer.get("link")

        if link and link.status == RNS.Link.ACTIVE:
            # Fast path: link already up
            self._send_on_link(link, peer_hash, text)
        else:
            # Build outbound destination from the identity we got in the announce
            try:
                peer_dest = RNS.Destination(
                    peer["identity"],
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    self.APP_NAME,
                    self.ASPECT,
                )
                # Pass callbacks so we don't call _link_established prematurely
                new_link = RNS.Link(
                    peer_dest,
                    established_callback=lambda l: self._outbound_ready(l, peer_hash, text),
                )
                with self._lock:
                    self.peers[peer_hash]["link"] = new_link
            except Exception as exc:
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "ERR",
                                       f"Link failed: {exc}", None))

    def _outbound_ready(self, link: RNS.Link, peer_hash: str, text: str):
        """Called by RNS once the outbound link handshake completes."""
        link.set_packet_callback(self._on_packet)
        link.set_resource_callback(lambda r: r.accept())
        link.set_link_closed_callback(self._link_closed)
        with self._lock:
            self.link_state = "LINKED"
            if peer_hash in self.peers:
                self.peers[peer_hash]["link"] = link
        self._send_on_link(link, peer_hash, text)

    def _send_on_link(self, link: RNS.Link, peer_hash: str, text: str):
        try:
            RNS.Packet(link, text.encode("utf-8")).send()
            peer_name = self._peer_name(peer_hash)
            with self._lock:
                self.inbox.append((time.strftime("%H:%M"), "ME",
                                   f"→{peer_name}: {text}", None))
        except Exception as exc:
            with self._lock:
                self.inbox.append((time.strftime("%H:%M"), "ERR",
                                   f"Send failed: {exc}", None))

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _peer_name(self, hash_str: str) -> str:
        p = self.peers.get(hash_str)
        return p["name"] if p else hash_str[:8]

    def clear_unread(self):
        with self._lock:
            self.has_unread = False

    def peer_list(self):
        with self._lock:
            return sorted(self.peers.items(),
                          key=lambda kv: kv[1].get("last_seen", ""), reverse=True)

    def iface_summary(self):
        if not self.reticulum:
            return []
        out = []
        for iface in RNS.Transport.interfaces:
            name = getattr(iface, "name", str(iface))
            # Skip the internal shared-instance interface RNS creates automatically
            if name.lower() == "reticulum":
                continue
            ok = getattr(iface, "online", False)
            out.append((name, "UP" if ok else "DOWN"))
        return out

    @property
    def short_hash(self) -> str:
        try:
            return RNS.prettyhexrep(self.dest.hash)[:12]
        except Exception:
            return "------"


# ═══════════════════════════════════════════════════════════════════════════
class Renderer:
    def __init__(self, screen, fonts):
        self.screen = screen
        self.F      = fonts

    # ── Primitives ──────────────────────────────────────────────────────────
    def filled_rect(self, x, y, w, h, color, r=4):
        pygame.draw.rect(self.screen, color, (x, y, w, h), border_radius=r)

    def outlined_rect(self, x, y, w, h, fill, border, r=4, bw=1):
        self.filled_rect(x, y, w, h, fill, r)
        pygame.draw.rect(self.screen, border, (x, y, w, h), bw, border_radius=r)

    def txt(self, s, x, y, color=TEXT, font="md", anchor="topleft") -> int:
        surf = self.F[font].render(str(s), True, color)
        rect = surf.get_rect(**{anchor: (x, y)})
        self.screen.blit(surf, rect)
        return rect.width

    def hline(self, y, color=BORDER):
        pygame.draw.line(self.screen, color, (PADX, y), (W - PADX, y))

    # ── Top bar ─────────────────────────────────────────────────────────────
    def draw_topbar(self, node, active_panel):
        self.filled_rect(0, 0, W, TOPBAR, PANEL)
        pygame.draw.line(self.screen, BORDER, (0, TOPBAR - 1), (W, TOPBAR - 1))
        self.txt("MESH", PADX, 6, ACCENT, "md")
        self.txt("CMD",  PADX + 46, 6, TEXT, "md")

        tab_w   = 80
        start_x = (W - tab_w * NUM_PANELS) // 2
        for i, name in enumerate(PANEL_NAMES):
            tx = start_x + i * tab_w
            if i == active_panel:
                self.filled_rect(tx - 2, 3, tab_w, TOPBAR - 6, ACCENT, r=3)
                self.txt(name, tx + tab_w // 2, 7, BG, "sm", anchor="midtop")
            else:
                self.txt(name, tx + tab_w // 2, 7, DIM, "sm", anchor="midtop")
            # Unread dot
            if i == PANEL_MESSAGES and node.has_unread and i != active_panel:
                pygame.draw.circle(self.screen, WARN, (tx + tab_w - 8, 8), 4)

        self.txt(node.short_hash, W - PADX, 7, DIM, "sm", anchor="topright")

    # ── Bottom hint bar ─────────────────────────────────────────────────────
    def draw_botbar(self, hints):
        y = H - BOTBAR
        pygame.draw.line(self.screen, BORDER, (0, y), (W, y))
        self.filled_rect(0, y + 1, W, BOTBAR, PANEL)
        x = PADX
        for btn, label in hints:
            x += self.txt(f"[{btn}]", x, y + 4, ACCENT, "sm") + 2
            x += self.txt(f"{label} ", x, y + 4, DIM, "sm") + 6

    # ── STATUS panel ────────────────────────────────────────────────────────
    def draw_status(self, node, transport_idx):
        y = CONTENT_Y

        # Link state
        state = node.link_state
        sc    = ACCENT if state in ("LISTENING", "LINKED") else (WARN if state == "ERROR" else DIM)
        self.outlined_rect(PADX, y, W - PADX * 2, 36, PANEL, sc, r=5)
        self.txt("LINK",  PADX + 10, y + 4,  DIM, "sm")
        self.txt(state,   PADX + 10, y + 18, sc,  "md")
        if node.boot_error:
            self.txt(node.boot_error[:55], PADX + 80, y + 18, WARN, "sm")
        y += 44

        # ── Transport selector ──────────────────────────────────────────────
        # Cross-reference selected transport with real interface online status
        ifaces = {name: status for name, status in node.iface_summary()}
        self.txt("TRANSPORT", PADX, y, DIM, "sm")
        y += 16
        for i, name in enumerate(TRANSPORTS):
            selected = (i == transport_idx)
            # Determine real online status from RNS interface list
            if "RNode" in name or "LoRa" in name:
                iface_key = next((k for k in ifaces if "Heltec" in k or "RNode" in k or "LoRa" in k), None)
            else:
                iface_key = next((k for k in ifaces if "TCP" in k or "Internet" in k), None)
            online = ifaces.get(iface_key) == "UP" if iface_key else False
            fill   = SEL_BG if selected else PANEL
            border = ACCENT if selected else BORDER
            self.outlined_rect(PADX, y, W - PADX * 2, 30, fill, border, r=4)
            dot_c  = ACCENT if online else WARN
            pygame.draw.circle(self.screen, dot_c, (PADX + 14, y + 15), 5)
            self.txt(name, PADX + 28, y + 8, ACCENT if selected else TEXT, "sm")
            status_txt = ("UP" if online else "DOWN") + (" ◄ selected" if selected else "")
            self.txt(status_txt, W - PADX - 8, y + 8,
                     ACCENT if online else WARN, "sm", anchor="topright")
            y += 34

        # Interfaces
        y += 4
        self.hline(y); y += 8
        ifaces = node.iface_summary()
        self.txt(f"INTERFACES ({len(ifaces)})", PADX, y, DIM, "sm")
        y += 16
        if not ifaces:
            self.txt("Starting Reticulum…", PADX + 8, y, DIM, "sm")
        else:
            for iname, status in ifaces:
                ok = status == "UP"
                self.outlined_rect(PADX, y, W - PADX * 2, 26, PANEL, BORDER, r=3)
                pygame.draw.circle(self.screen, ACCENT if ok else WARN,
                                   (PADX + 12, y + 13), 4)
                self.txt(iname,  PADX + 24,   y + 6, TEXT,               "sm")
                self.txt(status, W - PADX - 8, y + 6, ACCENT if ok else WARN,
                         "sm", anchor="topright")
                y += 30

        # Peers summary
        peers = node.peer_list()
        if peers:
            y += 4
            self.hline(y); y += 8
            self.txt(f"{len(peers)} peer(s) known", PADX, y, DIM, "sm")

    # ── MESSAGES panel ──────────────────────────────────────────────────────
    def draw_messages(self, node, scroll):
        msgs  = list(node.inbox)
        end   = max(len(msgs) - scroll, 0)
        start = max(end - 9, 0)
        y     = CONTENT_Y

        if not msgs:
            self.txt("No messages yet", W // 2, H // 2, DIM, "md", anchor="center")
            return

        for ts, sender, body, _h in msgs[start:end]:
            is_me  = sender == "ME"
            is_sys = sender in ("NET", "ERR")
            bg     = SEL_BG if is_me else PANEL
            bc     = ACCENT if is_me else (DIM if is_sys else BORDER)
            hc     = ACCENT if is_me else (DIM if is_sys else ACCENT2)

            words = body.split()
            lines, cur = [], ""
            for w in words:
                if len(cur) + len(w) + 1 > 54:
                    lines.append(cur); cur = w
                else:
                    cur = (cur + " " + w).strip()
            if cur:
                lines.append(cur)

            row_h = 16 + len(lines) * 17
            if y + row_h > H - BOTBAR - 4:
                break

            self.outlined_rect(PADX, y, W - PADX * 2, row_h, bg, bc, r=5)
            self.txt(f"{sender}  {ts}", PADX + 8, y + 4, hc, "sm")
            for i, ln in enumerate(lines):
                self.txt(ln, PADX + 8, y + 16 + i * 17, TEXT, "sm")
            y += row_h + 4

        if scroll > 0:
            self.txt(f"↑ {scroll} older", W - PADX, CONTENT_Y, DIM, "sm", anchor="topright")

    # ── PEERS panel ─────────────────────────────────────────────────────────
    def draw_peers(self, node, cursor, selected_hash):
        y     = CONTENT_Y
        peers = node.peer_list()

        self.txt("SELECT PEER", PADX, y, DIM, "sm"); y += 20

        if not peers:
            self.txt("Listening for announces…", W // 2, H // 2 - 10,
                     DIM, "md", anchor="center")
            return

        vis_start = max(0, cursor - 5)
        for idx, (h, info) in enumerate(peers[vis_start: vis_start + 8]):
            real_idx  = idx + vis_start
            is_cursor = real_idx == cursor
            is_sel    = h == selected_hash
            fill      = SEL_BG if is_cursor else PANEL
            border    = ACCENT if is_cursor else (ACCENT2 if is_sel else BORDER)

            self.outlined_rect(PADX, y, W - PADX * 2, 36, fill, border, r=5)

            if is_sel:
                self.txt("✓", PADX + 5, y + 10, ACCENT, "sm")
            if is_cursor:
                self.txt("▶", PADX + 18, y + 10, ACCENT, "sm")

            nc = ACCENT if is_cursor else (ACCENT2 if is_sel else TEXT)
            self.txt(info["name"],  PADX + 34, y + 4,  nc,  "md")
            self.txt(h[:16],        PADX + 34, y + 20, DIM, "sm")
            self.txt(info.get("last_seen", ""), W - PADX - 8, y + 10,
                     DIM, "sm", anchor="topright")

            # Link status badge
            lnk = info.get("link")
            if lnk:
                lc = GREEN if lnk.status == RNS.Link.ACTIVE else WARN
                self.txt("●", W - PADX - 28, y + 10, lc, "sm", anchor="topright")
            y += 40

        if selected_hash:
            sname = node.peers.get(selected_hash, {}).get("name", "?")
            self.txt(f"Target locked: {sname}",
                     W // 2, H - BOTBAR - 18, ACCENT, "sm", anchor="midtop")

    # ── COMPOSE panel ───────────────────────────────────────────────────────
    def draw_compose(self, draft, cursor_pos, peer_name, blink):
        y = CONTENT_Y

        tc = ACCENT if peer_name else WARN
        tt = f"TO: {peer_name}" if peer_name else "TO: none — select a peer first"
        self.txt(tt, PADX, y, tc, "sm"); y += 18

        # Draft box
        self.outlined_rect(PADX, y, W - PADX * 2, 76, PANEL, ACCENT, r=6)
        display  = draft + ("_" if blink else " ")
        chars_ln = 38
        lines    = [display[i:i + chars_ln] for i in range(0, max(len(display), 1), chars_ln)]
        for i, ln in enumerate(lines[:4]):
            self.txt(ln, PADX + 10, y + 8 + i * 17, TEXT, "mono")
        y += 84

        # Char picker — show a 4-row window around the cursor row
        self.txt("PICK:  ◄► char   ▲▼ row   [A] add   [B] del   [Y] SEND",
                 PADX, y, DIM, "sm"); y += 18

        cur_row   = cursor_pos // COLS
        start_row = max(0, cur_row - 1)
        char_w    = (W - PADX * 2) // COLS

        for r in range(start_row, start_row + 4):
            for c in range(COLS):
                i = r * COLS + c
                if i >= len(CHARSET):
                    break
                ch = CHARSET[i]
                cx = PADX + c * char_w
                cy = y + (r - start_row) * 26
                if cy > H - BOTBAR - 28:
                    break
                if i == cursor_pos:
                    self.filled_rect(cx, cy, char_w - 1, 24, ACCENT, r=3)
                    self.txt(ch, cx + char_w // 2, cy + 4, BG, "mono", anchor="midtop")
                else:
                    self.txt(ch, cx + char_w // 2, cy + 4, TEXT, "mono", anchor="midtop")


# ═══════════════════════════════════════════════════════════════════════════
def load_fonts():
    pygame.font.init()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ttf = next(
        (p for p in [
            os.path.join(script_dir, "terminus.ttf"),
            os.path.join(script_dir, "TerminusTTF.ttf"),
            os.path.join(script_dir, "font.ttf"),
        ] if os.path.exists(p)),
        None,
    )
    def load(size):
        if ttf:
            try: return pygame.font.Font(ttf, size)
            except Exception: pass
        return pygame.font.SysFont("monospace", size)
    return {"sm": load(13), "md": load(15), "lg": load(20), "mono": load(14)}


# ═══════════════════════════════════════════════════════════════════════════
def main():
    pygame.init()
    pygame.joystick.init()

    # Offscreen surface — we blit to /dev/fb0 manually each frame
    screen = pygame.display.set_mode((W, H), 0)
    pygame.display.set_caption("MeshCommander")
    pygame.mouse.set_visible(False)

    # Open framebuffer for direct writing
    try:
        fb0 = open(FB0_PATH, "wb")
    except Exception as e:
        print(f"Cannot open {FB0_PATH}: {e}")
        sys.exit(1)

    fonts    = load_fonts()
    renderer = Renderer(screen, fonts)

    # Start with LoRa by default; SELECT toggles on STATUS panel
    transport_idx = 0
    node = MeshNode(TRANSPORTS[transport_idx])

    # Joystick — retry loop so it works when ES launches before gamepad is ready
    joy = None
    for _attempt in range(5):
        pygame.joystick.quit()
        pygame.joystick.init()
        if pygame.joystick.get_count() > 0:
            joy = pygame.joystick.Joystick(0)
            joy.init()
            break
        time.sleep(0.5)

    # ── UI state ─────────────────────────────────────────────────────────────
    active_panel       = PANEL_STATUS
    msg_scroll         = 0
    peer_cursor        = 0
    selected_peer_hash = None
    compose_draft      = ""
    compose_cursor     = 0
    blink              = True
    last_blink         = time.time()

    HINTS = {
        PANEL_STATUS:   [("SEL", "transport"), ("START", "next")],
        PANEL_MESSAGES: [("↕", "scroll"), ("A", "compose"), ("START", "next")],
        PANEL_PEERS:    [("↕", "move"), ("A", "select"), ("START", "next")],
        PANEL_COMPOSE:  [("◄►▲▼", "char"), ("A", "add"), ("B", "del"), ("Y", "SEND")],
    }

    clock   = pygame.time.Clock()
    running = True

    while running:
        now = time.time()
        if now - last_blink > 0.5:
            blink      = not blink
            last_blink = now

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            # ── Keyboard (for desktop testing without a gamepad) ─────────────
            elif event.type == pygame.KEYDOWN:
                k = event.key
                if k == pygame.K_q:
                    running = False
                elif k == pygame.K_TAB:
                    active_panel = (active_panel + 1) % NUM_PANELS
                    if active_panel == PANEL_MESSAGES:
                        node.clear_unread()
                elif k == pygame.K_s and active_panel == PANEL_STATUS:
                    transport_idx = (transport_idx + 1) % len(TRANSPORTS)
                    node.transport = TRANSPORTS[transport_idx]
                elif active_panel == PANEL_MESSAGES:
                    if k == pygame.K_UP:
                        msg_scroll = min(msg_scroll + 1, max(0, len(node.inbox) - 5))
                    elif k == pygame.K_DOWN:
                        msg_scroll = max(0, msg_scroll - 1)
                elif active_panel == PANEL_PEERS:
                    peers = node.peer_list()
                    if k == pygame.K_UP:
                        peer_cursor = max(0, peer_cursor - 1)
                    elif k == pygame.K_DOWN:
                        peer_cursor = min(max(len(peers) - 1, 0), peer_cursor + 1)
                    elif k == pygame.K_RETURN and peers:
                        selected_peer_hash = peers[peer_cursor][0]
                        active_panel       = PANEL_COMPOSE
                elif active_panel == PANEL_COMPOSE:
                    if k == pygame.K_LEFT:
                        compose_cursor = max(0, compose_cursor - 1)
                    elif k == pygame.K_RIGHT:
                        compose_cursor = min(len(CHARSET) - 1, compose_cursor + 1)
                    elif k == pygame.K_UP:
                        compose_cursor = max(0, compose_cursor - COLS)
                    elif k == pygame.K_DOWN:
                        compose_cursor = min(len(CHARSET) - 1, compose_cursor + COLS)
                    elif k == pygame.K_RETURN:
                        compose_draft += CHARSET[compose_cursor]
                    elif k == pygame.K_BACKSPACE:
                        compose_draft = compose_draft[:-1]
                    elif k == pygame.K_y and compose_draft.strip() and selected_peer_hash:
                        node.send_to(selected_peer_hash, compose_draft.strip())
                        compose_draft = ""
                        active_panel  = PANEL_MESSAGES
                        msg_scroll    = 0
                        node.clear_unread()

            # ── Gamepad buttons ──────────────────────────────────────────────
            # GO-Super Gamepad button map (verified from event log):
            #   0=A  1=B  2=X  3=Y
            #   8=SELECT  9=START
            #   12=DPAD_UP  13=DPAD_DOWN  14=DPAD_LEFT  15=DPAD_RIGHT
            elif event.type == pygame.JOYBUTTONDOWN and joy:
                btn = event.button

                if btn == 8:   # SELECT — toggle transport (STATUS only)
                    if active_panel == PANEL_STATUS:
                        transport_idx  = (transport_idx + 1) % len(TRANSPORTS)
                        node.transport = TRANSPORTS[transport_idx]

                elif btn == 9:   # START — cycle panels
                    active_panel = (active_panel + 1) % NUM_PANELS
                    if active_panel == PANEL_MESSAGES:
                        node.clear_unread()

                elif btn == 0:   # A — confirm/select/add char
                    if active_panel == PANEL_MESSAGES:
                        active_panel = PANEL_COMPOSE
                    elif active_panel == PANEL_PEERS:
                        peers = node.peer_list()
                        if peers:
                            selected_peer_hash = peers[peer_cursor][0]
                            active_panel       = PANEL_COMPOSE
                    elif active_panel == PANEL_COMPOSE:
                        compose_draft += CHARSET[compose_cursor]

                elif btn == 1:   # B — delete / back
                    if active_panel == PANEL_COMPOSE:
                        if compose_draft:
                            compose_draft = compose_draft[:-1]
                        else:
                            active_panel = PANEL_MESSAGES

                elif btn == 3:   # Y — SEND
                    if active_panel == PANEL_COMPOSE and compose_draft.strip():
                        if selected_peer_hash:
                            node.send_to(selected_peer_hash, compose_draft.strip())
                        compose_draft = ""
                        active_panel  = PANEL_MESSAGES
                        msg_scroll    = 0
                        node.clear_unread()

                # ── D-pad (fires as buttons on GO-Super Gamepad, not hat) ───
                elif btn == 12:  # D-pad UP
                    if active_panel == PANEL_MESSAGES:
                        msg_scroll = min(msg_scroll + 1, max(0, len(node.inbox) - 5))
                    elif active_panel == PANEL_PEERS:
                        peer_cursor = max(0, peer_cursor - 1)
                    elif active_panel == PANEL_COMPOSE:
                        compose_cursor = max(0, compose_cursor - COLS)

                elif btn == 13:  # D-pad DOWN
                    if active_panel == PANEL_MESSAGES:
                        msg_scroll = max(0, msg_scroll - 1)
                    elif active_panel == PANEL_PEERS:
                        peers = node.peer_list()
                        peer_cursor = min(max(len(peers) - 1, 0), peer_cursor + 1)
                    elif active_panel == PANEL_COMPOSE:
                        compose_cursor = min(len(CHARSET) - 1, compose_cursor + COLS)

                elif btn == 14:  # D-pad LEFT
                    if active_panel == PANEL_COMPOSE:
                        compose_cursor = max(0, compose_cursor - 1)

                elif btn == 15:  # D-pad RIGHT
                    if active_panel == PANEL_COMPOSE:
                        compose_cursor = min(len(CHARSET) - 1, compose_cursor + 1)

        # ── Draw ─────────────────────────────────────────────────────────────
        screen.fill(BG)
        renderer.draw_topbar(node, active_panel)

        if active_panel == PANEL_STATUS:
            renderer.draw_status(node, transport_idx)
        elif active_panel == PANEL_MESSAGES:
            renderer.draw_messages(node, msg_scroll)
        elif active_panel == PANEL_PEERS:
            renderer.draw_peers(node, peer_cursor, selected_peer_hash)
        elif active_panel == PANEL_COMPOSE:
            peer_name = None
            if selected_peer_hash:
                with node._lock:
                    peer_name = node.peers.get(selected_peer_hash, {}).get("name")
            renderer.draw_compose(compose_draft, compose_cursor, peer_name, blink)

        renderer.draw_botbar(HINTS[active_panel])

        # Flush offscreen surface to /dev/fb0
        # 32-bit framebuffer expects BGRA; pygame surface is RGB — convert via RGBX
        # If colours look swapped (red/blue inverted) change "RGBX" to "BGRX"
        fb0.seek(0)
        fb0.write(pygame.image.tobytes(screen, "RGBX"))
        clock.tick(20)

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
