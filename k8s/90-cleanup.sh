#!/usr/bin/env bash
# Borra TODO lo de la réplica por etiqueta part-of=kiwi-perf (incluye PVC/datos). Solo al terminar el proyecto.
set -euo pipefail

NS=kiwi
SEL=app.kubernetes.io/part-of=kiwi-perf
KINDS=sts,svc,pvc,secret,configmap,pod,job

echo "Se eliminarán estos recursos de $NS (etiqueta $SEL):"
kubectl get "$KINDS" -n "$NS" -l "$SEL"
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Confirmar (escriba 'borrar'): " a
  [[ "$a" == borrar ]] || { echo "Cancelado"; exit 1; }
fi
kubectl delete "$KINDS" -n "$NS" -l "$SEL"
