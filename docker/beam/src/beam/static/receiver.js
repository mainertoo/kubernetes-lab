// Receiver (/screen): create a room, show the code, approve senders, play the stream.
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

// Small always-visible readout on top of everything — the first thing to
// read when a venue misbehaves (v0 acceptance lesson: the old UI hid all
// status the moment a track was negotiated).
function hud(extra = "") {
  const el = $("hud");
  el.hidden = false;
  el.textContent = [connState, path, extra].filter(Boolean).join(" · ");
}

// ontrack fires at SDP time, long before media flows — switch views only
// once real frames have dimensions.
function showVideoIfReady() {
  const tv = $("tv");
  if (tv.videoWidth > 0) {
    tv.hidden = false;
    $("room").hidden = true;
    hud();
  }
}

function tryPlay() {
  const tv = $("tv");
  tv.play()
    .then(() => ($("tapplay").hidden = true))
    .catch(() => ($("tapplay").hidden = false));
}

// The click is the user gesture that unlocks autoplay-with-audio (plan §1).
$("start").onclick = async () => {
  const res = await fetch("/api/rooms", { method: "POST" });
  const { code, receiver_token } = await res.json();
  $("lobby").hidden = true;
  $("room").hidden = false;
  $("code").textContent = code;
  $("joinurl").textContent = `${location.host}/s`;
  // TODO(v0): screen wake lock (navigator.wakeLock) + re-acquire on visibilitychange.
  // TODO(v1): go fullscreen when the first frames arrive.

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
        showVideoIfReady();
        tryPlay();
      });
      peer = createPeer({
        polite: false, // the sender is the polite peer (plan §3)
        config: buildIceConfig(turn),
        sendSignal: (payload) => signaling.send({ type: "signal", to: activeSenderId, payload }),
        onTrack: ({ streams }) => {
          if (tv.srcObject !== streams[0]) tv.srcObject = streams[0];
        },
        onPath: (p) => {
          path = p;
          hud();
        },
        onDiagnostic: (m) => {
          $("status").textContent = m;
          hud("relay unreachable");
        },
        onState: (s) => {
          connState = s;
          $("status").textContent = s;
          hud();
          if (s === "connected") {
            // Connected but black = sender is capturing black frames
            // (macOS: browser lacks Screen Recording permission; or an
            // occluded/minimized window was picked).
            setTimeout(() => {
              if ($("tv").videoWidth === 0) {
                const hint =
                  "connected, but no video frames arriving — the sender is likely capturing black (macOS: grant the browser Screen Recording permission, or pick a non-minimized window/screen)";
                $("status").textContent = hint;
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

$("tapplay").onclick = () => tryPlay();
