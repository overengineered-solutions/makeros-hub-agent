# ps-checker: the shop's file checker (pstation Send to PS step 4, operator 2026-10-22: "Let's go ahead and do the work to get it on the
# shop box instead of raising the linode cap"). check.mjs is the site's own check code, bundled into one file by pstation's
# scripts/build-checker.mjs and vendored here at hub release time (its first line names the pstation commit and the format). Node 22 on
# Alpine, pinned by version; no other package. Built on the shop box by oes-pool-deploy scripts/install-checker.py; run only through
# /opt/ps-checker/run-check: no network, a read-only root, capped memory, CPU, processes and time, a job directory at /job.
FROM node:22.20.0-alpine3.22
COPY check.mjs /opt/ps-check/check.mjs
RUN node /opt/ps-check/check.mjs selftest
RUN adduser -D -u 10002 checker
USER checker
WORKDIR /job
# the check's heap: well above what a file inside the site's budget needs (under 100 MB at 300,000 triangles, measured 2026-10-22),
# inside run-check's 2 GB
ENTRYPOINT ["node", "--max-old-space-size=1536", "/opt/ps-check/check.mjs"]
