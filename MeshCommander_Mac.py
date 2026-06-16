#!/usr/bin/env python3
"""
MeshCommander — macOS Companion
Runs as a normal windowed pygame app on macOS.
Functionally identical to the R36S version; hardware-specific layers replaced:
  • FB0Writer    → standard pygame display (no framebuffer)
  • DirectInput  → keyboard input via pygame events
  • /dev/ttyUSB0 → auto-detected macOS serial port (cu.usbserial-* / cu.SLAB_*)

Keyboard map (mirrors R36S button layout):
  Tab / ]        — next panel          [  / Shift-Tab  — previous panel
  Page Up        — page up (messages)  Page Down      — page down (messages)
  Arrow keys     — navigate / scroll / char cursor
  F1             — announce to mesh (Status / Messages)
  F2             — save peer as friend (Peers)
  F3             — select peer / open Compose (Peers / Messages)
  F4             — add selected character (Compose)
  F5             — send message (Compose)
  Backspace      — delete last char / back to Messages
  Escape         — quit

Serial port: set RNODE_PORT env var to override auto-detection.
  export RNODE_PORT=/dev/cu.usbserial-0001

Usage:
  pip install RNS pygame numpy
  python3 MeshCommander_Mac.py
"""

import os, sys, time, threading, collections, glob
import pygame
import RNS

