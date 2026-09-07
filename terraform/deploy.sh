#!/usr/bin/env bash

read -p "Enter TFPlan filename: " tfplan

if [ -f "$tfplan" ]; then
	terraform apply "$tfplan"
else
	echo "TFPlan file does not exist, please check again"
fi

QUEUE_URL=$(terraform output -raw sqs_queue_url)
BUCKET=$(terraform output -raw s3_bucket)
TABLE=$(terraform output -raw dynamodb_table)
TABLE_ARN=$(terraform output -raw dynamodb_table_arn)
API_ROLE=$(terraform output -raw api_role_arn)
WORKER_ROLE=$(terraform output -raw worker_role_arn)
CI_ROLE=$(terraform output -raw ci_role_arn)

aws ecr get-login-password --region eu-west-2 | docker login --username AWS --password-stdin 266735805454.dkr.ecr.eu-west-2.amazonaws.com

sleep 3 

docker build -f ../docker/Dockerfile.api -t 266735805454.dkr.ecr.eu-west-2.amazonaws.com/pixelforge/api:dev
docker push 266735805454.dkr.ecr.eu-west-2.amazonaws.com/pixelforge/api:dev

sleep 3

docker build -f ../docker/Dockerfile.worker -t 266735805454.dkr.ecr.eu-west-2.amazonaws.com/pixelforge/worker:dev
docker push 266735805454.dkr.ecr.eu-west-2.amazonaws.com/pixelforge/worker:dev

kubectl apply -f ../k8s/serviceaccounts.yaml
kubectl apply -f ../k8s/
