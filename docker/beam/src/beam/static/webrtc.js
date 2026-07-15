// Shared WebRTC + signaling glue for /screen and /s.
// Perfect negotiation per the MDN pattern; the SENDER is the polite peer (plan §3).

// ICE config comes from a `turn` frame pushed over the signaling WS — the
// receiver gets one on join, a sender on approval. No REST fetch (plan §4).
export function buildIceConfig(turn) {
  const iceServers = [];
  const stun = turn.uris.filter((u) => u.startsWith("stun:"));
  const relays = turn.uris.filter((u) => u.startsWith("turn:") || u.startsWith("turns:"));
  if (stun.length) iceServers.push({ urls: stun });
  if (relays.length && turn.username) {
    iceServers.push({ urls: relays, username: turn.username, credential: turn.credential });
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

export function createPeer({
  polite, config, sendSignal, onTrack, onPath, onState, onDiagnostic, onDataChannel,
}) {
  const pc = new RTCPeerConnection(config);
  let makingOffer = false;
  let ignoreOffer = false;
  let sawRelayCandidate = false;

  // Venue-blocked detection: if TURN servers were configured but gathering
  // finished without a single relay candidate, this network blocks the relay
  // (2026-07-13 field case: venue allowed only tcp/443 out) — say so instead
  // of failing silently.
  pc.onicegatheringstatechange = () => {
    if (pc.iceGatheringState !== "complete" || sawRelayCandidate) return;
    const hasRelayServers = (config.iceServers || []).some((s) =>
      [].concat(s.urls).some((u) => u.startsWith("turn")));
    if (hasRelayServers && onDiagnostic) {
      onDiagnostic(
        "relay unreachable from this network — cross-network connections will fail (the venue may block WebRTC; try a phone hotspot)"
      );
    }
  };

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

  pc.onicecandidate = ({ candidate }) => {
    if (candidate && candidate.candidate.includes(" typ relay")) sawRelayCandidate = true;
    sendSignal({ candidate });
  };
  // 701s here with the TURN uris = credential/quota trouble at the relay.
  pc.onicecandidateerror = (e) =>
    console.warn("ICE candidate error", e.errorCode, e.errorText || "", e.url || "");
  if (onTrack) pc.ontrack = onTrack;
  if (onDataChannel) pc.ondatachannel = (ev) => onDataChannel(ev.channel);

  pc.onconnectionstatechange = () => {
    if (onState) onState(pc.connectionState);
    // TODO(v1): ICE restart on "failed" + network-change handling (plan §7):
    // pc.restartIce() and renegotiate through the still-open signaling WS.
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
  // misbehaving venue (plan §3). Either side on a relay candidate = relayed.
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
    const remote = stats.get(pair.remoteCandidateId);
    const relayed =
      (local && local.candidateType === "relay") ||
      (remote && remote.candidateType === "relay");
    onPath(relayed ? "relayed" : "direct");
  }, 2000);

  return { pc, handleSignal };
}