# ── Serial port auto-detection ─────────────────────────────────────────────
def find_rnode_port() -> str | None:
    """Return the first likely RNode serial port on macOS, or None."""
    override = os.environ.get("RNODE_PORT")
    if override:
        return override
    patterns = [
        "/dev/cu.usbserial-*",
        "/dev/cu.SLAB_USBtoUART*",
        "/dev/cu.wchusbserial*",
        "/dev/cu.usbmodem*",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None

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
W, H      = 800, 600          # larger window on a real display
TOPBAR    = 32
BOTBAR    = 28
PADX      = 12
CONTENT_Y = TOPBAR + 8

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
class _AnnounceHandler:
    """
    Top-level class so the instance is not garbage-collected.
    RNS may hold only a weak reference internally; storing it on MeshNode
    (self._announce_handler) keeps it alive for the lifetime of the node.
    """
    aspect_filter = "meshcmd.msg"

    def __init__(self, cb):
        self.cb = cb

    def received_announce(self, destination_hash, announced_identity, app_data):
        self.cb(destination_hash, announced_identity, app_data)


# ═══════════════════════════════════════════════════════════════════════════
class MeshNode:
    """
    Reticulum wrapper — identical to R36S version.
    All RNS callbacks run in background threads; shared state protected by _lock.
    """
    APP_NAME = "meshcmd"
    ASPECT   = "msg"

    def __init__(self, transport: str):
        self.transport      = transport
        self.reticulum      = None
        self.identity       = None
        self.dest           = None
        self.peers          = {}   # hash_str → {name, dest_hash, identity, link, last_seen}
        self.friends        = {}   # hash_str → custom name (persisted)
        self.inbox          = collections.deque(maxlen=80)
        self.link_state     = "INIT"
        self.has_unread     = False
        self.boot_error     = None
        self._lock          = threading.Lock()
        self._friends_path  = os.path.expanduser("~/.reticulum/friends.json")
        self._load_friends()
        self._thread        = threading.Thread(target=self._start, daemon=True)
        self._thread.start()

    # ── Friends persistence ──────────────────────────────────────────────
    def _load_friends(self):
        try:
            import json
            with open(self._friends_path, 'r') as f:
                self.friends = json.load(f)
        except Exception:
            self.friends = {}

    def save_friend(self, hash_str: str, name: str):
        import json
        self.friends[hash_str] = name
        with self._lock:
            if hash_str in self.peers:
                self.peers[hash_str]["name"] = name
        try:
            with open(self._friends_path, 'w') as f:
                json.dump(self.friends, f)
        except Exception:
            pass

    def is_friend(self, hash_str: str) -> bool:
        return hash_str in self.friends

    # ── Boot ────────────────────────────────────────────────────────────
    def _start(self):
        try:
            import signal as _sig
            _orig_signal   = _sig.signal
            _sig.signal    = lambda *a, **kw: None
            self.reticulum = RNS.Reticulum()
            _sig.signal    = _orig_signal

            # Persist identity so our hash stays the same across restarts
            id_path = os.path.expanduser("~/.reticulum/meshcmd_identity")
            if os.path.exists(id_path):
                self.identity = RNS.Identity.from_file(id_path)
            else:
                self.identity = RNS.Identity()
                self.identity.to_file(id_path)
            self.dest = RNS.Destination(
                self.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                self.APP_NAME,
                self.ASPECT,
            )
            self.dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
            self.dest.set_link_established_callback(self._link_established)

            self._announce_handler = _AnnounceHandler(self._on_announce)
            RNS.Transport.register_announce_handler(self._announce_handler)
            with self._lock:
                self.link_state = "LISTENING"
            self.dest.announce(app_data=b"Mac")
        except Exception as exc:
            err = str(exc)
            if "signal" not in err.lower():
                with self._lock:
                    self.link_state = "ERROR"
                    self.boot_error = err

    # ── Incoming link ────────────────────────────────────────────────────
    def _link_established(self, link: RNS.Link):
        link.set_packet_callback(self._on_packet)
        link.set_resource_callback(lambda r: r.accept())
        link.set_link_closed_callback(self._link_closed)
        link.identify(self.identity)   # tell the remote who we are
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

    # ── Incoming packet ──────────────────────────────────────────────────
    def _on_packet(self, message: bytes, packet: RNS.Packet):
        text = message.decode("utf-8", errors="replace")
        try:
            remote_id    = packet.link.remote_identity
            sender_hash  = RNS.prettyhexrep(remote_id.hash) if remote_id else None
            sender_label = self._peer_name(sender_hash) if sender_hash else "unknown"
        except Exception:
            sender_hash  = None
            sender_label = "unknown"
        with self._lock:
            self.inbox.append((time.strftime("%H:%M"), sender_label, text, sender_hash))
            self.has_unread = True

    # ── Peer announced ───────────────────────────────────────────────────
    def _on_announce(self, dest_hash: bytes, identity, app_data):
        h    = RNS.prettyhexrep(dest_hash)
        name = app_data.decode("utf-8", errors="replace") if app_data else h[:8]
        name = self.friends.get(h, name)
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

    # ── Announce ─────────────────────────────────────────────────────────
    def announce(self):
        def _do():
            with self._lock:
                dest = self.dest
            if dest is None:
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "ERR",
                                       "RNS not ready", None))
                    self.has_unread = True
                return
            try:
                dest.announce(app_data=b"Mac")
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "NET",
                                       "Announced to mesh", None))
                    self.has_unread = True
            except Exception as exc:
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "ERR",
                                       f"Announce failed: {exc}", None))
                    self.has_unread = True
        threading.Thread(target=_do, daemon=True).start()

    # ── Outbound send ────────────────────────────────────────────────────
    def send_to(self, peer_hash: str, text: str):
        with self._lock:
            peer = self.peers.get(peer_hash)
        if not peer:
            return
        link = peer.get("link")
        if link and link.status == RNS.Link.ACTIVE:
            self._send_on_link(link, peer_hash, text)
        else:
            try:
                peer_dest = RNS.Destination(
                    peer["identity"],
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    self.APP_NAME,
                    self.ASPECT,
                )
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
        link.set_packet_callback(self._on_packet)
        link.set_resource_callback(lambda r: r.accept())
        link.set_link_closed_callback(self._link_closed)
        link.identify(self.identity)   # tell the remote who we are
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

    # ── Helpers ──────────────────────────────────────────────────────────
    def _peer_name(self, hash_str: str) -> str:
        with self._lock:
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

    def filled_rect(self, x, y, w, h, color, r=4):
        pygame.draw.rect(self.screen, color, (x, y, w, h), border_radius=r)

    def outlined_rect(self, x, y, w, h, fill, border, r=4, bw=1):
        self.filled_rect(x, y, w, h, fill, r)
        pygame.draw.rect(self.screen, border, (x, y, w, h), bw, border_radius=r)

    def txt(self, s, x, y, color=TEXT, font="md", anchor="topleft") -> int:
        surf = self.F[font].render(str(s).replace('\x00', ''), True, color)
        rect = surf.get_rect(**{anchor: (x, y)})
        self.screen.blit(surf, rect)
        return rect.width

    def hline(self, y, color=BORDER):
        pygame.draw.line(self.screen, color, (PADX, y), (W - PADX, y))

    # ── Top bar ──────────────────────────────────────────────────────────
    def draw_topbar(self, node, active_panel, port_label):
        self.filled_rect(0, 0, W, TOPBAR, PANEL)
        pygame.draw.line(self.screen, BORDER, (0, TOPBAR - 1), (W, TOPBAR - 1))
        self.txt("MESH", PADX, 8, ACCENT, "md")
        self.txt("CMD",  PADX + 50, 8, TEXT, "md")

        tab_w   = 90
        start_x = (W - tab_w * NUM_PANELS) // 2
        for i, name in enumerate(PANEL_NAMES):
            tx = start_x + i * tab_w
            if i == active_panel:
                self.filled_rect(tx - 2, 4, tab_w, TOPBAR - 8, ACCENT, r=3)
                self.txt(name, tx + tab_w // 2, 9, BG, "sm", anchor="midtop")
            else:
                self.txt(name, tx + tab_w // 2, 9, DIM, "sm", anchor="midtop")
            if i == PANEL_MESSAGES and node.has_unread and i != active_panel:
                pygame.draw.circle(self.screen, WARN, (tx + tab_w - 8, 9), 4)

        # Right side: port label + node hash
        self.txt(port_label, W - PADX, 4, DIM, "sm", anchor="topright")
        self.txt(node.short_hash, W - PADX, 18, ACCENT2, "sm", anchor="topright")

    # ── Bottom hint bar ──────────────────────────────────────────────────
    def draw_botbar(self, hints):
        y = H - BOTBAR
        pygame.draw.line(self.screen, BORDER, (0, y), (W, y))
        self.filled_rect(0, y + 1, W, BOTBAR, PANEL)
        x = PADX
        for btn, label in hints:
            x += self.txt(f"[{btn}]", x, y + 6, ACCENT, "sm") + 2
            x += self.txt(f"{label} ", x, y + 6, DIM, "sm") + 8

    # ── STATUS panel ─────────────────────────────────────────────────────
    def draw_status(self, node, transport_idx):
        y = CONTENT_Y

        state = node.link_state
        sc    = ACCENT if state in ("LISTENING", "LINKED") else (WARN if state == "ERROR" else DIM)
        self.outlined_rect(PADX, y, W - PADX * 2, 40, PANEL, sc, r=5)
        self.txt("LINK",  PADX + 10, y + 4,  DIM,  "sm")
        self.txt(state,   PADX + 10, y + 20, sc,   "md")
        if node.boot_error:
            self.txt(node.boot_error[:70], PADX + 90, y + 20, WARN, "sm")
        y += 50

        ifaces_list = node.iface_summary()
        ifaces = {name: status for name, status in ifaces_list}

        self.txt("TRANSPORT", PADX, y, DIM, "sm"); y += 18
        for i, name in enumerate(TRANSPORTS):
            selected = (i == transport_idx)
            if "RNode" in name or "LoRa" in name:
                iface_key = next((k for k in ifaces if any(x in k for x in ("Heltec","RNode","LoRa","USB"))), None)
            else:
                iface_key = next((k for k in ifaces if any(x in k for x in ("TCP","Internet"))), None)
            online = ifaces.get(iface_key) == "UP" if iface_key else False
            fill   = SEL_BG if selected else PANEL
            border = ACCENT if selected else BORDER
            self.outlined_rect(PADX, y, W - PADX * 2, 32, fill, border, r=4)
            dot_c  = ACCENT if online else WARN
            pygame.draw.circle(self.screen, dot_c, (PADX + 16, y + 16), 5)
            self.txt(name, PADX + 32, y + 9, ACCENT if selected else TEXT, "sm")
            status_txt = ("UP" if online else "DOWN") + (" ◄ selected" if selected else "")
            self.txt(status_txt, W - PADX - 8, y + 9,
                     ACCENT if online else WARN, "sm", anchor="topright")
            y += 36

        y += 6
        self.hline(y); y += 10
        self.txt(f"INTERFACES ({len(ifaces_list)})", PADX, y, DIM, "sm"); y += 18
        if not ifaces_list:
            self.txt("Starting Reticulum…", PADX + 8, y, DIM, "sm")
        else:
            for iname, status in ifaces_list:
                ok = status == "UP"
                self.outlined_rect(PADX, y, W - PADX * 2, 28, PANEL, BORDER, r=3)
                pygame.draw.circle(self.screen, ACCENT if ok else WARN,
                                   (PADX + 14, y + 14), 4)
                self.txt(iname,  PADX + 28,    y + 7, TEXT,               "sm")
                self.txt(status, W - PADX - 8, y + 7, ACCENT if ok else WARN,
                         "sm", anchor="topright")
                y += 32

        peers = node.peer_list()
        if peers:
            y += 6
            self.hline(y); y += 10
            self.txt(f"{len(peers)} peer(s) known", PADX, y, DIM, "sm")

    # ── MESSAGES panel ───────────────────────────────────────────────────
    def draw_messages(self, node, scroll):
        msgs  = list(node.inbox)
        end   = max(len(msgs) - scroll, 0)
        start = max(end - 11, 0)
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
                if len(cur) + len(w) + 1 > 66:
                    lines.append(cur); cur = w
                else:
                    cur = (cur + " " + w).strip()
            if cur:
                lines.append(cur)

            row_h = 18 + len(lines) * 18
            if y + row_h > H - BOTBAR - 4:
                break

            self.outlined_rect(PADX, y, W - PADX * 2, row_h, bg, bc, r=5)
            self.txt(f"{sender}  {ts}", PADX + 8, y + 4, hc, "sm")
            for i, ln in enumerate(lines):
                self.txt(ln, PADX + 8, y + 18 + i * 18, TEXT, "sm")
            y += row_h + 4

        if scroll > 0:
            self.txt(f"↑ {scroll} older", W - PADX, CONTENT_Y, DIM, "sm", anchor="topright")

    # ── PEERS panel ──────────────────────────────────────────────────────
    def draw_peers(self, node, cursor, selected_hash):
        y     = CONTENT_Y
        peers = node.peer_list()

        self.txt("SELECT PEER  [x] save as friend", PADX, y, DIM, "sm"); y += 22

        if not peers:
            self.txt("Listening for announces…", W // 2, H // 2 - 10,
                     DIM, "md", anchor="center")
            self.txt("Press [x] on STATUS to announce yourself",
                     W // 2, H // 2 + 16, DIM, "sm", anchor="center")
            return

        vis_start = max(0, cursor - 5)
        for idx, (h, info) in enumerate(peers[vis_start: vis_start + 9]):
            real_idx  = idx + vis_start
            is_cursor = real_idx == cursor
            is_sel    = h == selected_hash
            is_friend = node.is_friend(h)
            fill      = SEL_BG if is_cursor else PANEL
            border    = ACCENT if is_cursor else (ACCENT2 if is_sel else BORDER)

            self.outlined_rect(PADX, y, W - PADX * 2, 38, fill, border, r=5)

            if is_friend:
                self.txt("★", PADX + 5,  y + 11, ACCENT,  "sm")
            if is_sel:
                self.txt("✓", PADX + 20, y + 11, ACCENT,  "sm")
            if is_cursor:
                self.txt("▶", PADX + 35, y + 11, ACCENT,  "sm")

            nc = ACCENT if is_cursor else (ACCENT2 if is_sel else TEXT)
            self.txt(info["name"],  PADX + 52, y + 4,  nc,  "md")
            self.txt(h[:16],        PADX + 52, y + 22, DIM, "sm")
            self.txt(info.get("last_seen", ""), W - PADX - 8, y + 12,
                     DIM, "sm", anchor="topright")

            lnk = info.get("link")
            if lnk:
                lc = GREEN if lnk.status == RNS.Link.ACTIVE else WARN
                self.txt("●", W - PADX - 30, y + 12, lc, "sm", anchor="topright")
            y += 42

        if selected_hash:
            sname = node.peers.get(selected_hash, {}).get("name", "?")
            self.txt(f"Target: {sname}",
                     W // 2, H - BOTBAR - 20, ACCENT, "sm", anchor="midtop")

    # ── COMPOSE panel ────────────────────────────────────────────────────
    def draw_compose(self, draft, peer_name, blink):
        y = CONTENT_Y

        tc = ACCENT if peer_name else WARN
        tt = f"TO: {peer_name}" if peer_name else "TO: none — go to PEERS first"
        self.txt(tt, PADX, y, tc, "sm"); y += 24

        # Text input box
        self.outlined_rect(PADX, y, W - PADX * 2, 100, PANEL, ACCENT, r=6)
        self.txt("MESSAGE", PADX + 10, y + 6, DIM, "sm")
        display  = draft + ("█" if blink else " ")
        chars_ln = 60
        lines    = [display[i:i + chars_ln] for i in range(0, max(len(display), 1), chars_ln)]
        for i, ln in enumerate(lines[:4]):
            self.txt(ln, PADX + 10, y + 22 + i * 18, TEXT, "mono")
        y += 110

        self.txt(f"{len(draft)} chars  —  just type your message, Enter to send",
                 PADX, y, DIM, "sm")


# ═══════════════════════════════════════════════════════════════════════════
def load_fonts():
    pygame.font.init()
    # Prefer monospace system fonts available on macOS
    preferred = ["Menlo", "Monaco", "Courier New", "monospace"]
    def load(size):
        for name in preferred:
            try:
                f = pygame.font.SysFont(name, size)
                if f:
                    return f
            except Exception:
                pass
        return pygame.font.Font(None, size)
    return {"sm": load(13), "md": load(15), "lg": load(20), "mono": load(14)}


# ═══════════════════════════════════════════════════════════════════════════
def main():
    # Detect RNode serial port before starting UI
    rnode_port = find_rnode_port()
    port_label = rnode_port if rnode_port else "no RNode detected"

    # If no RNode found, inject a TCP-only Reticulum config warning but continue —
    # the app still works for TCP transport and testing without hardware.
    if not rnode_port:
        print("[MeshCommander] No RNode serial port found.")
        print("  Connect a Heltec V3 via USB, or set RNODE_PORT=/dev/cu.xxx")
        print("  Continuing with TCP-only transport.")

    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("MeshCommander")

    fonts    = load_fonts()
    renderer = Renderer(screen, fonts)

    transport_idx = 0
    node = MeshNode(TRANSPORTS[transport_idx])

    # ── UI state ──────────────────────────────────────────────────────────
    active_panel       = PANEL_STATUS
    msg_scroll         = 0
    peer_cursor        = 0
    selected_peer_hash = None
    compose_draft      = ""
    compose_cursor     = 0
    blink              = True
    last_blink         = time.time()

    HINTS = {
        PANEL_STATUS:   [("Tab/]", "next panel"), ("◄►", "transport"), ("F1", "announce"), ("Esc", "quit")],
        PANEL_MESSAGES: [("↕", "scroll"), ("PgUp/Dn", "page"), ("F1", "announce"), ("F3", "compose")],
        PANEL_PEERS:    [("↕", "move"), ("F3", "select"), ("F2", "★ friend")],
        PANEL_COMPOSE:  [("type", "message"), ("Bksp", "del"), ("Enter/F5", "SEND")],
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

            elif event.type == pygame.KEYDOWN:
                k   = event.key
                mod = event.mod

                # ── Global ─────────────────────────────────────────────
                if k == pygame.K_ESCAPE:
                    running = False

                # Tab / ] → next panel   Shift-Tab / [ → previous panel
                elif k in (pygame.K_TAB, pygame.K_RIGHTBRACKET):
                    if mod & pygame.KMOD_SHIFT:
                        active_panel = (active_panel - 1) % NUM_PANELS
                    else:
                        active_panel = (active_panel + 1) % NUM_PANELS
                    if active_panel == PANEL_MESSAGES:
                        node.clear_unread()

                elif k == pygame.K_LEFTBRACKET:
                    active_panel = (active_panel - 1) % NUM_PANELS
                    if active_panel == PANEL_MESSAGES:
                        node.clear_unread()

                # ── Per-panel ──────────────────────────────────────────
                elif active_panel == PANEL_STATUS:
                    if k == pygame.K_LEFT:
                        transport_idx = (transport_idx - 1) % len(TRANSPORTS)
                        node.transport = TRANSPORTS[transport_idx]
                    elif k == pygame.K_RIGHT:
                        transport_idx = (transport_idx + 1) % len(TRANSPORTS)
                        node.transport = TRANSPORTS[transport_idx]
                    elif k == pygame.K_F1:
                        node.announce()

                elif active_panel == PANEL_MESSAGES:
                    if k == pygame.K_UP:
                        msg_scroll = min(msg_scroll + 1, max(0, len(node.inbox) - 5))
                    elif k == pygame.K_DOWN:
                        msg_scroll = max(0, msg_scroll - 1)
                    elif k == pygame.K_PAGEUP:
                        msg_scroll = min(msg_scroll + 5, max(0, len(node.inbox) - 5))
                    elif k == pygame.K_PAGEDOWN:
                        msg_scroll = max(0, msg_scroll - 5)
                    elif k == pygame.K_F1:
                        node.announce()
                    elif k == pygame.K_F3:
                        active_panel = PANEL_COMPOSE

                elif active_panel == PANEL_PEERS:
                    peers = node.peer_list()
                    if k == pygame.K_UP:
                        peer_cursor = max(0, peer_cursor - 1)
                    elif k == pygame.K_DOWN:
                        peer_cursor = min(max(len(peers) - 1, 0), peer_cursor + 1)
                    elif k == pygame.K_F3 and peers:
                        selected_peer_hash = peers[peer_cursor][0]
                        active_panel       = PANEL_COMPOSE
                    elif k == pygame.K_F2 and peers:
                        h, info = peers[peer_cursor]
                        node.save_friend(h, info["name"])
                        with node._lock:
                            node.inbox.append((time.strftime("%H:%M"), "NET",
                                               f"★ Saved {info['name']}", None))
                            node.has_unread = True

                elif active_panel == PANEL_COMPOSE:
                    if k == pygame.K_RETURN or k == pygame.K_KP_ENTER:
                        # Enter sends
                        if compose_draft.strip() and selected_peer_hash:
                            node.send_to(selected_peer_hash, compose_draft.strip())
                            compose_draft = ""
                            active_panel  = PANEL_MESSAGES
                            msg_scroll    = 0
                            node.clear_unread()
                    elif k == pygame.K_BACKSPACE:
                        if compose_draft:
                            compose_draft = compose_draft[:-1]
                        else:
                            active_panel = PANEL_MESSAGES
                    elif k == pygame.K_F5:
                        if compose_draft.strip() and selected_peer_hash:
                            node.send_to(selected_peer_hash, compose_draft.strip())
                            compose_draft = ""
                            active_panel  = PANEL_MESSAGES
                            msg_scroll    = 0
                            node.clear_unread()
                    elif event.unicode and event.unicode.isprintable():
                        compose_draft += event.unicode

        # ── Draw ──────────────────────────────────────────────────────────
        screen.fill(BG)
        renderer.draw_topbar(node, active_panel, port_label)

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
            renderer.draw_compose(compose_draft, peer_name, blink)

        renderer.draw_botbar(HINTS[active_panel])
        pygame.display.flip()
        clock.tick(30)    # smoother on a real display

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
