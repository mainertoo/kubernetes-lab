# Vendored chart

This is a pinned copy of Firecrawl's in-tree Helm chart
`examples/kubernetes/firecrawl-helm`, taken from
`github.com/firecrawl/firecrawl` at commit
`44a6a1665e6ecb565c16a05b25719b377c45c0c5` (chart version 0.2.0).

It is vendored (rather than referenced via an external GitRepository) because
Firecrawl publishes no Helm/OCI chart artifact, and flux-local CI can only
render charts sourced from the `flux-system` GitRepository. The HelmRelease
(`../firecrawl-release.yaml`) sources this path via `flux-system` and overrides
all images to official `ghcr.io/firecrawl/*` builds.

To update: re-copy the chart subtree from a newer pinned commit and re-review.
