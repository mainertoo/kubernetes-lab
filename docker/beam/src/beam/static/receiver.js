// Receiver (/screen): create a room, show the code, approve senders, then show
// whatever arrives — a live stream (screen/camera), cast photos, or a video
// file (v1 phone modes; files arrive over a DataChannel, never via the server).
// External file (not inline) so CSP script-src 'self' holds (plan §5).

import { buildIceConfig, openSignaling, createPeer } from "/static/webrtc.js";

const $ = (id) => document.getElementById(id);

let signaling = null;
let peer = null;
let peerReady = null;
let activeSenderId = null;
let turn = null; // `turn` frame pushed by the server right after our join
let connState = "";
let path = "";
let expectingStream = false; // gates the connected-but-black watchdog
let lastUrl = null; // revoke old blob URLs
let wakeLock = null;

// Small always-visible readout on top of everything — the first thing to
// read when a venue misbehaves.
function hud(extra = "") {
  const el = $("hud");
  el.hidden = false;
  el.textContent = [connState, path, extra].filter(Boolean).join(" · ");
}

// One thing on screen at a time; first real content hides the room card.
function showContent(kind) {
  const tv = $("tv");
  const photo = $("photo");
  if (kind === "photo") {
    tv.hidden = true;
    if (!tv.paused) tv.pause();
    photo.hidden = false;
  } else {
    photo.hidden = true;
    tv.hidden = false;
  }
  $("room").hidden = true;
  hud();
}

function tryPlay() {
  const tv = $("tv");
  tv.play()
    .then(() => ($("tapplay").hidden = true))
    .catch(() => ($("tapplay").hidden = false));
}

// The TV must not sleep mid-session (v0 checklist item).
async function lockScreen() {
  try {
    wakeLock = await navigator.wakeLock?.request("screen");
  } catch {
    /* unsupported or denied — cosmetic */
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && wakeLock !== null) lockScreen();
});

// The click is the user gesture that unlocks autoplay-with-audio (plan §1).
$("start").onclick = async () => {
  const res = await fetch("/api/rooms", { method: "POST" });
  const { code, receiver_token } = await res.json();
  $("lobby").hidden = true;
  $("room").hidden = false;
  $("code").textContent = code;
  $("joinurl").textContent = `${location.host}/s`;
  lockScreen();

  signaling = openSignaling(code, onFrame);
  signaling.ws.onopen = () =>
    signaling.send({ type: "hello", role: "receiver", name: "screen", receiver_token });
  signaling.ws.onclose = () => {
    $("status").textContent = "room closed — reload to restart";
    connState = "room closed — reload";
    hud();
  };

  async function onFrame(frame) {
    if (frame.type === "turn") {
      turn = frame;
      return;
    }
    if (frame.type === "room-state") return renderApprovals(frame);
    if (frame.type === "signal") {
      await ensurePeer(frame.from);
      if (frame.from === activeSenderId) await peer.handleSignal(frame.payload);
      return;
    }
    if (frame.type === "error") {
      $("status").textContent = `${frame.code}: ${frame.message}`;
    }
  }

  // Memoized: two quick signal frames must not double-create the peer.
  function ensurePeer(senderId) {
    peerReady ??= (async () => {
      activeSenderId = senderId;
      if (!turn) {
        $("status").textContent = "no ICE config — reload";
        throw new Error("turn frame missing");
      }
      const tv = $("tv");
      tv.addEventListener("loadedmetadata", () => {
        if (tv.videoWidth > 0) {
          showContent("video");
          tryPlay();
        }
      });
      peer = createPeer({
        polite: false, // the sender is the polite peer (plan §3)
        config: buildIceConfig(turn),
        sendSignal: (payload) => signaling.send({ type: "signal", to: activeSenderId, payload }),
        onTrack: ({ streams }) => {
          expectingStream = true;
          if (tv.src) {
            tv.removeAttribute("src"); // srcObject wins only if src is clear
            tv.load();
          }
          if (tv.srcObject !== streams[0]) tv.srcObject = streams[0];
        },
        onDataChannel: attachFileChannel,
        onPath: (p) => {
          path = p;
          hud();
        },
        onState: (s) => {
          connState = s;
          $("status").textContent = s;
          hud();
          if (s === "connected") {
            setTimeout(() => {
              // Connected but black = sender capturing black frames (macOS
              // Screen Recording permission, occluded window). Only relevant
              // when a live stream is expected — photo mode is exempt.
              if (expectingStream && $("tv").srcObject && $("tv").videoWidth === 0) {
                $("status").textContent =
                  "connected, but no video frames arriving — the sender is likely capturing black (macOS: grant the browser Screen Recording permission, or pick a non-minimized window/screen)";
                hud("no frames — check sender capture");
              }
            }, 8000);
          }
        },
      });
    })();
    return peerReady;
  }

  function renderApprovals(frame) {
    const list = $("approvals");
    list.replaceChildren(); // textContent only, never innerHTML — XSS (plan §5)
    const pending = frame.peers.filter((p) => p.role === "sender" && p.state === "pending");
    for (const p of pending) {
      const li = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = `allow "${p.name || "unnamed"}"? `;
      const yes = document.createElement("button");
      yes.textContent = "Allow";
      yes.onclick = () => signaling.send({ type: "approve", peer_id: p.id, allow: true });
      const no = document.createElement("button");
      no.textContent = "Deny";
      no.onclick = () => signaling.send({ type: "approve", peer_id: p.id, allow: false });
      li.append(label, yes, no);
      list.append(li);
    }
  }
};

// --- file receive (photos / video files) over the DataChannel -----------------

function attachFileChannel(dc) {
  dc.binaryType = "arraybuffer";
  let cur = null; // { meta, parts, received }
  dc.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const m = JSON.parse(ev.data);
      if (m.t === "file-start") {
        cur = { meta: m, parts: [], received: 0 };
      } else if (m.t === "file-end" && cur) {
        showFile(cur);
        cur = null;
      }
      return;
    }
    if (!cur) return;
    cur.parts.push(ev.data);
    cur.received += ev.data.byteLength;
    if (cur.meta.size > 0) {
      hud(`receiving ${cur.meta.kind} ${Math.round((100 * cur.received) / cur.meta.size)}%`);
    }
  };
}

function showFile({ meta, parts }) {
  const blob = new Blob(parts, { type: meta.mime || "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  if (lastUrl) URL.revokeObjectURL(lastUrl);
  lastUrl = url;
  if (meta.kind === "photo") {
    expectingStream = false;
    const photo = $("photo");
    photo.onload = () => showContent("photo");
    photo.src = url;
  } else {
    expectingStream = false; // blob playback, not a live stream
    const tv = $("tv");
    tv.srcObject = null;
    tv.src = url;
    showContent("video");
    tryPlay();
  }
}

$("tapplay").onclick = () => tryPlay();
