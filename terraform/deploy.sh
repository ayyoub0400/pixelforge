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
