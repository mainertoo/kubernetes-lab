// Receiver (/screen): create a room, show the code, approve senders, play the stream.
// External file (not inline) so CSP script-src 'self' holds (plan §5).

import { buildIceConfig, openSignaling, createPeer } from "/static/webrtc.js";

const $ = (id) => document.getElementById(id);

let signaling = null;
let peer = null;
let peerReady = null;
let activeSenderId = null;
let turn = null; // `turn` frame pushed by the server right after our join

// The click is the user gesture that unlocks autoplay-with-audio (plan §1).
$("start").onclick = async () => {
  const res = await fetch("/api/rooms", { method: "POST" });
  const { code, receiver_token } = await res.json();
  $("lobby").hidden = true;
  $("room").hidden = false;
  $("code").textContent = code;
  $("joinurl").textContent = `${location.host}/s`;
  // TODO(v0): screen wake lock (navigator.wakeLock) + re-acquire on visibilitychange.
  // TODO(v1): go fullscreen when the first track arrives.

  signaling = openSignaling(code, onFrame);
  signaling.ws.onopen = () =>
    signaling.send({ type: "hello", role: "receiver", name: "screen", receiver_token });
  signaling.ws.onclose = () => ($("status").textContent = "room closed — reload to restart");

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
      peer = createPeer({
        polite: false, // the sender is the polite peer (plan §3)
        config: buildIceConfig(turn),
        sendSignal: (payload) => signaling.send({ type: "signal", to: activeSenderId, payload }),
        onTrack: ({ streams }) => {
          const tv = $("tv");
          tv.srcObject = streams[0];
          tv.hidden = false;
          $("room").hidden = true;
          // Autoplay can still be refused (e.g. stale gesture) — recoverable UI
          // beats a silently black TV (review round 1).
          tv.play().catch(() => ($("tapplay").hidden = false));
        },
        onPath: (p) => ($("status").textContent = p),
        onState: (s) => {
          if (s !== "connected") $("status").textContent = s;
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

$("tapplay").onclick = () => {
  $("tv").play();
  $("tapplay").hidden = true;
};
