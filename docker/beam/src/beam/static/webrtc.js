// Shared WebRTC + signaling glue for /screen and /s.
// Perfect negotiation per the MDN pattern; the SENDER is the polite peer (plan §3).

export async function fetchIceConfig(code) {
  const res = await fetch(`/api/turn-credentials?code=${encodeURIComponent(code)}`);
  if (!res.ok) throw new Error("failed to fetch ICE config");
  const t = await res.json();
  const iceServers = [];
  const stun = t.uris.filter((u) => u.startsWith("stun:"));
  const turn = t.uris.filter((u) => u.startsWith("turn:") || u.startsWith("turns:"));
  if (stun.length) iceServers.push({ urls: stun });
  if (turn.length && t.username) {
    iceServers.push({ urls: turn, username: t.username, credential: t.credential });
  }
  // ?relay=1 forces TURN — the hostile-venue acceptance test (plan §6).
  const forceRelay = new URLSearchParams(location.search).has("relay");
  return { iceServers, iceTransportPolicy: forceRelay ? "relay" : "all" };
}

export function openSignaling(code, onFrame) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(code)}`);
  ws.onmessage = (ev) => {
    const frame = JSON.parse(ev.data);
    if (frame.type === "ping") {
      ws.send(JSON.stringify({ type: "pong" }));
      return;
    }
    onFrame(frame);
  };
  return { ws, send: (obj) => ws.send(JSON.stringify(obj)) };
}

export function createPeer({ polite, config, sendSignal, onTrack, onPath, onState }) {
  const pc = new RTCPeerConnection(config);
  let makingOffer = false;
  let ignoreOffer = false;

  pc.onnegotiationneeded = async () => {
    try {
      makingOffer = true;
      await pc.setLocalDescription();
      sendSignal({ description: pc.localDescription });
    } catch (err) {
      console.error("negotiation failed", err);
    } finally {
      makingOffer = false;
    }
  };

  pc.onicecandidate = ({ candidate }) => sendSignal({ candidate });
  if (onTrack) pc.ontrack = onTrack;

  pc.onconnectionstatechange = () => {
    if (onState) onState(pc.connectionState);
    // TODO(v1): ICE restart on "failed" + network-change handling (plan §7).
    if (pc.connectionState === "closed" || pc.connectionState === "failed") {
      clearInterval(pathTimer);
    }
  };

  async function handleSignal({ description, candidate }) {
    try {
      if (description) {
        const collision =
          description.type === "offer" && (makingOffer || pc.signalingState !== "stable");
        ignoreOffer = !polite && collision;
        if (ignoreOffer) return;
        await pc.setRemoteDescription(description);
        if (description.type === "offer") {
          await pc.setLocalDescription();
          sendSignal({ description: pc.localDescription });
        }
      } else if (candidate) {
        try {
          await pc.addIceCandidate(candidate);
        } catch (err) {
          if (!ignoreOffer) throw err;
        }
      }
    } catch (err) {
      console.error("signal handling failed", err);
    }
  }

  // Path indicator: "direct" vs "relayed" — the first thing to check at a
  // misbehaving venue (plan §3).
  const pathTimer = setInterval(async () => {
    if (pc.connectionState !== "connected" || !onPath) return;
    const stats = await pc.getStats();
    let pair = null;
    stats.forEach((s) => {
      if (s.type === "transport" && s.selectedCandidatePairId) {
        pair = stats.get(s.selectedCandidatePairId);
      }
    });
    if (!pair) {
      stats.forEach((s) => {
        if (s.type === "candidate-pair" && s.nominated && s.state === "succeeded") pair = s;
      });
    }
    if (!pair) return;
    const local = stats.get(pair.localCandidateId);
    if (local) onPath(local.candidateType === "relay" ? "relayed" : "direct");
  }, 2000);

  return { pc, handleSignal };
}
