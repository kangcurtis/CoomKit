# CoomKit away from home

You are on your phone, your GPU is at home behind a NAT'ed dynamic IP, and
you want to chat with your local model and have her render things like you
never left. This is how, from easiest to most self-hosted.

**One rule before anything else: never expose CoomKit to the internet.**
No port-forwarding 3939, no reverse proxy on a public domain, no "just for
a minute". CoomKit has no authentication — neither do ComfyUI, LM Studio,
llama-server or KoboldCpp — and every one of them will cheerfully do
whatever a stranger asks. The tunnel *is* the auth. Everything below builds
a private network between your devices and nothing else.

## Which machine runs what

Two topologies work. Pick one before setting up the tunnel:

**A. Everything on the rig, phone is just a browser (recommended).**
CoomKit, the LLM server and ComfyUI all run at home. On the phone you open
`http://<rig's tunnel address>:3939` in a browser — under 700px wide you get
the SMS app automatically. Set `"host": "0.0.0.0"` in the rig's
`data/config.json` so CoomKit answers on the tunnel interface (it binds only
127.0.0.1 until you say otherwise). Turn on **settings → backends → she
texts on her own clock** and she can text you while the phone browser is
suspended, because the schedule lives on the rig. Nothing to sync; there is
one install.

**B. CoomKit on the phone (Termux), GPU services on the rig.**
The phone runs its own CoomKit (`pkg install python`, clone into `$HOME` —
sqlite cannot live on `/sdcard` — then `./run.sh`; run `termux-wake-lock`
so Android leaves the server alone). Point its backends and ComfyUI URL at
the rig's tunnel address, and pull your desktop's characters over with
**settings → backends → sync**. Your data lives in your pocket; the heavy
lifting stays home. Caveat: a backend added by URL is treated as *remote*
(vision refused, prefill emulated) — a guard that cannot tell your tunnel
from OpenRouter. Topology A avoids that entirely.

## Route 1 — Tailscale: easiest, and genuinely secure

[Tailscale](https://tailscale.com) is WireGuard underneath, with the
annoying parts (key exchange, NAT traversal, dynamic IPs, roaming) handled
for you. It punches through NAT and even CGNAT — the case where plain
WireGuard is simply impossible without renting a server.

1. Install on the rig (`emerge tailscale` / your distro's package,
   `tailscale up`) and the phone (Play Store app), sign both into the same
   tailnet.
2. That's it. The rig gets a stable `100.x.y.z` address and a MagicDNS name;
   open `http://rig:3939` on the phone from anywhere.

Traffic is end-to-end encrypted WireGuard between your devices; Tailscale's
coordination server sees who's who, not what's said. If a third party in
the control plane bothers you anyway, [Headscale](https://github.com/juanfont/headscale)
is the self-hosted coordination server and the clients are the same.

## Route 2 — plain WireGuard: first-class, fully yours

No third party at all. What it costs you: one reachable UDP port at home
and something that tracks your dynamic IP.

**Reachability, in order of preference:**
- **IPv6**: if your ISP delegates a prefix, your rig has a global address
  and NAT was never the problem — open UDP 51820 inbound to the rig in the
  router's v6 firewall and skip port forwarding entirely.
- **IPv4 behind NAT**: forward UDP 51820 on the router to the rig.
- **Dynamic IP, either family**: free dynamic DNS (DuckDNS, or `ddclient`
  against your DNS provider) so the phone's config names
  `yourname.duckdns.org` instead of an address that changed overnight.
- **CGNAT** (no public IPv4 at all, no v6): plain WireGuard cannot receive
  the connection. Use Tailscale, or rent the cheapest VPS alive and run the
  WireGuard hub there.

**Rig** (`/etc/wireguard/wg0.conf`), keys via `wg genkey | tee k | wg pubkey`:

```ini
[Interface]
Address = 10.99.0.1/24
ListenPort = 51820
PrivateKey = <rig private key>

[Peer]   # the phone
PublicKey = <phone public key>
AllowedIPs = 10.99.0.2/32
```

`systemctl enable --now wg-quick@wg0`, open UDP 51820 as above.

**Phone** (official WireGuard app; generate the config on the rig and show
it as a QR with `qrencode -t ansiutf8 < phone.conf`):

```ini
[Interface]
Address = 10.99.0.2/24
PrivateKey = <phone private key>

[Peer]
PublicKey = <rig public key>
Endpoint = yourname.duckdns.org:51820
AllowedIPs = 10.99.0.0/24
PersistentKeepalive = 25
```

`AllowedIPs = 10.99.0.0/24` is a deliberate split tunnel: only CoomKit
traffic takes the tunnel, the phone's normal browsing does not ride your
home connection. `PersistentKeepalive` keeps the NAT mapping warm so the
rig can reach the phone, not just the reverse. Then open
`http://10.99.0.1:3939` and you are home.

WireGuard's app has per-tunnel "on-demand" settings on Android; leaving the
tunnel always-on costs close to nothing — the protocol is silent when idle.

## Whichever route: the checklist

- `"host": "0.0.0.0"` in the rig's `data/config.json`, restart CoomKit.
- LM Studio: enable "serve on local network" (or it answers only
  127.0.0.1). llama-server: `--host 0.0.0.0`. ComfyUI: `--listen`.
  These bind to every interface, so make sure your router's firewall (or
  the machine's) blocks them from the actual internet — the tunnel subnet
  is the only guest list.
- Phone browser → `http://<tunnel address>:3939`. Under 700px you get the
  SMS app; a tablet gets the full UI.
- Want her texting you while the app is closed? Settings → backends →
  **she texts on her own clock**. The rig keeps her schedule; texts are
  waiting when you open the app.
