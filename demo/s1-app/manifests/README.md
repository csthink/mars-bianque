# s1-app manifests

Deployment manifests for s1-app in the `demo-s1` namespace. Every change is
committed here and then applied with `kubectl apply -f .`.

- `00-namespace.yaml`: the namespace (applied first; `kubectl apply -f .` goes
  through the files in name order)
- `deployment.yaml`: the application and its configuration
- `service.yaml`: the in-cluster service on port 80
