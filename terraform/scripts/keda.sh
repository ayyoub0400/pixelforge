#!/usr/bin/bash 

#add official helm repo
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm upgrade --install keda kedacore/keda \
	--namespace keda \
	--create-namespace \
	--set prometheus.operator.enabled=true \
	--wait \
	--timeout 5m \
	--set-string podIdentity.aws.irsa.enabled=true \
	--set-string podIdentity.aws.irsa.roleArn="${WORKER_ROLE}" \
	--set-string podIdentity.aws.irsa.stsRegionalEndpoints=true 

sleep 5

kubectl apply -f k8s/autoscaling.yaml
