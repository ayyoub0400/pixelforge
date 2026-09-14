#!/bin/bash
pkill -f "kubectl port-forward"


kubectl delete namespace pixelforge --wait=true

helm uninstall kube-prometheus-stack --namespace monitoring --wait
helm uninstall keda --namespace keda --wait

cd terraform

terraform destroy --auto-approve

terraform state list 
