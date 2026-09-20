# syntax=docker/dockerfile:1
#
# QIDIStudio in a browser-streamed Selkies desktop.
#
# This is a near-verbatim clone of linuxserver/docker-orcaslicer, with the one
# change that matters: it runs QIDI's own Orca fork (QIDIStudio) instead of
# mainline OrcaSlicer. QIDIStudio ships QIDI's network agent, so the Device tab,
# LAN connect-by-IP and — the reason this image exists — "Sync filaments" from
# the QIDI Box actually work. Mainline OrcaSlicer's LinuxServer image ships no
# network plugin at all (Network plugin mode: modern (version: )), so its device
# path is a dead end; only the plugin-free Moonraker HTTP upload works there.
#
# Same Selkies base, GPU plumbing and /config layout as the orcaslicer app, so
# the Kubernetes HelmRelease reuses that app's pattern almost 1:1 (port 3001).
FROM ghcr.io/linuxserver/baseimage-selkies:debiantrixie

# QIDIStudio release tag on github.com/QIDITECH/QIDIStudio. Pinned for
# reproducible builds; bump to adopt a new QIDIStudio version.
# renovate: datasource=github-releases depName=QIDITECH/QIDIStudio
ARG QIDISTUDIO_VERSION="v2.07.02.60"

ENV TITLE=QIDIStudio \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIXELFLUX_WAYLAND=true

RUN \
  echo "**** add mozilla apt repo ****" && \
  install -d -m 0755 /etc/apt/keyrings && \
  curl -o \
    /etc/apt/keyrings/packages.mozilla.org.asc -L \
    https://packages.mozilla.org/apt/repo-signing-key.gpg && \
  echo \
    "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" > \
    /etc/apt/sources.list.d/mozilla.list && \
  printf \
    "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n" > \
    /etc/apt/preferences.d/mozilla && \
  echo "**** install packages ****" && \
  apt-get update && \
  DEBIAN_FRONTEND=noninteractive \
  apt-get install --no-install-recommends -y \
    firefox \
    gstreamer1.0-alsa \
    gstreamer1.0-gl \
    gstreamer1.0-gtk3 \
    gstreamer1.0-libav \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-pulseaudio \
    gstreamer1.0-qt5 \
    gstreamer1.0-tools \
    gstreamer1.0-x \
    libgstreamer-plugins-bad1.0 \
    libmspack0 \
    libwebkit2gtk-4.1-0 \
    libwx-perl && \
  echo "**** install qidistudio from appimage ****" && \
  DOWNLOAD_URL=$(curl -sX GET \
    "https://api.github.com/repos/QIDITECH/QIDIStudio/releases/tags/${QIDISTUDIO_VERSION}" \
    | jq -r 'first(.assets[].browser_download_url | select(test("Ubuntu24.AppImage")))') && \
  cd /tmp && \
  curl -o /tmp/qidi.app -L "${DOWNLOAD_URL}" && \
  chmod +x /tmp/qidi.app && \
  ./qidi.app --appimage-extract && \
  mv squashfs-root /opt/qidistudio && \
  chmod +x /opt/qidistudio/AppRun && \
  localedef -i en_GB -f UTF-8 en_GB.UTF-8 && \
  echo "**** best-effort app icon for the selkies landing page ****" && \
  ICON=$(find /opt/qidistudio -maxdepth 3 -iname '*.png' 2>/dev/null | head -1) && \
  [ -n "${ICON}" ] && cp "${ICON}" /usr/share/selkies/www/icon.png || true && \
  echo "**** cleanup ****" && \
  apt-get autoclean && \
  rm -rf \
    /config/.cache \
    /config/.launchpadlib \
    /var/lib/apt/lists/* \
    /var/tmp/* \
    /tmp/*

# add local files (autostart -> /opt/qidistudio/AppRun, openbox menu, dbus svc)
COPY /root /

# ports and volumes
EXPOSE 3001
VOLUME /config
