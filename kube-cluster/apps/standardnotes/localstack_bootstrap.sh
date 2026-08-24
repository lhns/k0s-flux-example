#!/usr/bin/env bash
# Upstream standardnotes/server docker/localstack_bootstrap.sh — creates the
# SNS topics + SQS queues + subscriptions the monolith's services publish to and
# consume from. Mounted into LocalStack's init hook (/etc/localstack/init/ready.d)
# so it runs once LocalStack is up. LocalStack state is ephemeral, so this
# re-runs on every LocalStack (re)start.

set -euo pipefail

echo "configuring sns/sqs"
echo "==================="
LOCALSTACK_HOST=localhost
AWS_REGION=us-east-1
LOCALSTACK_DUMMY_ID=000000000000

get_all_queues() {
  awslocal --endpoint-url=http://${LOCALSTACK_HOST}:4566 sqs list-queues
}

create_queue() {
  local QUEUE_NAME_TO_CREATE=$1
  awslocal --endpoint-url=http://${LOCALSTACK_HOST}:4566 sqs create-queue --queue-name ${QUEUE_NAME_TO_CREATE}
}

get_all_topics() {
  awslocal --endpoint-url=http://${LOCALSTACK_HOST}:4566 sns list-topics
}

create_topic() {
  local TOPIC_NAME_TO_CREATE=$1
  awslocal --endpoint-url=http://${LOCALSTACK_HOST}:4566 sns create-topic --name ${TOPIC_NAME_TO_CREATE}
}

link_queue_and_topic() {
  local TOPIC_ARN_TO_LINK=$1
  local QUEUE_ARN_TO_LINK=$2
  awslocal --endpoint-url=http://${LOCALSTACK_HOST}:4566 sns subscribe --topic-arn ${TOPIC_ARN_TO_LINK} --protocol sqs --notification-endpoint ${QUEUE_ARN_TO_LINK}
}

get_queue_arn_from_name() {
  local QUEUE_NAME=$1
  echo "arn:aws:sns:${AWS_REGION}:${LOCALSTACK_DUMMY_ID}:$QUEUE_NAME"
}

get_topic_arn_from_name() {
  local TOPIC_NAME=$1
  echo "arn:aws:sns:${AWS_REGION}:${LOCALSTACK_DUMMY_ID}:$TOPIC_NAME"
}

PAYMENTS_TOPIC_NAME="payments-local-topic"
echo "creating topic $PAYMENTS_TOPIC_NAME"
create_topic ${PAYMENTS_TOPIC_NAME}
PAYMENTS_TOPIC_ARN=$(get_topic_arn_from_name $PAYMENTS_TOPIC_NAME)

SYNCING_SERVER_TOPIC_NAME="syncing-server-local-topic"
echo "creating topic $SYNCING_SERVER_TOPIC_NAME"
create_topic ${SYNCING_SERVER_TOPIC_NAME}
SYNCING_SERVER_TOPIC_ARN=$(get_topic_arn_from_name $SYNCING_SERVER_TOPIC_NAME)

AUTH_TOPIC_NAME="auth-local-topic"
echo "creating topic $AUTH_TOPIC_NAME"
create_topic ${AUTH_TOPIC_NAME}
AUTH_TOPIC_ARN=$(get_topic_arn_from_name $AUTH_TOPIC_NAME)

FILES_TOPIC_NAME="files-local-topic"
echo "creating topic $FILES_TOPIC_NAME"
create_topic ${FILES_TOPIC_NAME}
FILES_TOPIC_ARN=$(get_topic_arn_from_name $FILES_TOPIC_NAME)

ANALYTICS_TOPIC_NAME="analytics-local-topic"
echo "creating topic $ANALYTICS_TOPIC_NAME"
create_topic ${ANALYTICS_TOPIC_NAME}
ANALYTICS_TOPIC_ARN=$(get_topic_arn_from_name $ANALYTICS_TOPIC_NAME)

REVISIONS_TOPIC_NAME="revisions-server-local-topic"
echo "creating topic $REVISIONS_TOPIC_NAME"
create_topic ${REVISIONS_TOPIC_NAME}
REVISIONS_TOPIC_ARN=$(get_topic_arn_from_name $REVISIONS_TOPIC_NAME)

SCHEDULER_TOPIC_NAME="scheduler-local-topic"
echo "creating topic $SCHEDULER_TOPIC_NAME"
create_topic ${SCHEDULER_TOPIC_NAME}
SCHEDULER_TOPIC_ARN=$(get_topic_arn_from_name $SCHEDULER_TOPIC_NAME)

QUEUE_NAME="analytics-local-queue"
echo "creating queue $QUEUE_NAME"
create_queue ${QUEUE_NAME}
ANALYTICS_QUEUE_ARN=$(get_queue_arn_from_name $QUEUE_NAME)
link_queue_and_topic $PAYMENTS_TOPIC_ARN $ANALYTICS_QUEUE_ARN

QUEUE_NAME="auth-local-queue"
echo "creating queue $QUEUE_NAME"
create_queue ${QUEUE_NAME}
AUTH_QUEUE_ARN=$(get_queue_arn_from_name $QUEUE_NAME)
link_queue_and_topic $PAYMENTS_TOPIC_ARN $AUTH_QUEUE_ARN
link_queue_and_topic $AUTH_TOPIC_ARN $AUTH_QUEUE_ARN
link_queue_and_topic $FILES_TOPIC_ARN $AUTH_QUEUE_ARN
link_queue_and_topic $REVISIONS_TOPIC_ARN $AUTH_QUEUE_ARN

QUEUE_NAME="files-local-queue"
echo "creating queue $QUEUE_NAME"
create_queue ${QUEUE_NAME}
FILES_QUEUE_ARN=$(get_queue_arn_from_name $QUEUE_NAME)
link_queue_and_topic $AUTH_TOPIC_ARN $FILES_QUEUE_ARN
link_queue_and_topic $SYNCING_SERVER_TOPIC_ARN $FILES_QUEUE_ARN

QUEUE_NAME="syncing-server-local-queue"
echo "creating queue $QUEUE_NAME"
create_queue ${QUEUE_NAME}
SYNCING_SERVER_QUEUE_ARN=$(get_queue_arn_from_name $QUEUE_NAME)
link_queue_and_topic $SYNCING_SERVER_TOPIC_ARN $SYNCING_SERVER_QUEUE_ARN
link_queue_and_topic $FILES_TOPIC_ARN $SYNCING_SERVER_QUEUE_ARN
link_queue_and_topic $SYNCING_SERVER_TOPIC_ARN $AUTH_QUEUE_ARN
link_queue_and_topic $AUTH_TOPIC_ARN $SYNCING_SERVER_QUEUE_ARN

QUEUE_NAME="revisions-server-local-queue"
echo "creating queue $QUEUE_NAME"
create_queue ${QUEUE_NAME}
REVISIONS_QUEUE_ARN=$(get_queue_arn_from_name $QUEUE_NAME)
link_queue_and_topic $SYNCING_SERVER_TOPIC_ARN $REVISIONS_QUEUE_ARN
link_queue_and_topic $REVISIONS_TOPIC_ARN $REVISIONS_QUEUE_ARN

QUEUE_NAME="scheduler-local-queue"
echo "creating queue $QUEUE_NAME"
create_queue ${QUEUE_NAME}
SCHEDULER_QUEUE_ARN=$(get_queue_arn_from_name $QUEUE_NAME)

echo "all topics are:"
get_all_topics
echo "all queues are:"
get_all_queues
