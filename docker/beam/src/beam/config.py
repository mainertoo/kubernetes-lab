from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All knobs come from BEAM_* env vars; see docs/plans/beam-webrtc-beamer.md."""

    public_origin: str = "http://localhost:8080"
    # Shared with coturn's --static-auth-secret. Empty = TURN disabled (LAN-only dev).
    turn_secret: str = ""
    # Comma-separated ICE URIs handed to clients, e.g.
    # "stun:turn.mainertoo.com:3478,turn:turn.mainertoo.com:3478?transport=udp"
    turn_uris: str = ""
    turn_cred_ttl_seconds: int = 7200
    room_ttl_seconds: int = 900
    hello_deadline_seconds: int = 10
    ping_interval_seconds: int = 25
    # v0 is deliberately single-sender: a second approved sender would be
    # silently stranded by the receiver UI (review round 1). Raise for v2.
    max_senders_per_room: int = 1
    room_code_length: int = 5
    max_rooms: int = 500
    max_frame_bytes: int = 65536

    model_config = {"env_prefix": "BEAM_"}

    @property
    def turn_uri_list(self) -> list[str]:
        return [u.strip() for u in self.turn_uris.split(",") if u.strip()]

    @property
    def allowed_ws_origins(self) -> set[str]:
        # Browser same-origin policy does NOT protect WebSockets — enforce an
        # Origin allow-list (review round 1). Non-browser clients send no
        # Origin header and are allowed; the threat model is hostile web pages.
        return {self.public_origin, "http://localhost:8080", "http://127.0.0.1:8080"}


settings = Settings()
