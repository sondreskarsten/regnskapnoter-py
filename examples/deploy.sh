#!/usr/bin/env bash
# Deploy the LLM analyst as a Cloud Run Job that processes the review queue
# in batches. Schedule via Cloud Scheduler to run e.g. every hour.

set -euo pipefail

PROJECT="${GCP_PROJECT:-sondreskarsten-d7d14}"
JOB_NAME="${JOB_NAME:-rn-analyst}"
REGION="${REGION:-europe-north1}"
SCHEDULE_REGION="${SCHEDULE_REGION:-europe-west1}"
IMAGE="europe-north1-docker.pkg.dev/${PROJECT}/brreg-pipelines/${JOB_NAME}:latest"

# 1) Build via Cloud Build
gcloud builds submit examples/ \
    --tag "${IMAGE}" \
    --project "${PROJECT}"

# 2) Create or update the Cloud Run Job
gcloud run jobs deploy "${JOB_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --project "${PROJECT}" \
    --service-account "s1sfreracct@${PROJECT}.iam.gserviceaccount.com" \
    --set-env-vars "GCP_PROJECT=${PROJECT},GCP_LOCATION=europe-west1,MIN_CONFIDENCE=0.6" \
    --set-secrets "HYPOTHESIS_TOKEN=hypothesis-token:latest,HYPOTHESIS_GROUP=hypothesis-group:latest" \
    --max-retries 1 \
    --task-timeout 3600s \
    --memory 2Gi \
    --cpu 1

# 3) Schedule hourly
gcloud scheduler jobs create http "${JOB_NAME}-hourly" \
    --location "${SCHEDULE_REGION}" \
    --project "${PROJECT}" \
    --schedule "0 * * * *" \
    --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB_NAME}:run" \
    --http-method POST \
    --oauth-service-account-email "s1sfreracct@${PROJECT}.iam.gserviceaccount.com" \
    || echo "(scheduler already exists)"

echo "Deployed ${JOB_NAME} to Cloud Run Jobs in ${REGION}."
echo "Trigger manually: gcloud run jobs execute ${JOB_NAME} --region ${REGION}"
