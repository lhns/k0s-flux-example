# code-scanner

A web app (`ghcr.io/lhns/code-scanner`) ported from a Docker Swarm stack. Exposed
at **`scanner.example.com`** via traefik (TLS from the wildcard cert). Listens on
`:8080` (http). The pod joins the traefik vnet as an ingress receiver
(`kube-vnet/net.traefik.traefik: ingress`); egress (incl. internet) comes from the
kube-vnet baseline, so no extra vnet config is needed.

- `resources.yaml` — Deployment, Service, Ingress.
