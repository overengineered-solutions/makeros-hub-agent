# ps-paper: measures and renders a member's paper print for the shop's Epson ET-16600 (2026-10-18). Ghostscript reads the
# untrusted PDF only in here: no network, read-only root, capped, as the hub's user, through /opt/ps-paper/run-paper.
# It writes how much ink each page uses (to count colour and dense pages against the membership allowance) and the pages
# as PWG raster, the format the Epson takes over IPP. Built on the shop box by oes-pool-deploy scripts/install-paper.py.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ghostscript python3 python3-pil ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY ps_paper.py /opt/ps-paper/ps_paper.py
RUN python3 /opt/ps-paper/ps_paper.py selftest
RUN useradd -m -u 10001 paper
USER paper
WORKDIR /job
ENTRYPOINT ["python3", "/opt/ps-paper/ps_paper.py"]
