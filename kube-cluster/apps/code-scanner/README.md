# code-scanner

A web app (`ghcr.io/lhns/code-scanner`) ported from a Docker Swarm stack. Exposed
at **`scanner.kube.example.com`** via traefik (TLS from the wildcard cert). Listens on
`:8080` (http). The pod joins the traefik vnet as an ingress receiver
(`kube-vnet/net.traefik.traefik: ingress`); egress (incl. internet) comes from the
kube-vnet baseline, so no extra vnet config is needed.

- `resources.yaml` — the `code-scanner` namespace, Deployment, Service, Ingress.

> **Image is private on GHCR.** The pod can't pull until the package is made
> public, or an `imagePullSecret` is added to the namespace. Deploying before then
> just leaves the pod in `ImagePullBackOff` until access is granted.
