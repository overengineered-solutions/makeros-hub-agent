# ps-slicer: the shop's headless slicer (operator, 2026-10-18). Official OrcaSlicer Linux AppImage, pinned by version and
# sha256 (GitHub's own asset digest), unpacked at build time, on Ubuntu 24.04 with the libraries its binary links
# (WebKitGTK 4.1, OpenGL, libmspack). Built on the shop box by oes-pool-deploy scripts/install-slicer.py; run only through
# /opt/ps-slicer/run-slice: no network, read-only root, capped memory, CPU and processes, a job directory at /job.
FROM ubuntu:24.04
ARG ORCA_VERSION=2.4.2
ARG ORCA_SHA256=d12fb8c8eac1aecd2dfb6377acd48f994f8fa439ed5292fa532dd82880f029fd
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libwebkit2gtk-4.1-0 libopengl0 libglu1-mesa libmspack0 libgl1 libegl1 libgtk-3-0 libsm6 libice6 python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN cd /tmp \
    && curl -fsSL -o orca.AppImage "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage" \
    && echo "${ORCA_SHA256}  orca.AppImage" | sha256sum -c - \
    && chmod +x orca.AppImage && ./orca.AppImage --appimage-extract >/dev/null \
    && mv squashfs-root /opt/orca && rm orca.AppImage \
    && chmod -R a+rX /opt/orca
# every library the binary links must resolve here, or the build stops (2026-10-18: libSM and libICE came in only by accident)
RUN ! ldd /opt/orca/bin/orca-slicer | grep 'not found'
COPY ps_slice.py /opt/ps-slice/ps_slice.py
RUN python3 /opt/ps-slice/ps_slice.py selftest
RUN useradd -m -u 10001 slicer
USER slicer
WORKDIR /job
ENTRYPOINT ["python3", "/opt/ps-slice/ps_slice.py"]
