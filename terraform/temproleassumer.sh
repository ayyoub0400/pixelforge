#!/usr/bin/env bash

#for local testing use only

for svc in api worker; do
  CREDS=$(aws sts assume-role \
    --role-arn "$(terraform output -raw ${svc}_role_arn)" \
    --role-session-name pixelforge-kind-${svc} \
    --query 'Credentials' --output json)

  # --dry-run=client + apply makes this idempotent: it CREATES the secret the
  # first time and REPLACES it on every subsequent run. Plain `create` would
  # fail with AlreadyExists on the second attempt.
  kubectl create secret generic ${svc}-aws-creds \
    --namespace pixelforge \
    --from-literal=AWS_ACCESS_KEY_ID="$(echo "$CREDS" | jq -r .AccessKeyId)" \
    --from-literal=AWS_SECRET_ACCESS_KEY="$(echo "$CREDS" | jq -r .SecretAccessKey)" \
    --from-literal=AWS_SESSION_TOKEN="$(echo "$CREDS" | jq -r .SessionToken)" \
    --dry-run=client -o yaml | kubectl apply -f -
done
