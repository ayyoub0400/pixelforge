#!/usr/bin/bash 

#add official helm repo
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm upgrade --install kedaa kedacore/keda --namespace keda --create-namespace --set prometheus.operator.enabled=true --wait --timeout 5m

sleep 30

kubectl get pods -n keda

sleep 5 

kubectl get apiservice v1beta1.external.metrics.k8s.io
