// Sender (/s): join by code, wait for approval, share the screen.
// External file (not inline) so CSP script-src 'self' holds (plan §5).

import { buildIceConfig, openSignaling, createPeer } from "/static/webrtc.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
if (params.get("code")) $("code").value = params.get("code");

const canShareScreen = !!navigator.mediaDevices?.getDisplayMedia;

let signaling = null;
let peer = null;
let code = "";
let turn = null; // `turn` frame arrives on approval

$("join").onsubmit = (ev) => {
  ev.preventDefault();
  code = $("code").value.trim().toUpperCase();
  $("join").hidden = true;
  $("session").hidden = false;
  signaling = openSignaling(code, onFrame);
  signaling.ws.onopen = () =>
    signaling.send({ type: "hello", role: "sender", name: $("name").value.trim() });
  signaling.ws.onclose = () => ($("status").textContent = "disconnected — reload to rejoin");
};

async function onFrame(frame) {
  if (frame.type === "turn") {
    turn = frame;
    return;
  }
  if (frame.type === "room-state") {
    if (frame.you.state === "pending") {
      $("status").textContent = "waiting for the screen to allow you…";
    } else if (frame.you.state === "approved" && !peer) {
      if (canShareScreen) {
        $("status").textContent = "approved — pick what to share";
        $("share").hidden = false;
      } else {
        // iPhone/iPad Safari: no getDisplayMedia (plan §1) — don't show a
        // button that can only throw (review round 1).
        $("status").textContent =
          "approved — but this device can't screen-share; phone modes (camera/photos/video) land in v1";
      }
    }
    return;
  }
  if (frame.type === "signal") {
    if (peer) await peer.handleSignal(frame.payload);
    return;
  }
  if (frame.type === "error") {
    $("status").textContent = `${frame.code}: ${frame.message}`;
  }
}

$("share").onclick = async () => {
  if (!turn) {
    $("status").textContent = "no ICE config yet — wait a beat and retry";
    return;
  }
  peer = createPeer({
    polite: true, // sender yields in glare (plan §3)
    config: buildIceConfig(turn),
    sendSignal: (payload) => signaling.send({ type: "signal", payload }),
    onPath: (p) => ($("status").textContent = `sharing (${p})`),
    onState: (s) => {
      if (s !== "connected") $("status").textContent = s;
    },
  });
  // System audio arrives only on Chrome (Windows; macOS 14.2+ with Chrome 141+) — plan §1.
  const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
  for (const track of stream.getTracks()) peer.pc.addTrack(track, stream);
  stream.getVideoTracks()[0].addEventListener("ended", () => {
    $("status").textContent = "sharing stopped";
    // TODO(v1): remove tracks + renegotiate instead of leaving a frozen last frame.
  });
};
