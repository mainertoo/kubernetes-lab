// Sender (/s): join by code, wait for approval, then beam things at the screen —
// share screen (desktop), live camera, photos, or a video file (phone modes, v1).
// Photos/videos travel over a WebRTC DataChannel peer-to-peer with backpressure;
// the beam server never carries a byte of content (plan §2 invariant).
// External file (not inline) so CSP script-src 'self' holds (plan §5).

import { buildIceConfig, openSignaling, createPeer } from "/static/webrtc.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
if (params.get("code")) $("code").value = params.get("code");

// iPhone/iPad Safari has no getDisplayMedia (plan §1) — screen share is
// desktop-only; every other mode below works everywhere.
const canShareScreen = !!navigator.mediaDevices?.getDisplayMedia;

const CHUNK_BYTES = 64 * 1024;    // safe SCTP message size across browsers
const HIGH_WATER = 8 * (1 << 20); // pause sending above 8 MiB buffered

let signaling = null;
let peer = null;
let dc = null;
let code = "";
let turn = null; // `turn` frame arrives on approval
let camSender = null; // RTCRtpSender, reused across camera flips
let camStream = null;
let camFacing = "environment";
let photos = [];
let photoIdx = -1;
let sendChain = Promise.resolve(); // serialize file transfers

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
    } else if (frame.you.state === "approved" && $("modes").hidden) {
      $("status").textContent = "approved — pick what to beam";
      $("modes").hidden = false;
      if (canShareScreen) $("share").hidden = false;
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

function ensurePeer() {
  if (!turn) throw new Error("no ICE config yet");
  if (!peer) {
    peer = createPeer({
      polite: true, // sender yields in glare (plan §3)
      config: buildIceConfig(turn),
      sendSignal: (payload) => signaling.send({ type: "signal", payload }),
      onPath: (p) => ($("path").textContent = `path: ${p}`),
      onState: (s) => {
        if (s !== "connected") $("status").textContent = s;
      },
      onDiagnostic: (m) => ($("status").textContent = m),
    });
  }
  return peer;
}

// --- file transfer over DataChannel -----------------------------------------

function ensureChannel() {
  const p = ensurePeer();
  if (!dc) {
    dc = p.pc.createDataChannel("beam-files", { ordered: true });
    dc.binaryType = "arraybuffer";
    dc.bufferedAmountLowThreshold = 1 << 20;
  }
  return dc;
}

function channelOpen(d) {
  if (d.readyState === "open") return Promise.resolve();
  return new Promise((resolve, reject) => {
    d.addEventListener("open", resolve, { once: true });
    d.addEventListener("error", reject, { once: true });
  });
}

function lowWater(d) {
  return new Promise((resolve) =>
    d.addEventListener("bufferedamountlow", resolve, { once: true })
  );
}

async function sendFile(file, kind, label) {
  const d = ensureChannel();
  await channelOpen(d);
  d.send(JSON.stringify({
    t: "file-start", kind, name: file.name, mime: file.type, size: file.size,
  }));
  let sent = 0;
  const reader = file.stream().getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    for (let off = 0; off < value.byteLength; off += CHUNK_BYTES) {
      d.send(value.slice(off, off + CHUNK_BYTES));
      if (d.bufferedAmount > HIGH_WATER) await lowWater(d);
    }
    sent += value.byteLength;
    $("status").textContent = `${label} — sending ${Math.round((100 * sent) / file.size)}%`;
  }
  d.send(JSON.stringify({ t: "file-end" }));
  $("status").textContent = `${label} — on screen`;
}

function queueFile(file, kind, label) {
  sendChain = sendChain
    .then(() => sendFile(file, kind, label))
    .catch((err) => {
      console.error("file send failed", err);
      $("status").textContent = "send failed — check the connection and retry";
    });
}

// --- modes -------------------------------------------------------------------

function stopCamera() {
  if (camStream) {
    for (const t of camStream.getTracks()) t.stop();
    camStream = null;
  }
  if (camSender) camSender.replaceTrack(null);
  $("flip").hidden = true;
}

async function startCamera() {
  const p = ensurePeer();
  if (camStream) for (const t of camStream.getTracks()) t.stop();
  try {
    camStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: camFacing },
      audio: false,
    });
  } catch {
    $("status").textContent = "camera permission denied";
    return;
  }
  const track = camStream.getVideoTracks()[0];
  if (camSender) {
    await camSender.replaceTrack(track); // flips don't renegotiate
  } else {
    camSender = p.pc.addTrack(track, camStream);
  }
  $("flip").hidden = false;
  $("status").textContent = "camera live";
}

$("camera").onclick = () => startCamera();

$("flip").onclick = () => {
  camFacing = camFacing === "environment" ? "user" : "environment";
  startCamera();
};

$("photopick").onchange = (ev) => {
  const files = [...ev.target.files];
  if (!files.length) return;
  stopCamera();
  photos = files;
  photoIdx = 0;
  $("photonav").hidden = photos.length < 2;
  sendCurrentPhoto();
  ev.target.value = ""; // re-selecting the same files should fire again
};

function sendCurrentPhoto() {
  $("pcount").textContent = `${photoIdx + 1} / ${photos.length}`;
  queueFile(photos[photoIdx], "photo", `photo ${photoIdx + 1}/${photos.length}`);
}

$("prev").onclick = () => {
  if (photoIdx > 0) {
    photoIdx -= 1;
    sendCurrentPhoto();
  }
};
$("next").onclick = () => {
  if (photoIdx < photos.length - 1) {
    photoIdx += 1;
    sendCurrentPhoto();
  }
};

$("videopick").onchange = (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  stopCamera();
  // File-transfer mode on purpose (plan §10): iOS Safari lacks captureStream()
  // on media elements, and native playback on the receiver beats re-encoding.
  // Large files take a moment on relayed paths — progress shows on both ends.
  queueFile(file, "video", file.name || "video");
  ev.target.value = "";
};

$("share").onclick = async () => {
  // Capture BEFORE peer wiring — a canceled picker must not leak state.
  // System audio arrives only on Chrome (Windows; macOS 14.2+ w/ Chrome 141+) — plan §1.
  let stream;
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
  } catch {
    $("status").textContent = "share canceled — pick what to beam when ready";
    return;
  }
  stopCamera();
  const p = ensurePeer();
  for (const track of stream.getTracks()) p.pc.addTrack(track, stream);
  stream.getVideoTracks()[0].addEventListener("ended", () => {
    $("status").textContent = "sharing stopped";
  });
  $("status").textContent = "sharing screen";
};
