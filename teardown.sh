#!/bin/bash
pkill -f "kubectl port-forward"


kubectl delete -f k8s/autoscaling.yaml

kubectl delete -f k8s/observability/alerts.yaml
kubectl delete -f k8s/observability/dashboard.yaml
kubectl delete -f k8s/observability/monitors.yaml


helm uninstall monitoring --namespace monitoring
helm uninstall keda --namespace keda

cd terraform

terraform destroy --auto-approve

terraform state list 
