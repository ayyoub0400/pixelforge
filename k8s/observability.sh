#!/bin/bash

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update 


helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
	--namespace monitoring \
	--create-namespace \
	--version 88.5.4 \
	--values monitoring-values.yaml \
	--wait \
	--timeout 10m

sleep 5

kubectl get pods -n monitoring

sleep 3

kubectl get crd servicemonitors.monitoring.coreos.com \
  podmonitors.monitoring.coreos.com \
  prometheusrules.monitoring.coreos.com

sleep 3

#updating the values of our keda chart
helm upgrade keda kedacore/keda \
	--namespace keda \
	--version 2.20.2 \
	--reuse-values \
	--set prometheus.operator.serviceMonitor.enabled=true \
	--set prometheus.operator.serviceMonitor.additionalLabels.release=monitoring \
	--wait \
	--timeout 5m

kubectl apply -f ./observability/monitors.yaml

sleep 5

kubectl get servicemonitor,podmonitor -A

sleep 5

kubectl apply -f ./observability/alerts.yaml
kubectl apply -f ./observability/dashboard.yaml

sleep 2

kubectl get secret monitoring-grafana -n monitoring \
  -o jsonpath='{.data.admin-password}' | base64 --decode
echo

sleep 2

kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80 &

echo 1

kubectl port-forward svc/monitoring-kube-prometheus-prometheus -n monitoring 9090:9090 &

