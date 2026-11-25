#!/usr/bin/env bash
# Push new code to the running server: build + publish with CDK, then re-run the server script.
#   scripts/redeploy.sh -c domain=... -c adminEmail=... -c guestEmail=...
set -euo pipefail
cd "$(dirname "$0")/../infra"
npx cdk deploy --require-approval never "$@"
INSTANCE=$(aws cloudformation describe-stacks --stack-name Stratum --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
CMD=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
  --parameters 'commands=["set -euo pipefail","cd /opt/stratum","aws ssm get-parameter --name /stratum/config --query Parameter.Value --output text > config.env","set -a && . ./config.env && set +a","aws s3 cp \"$BUNDLE_S3_URL\" bundle.zip && unzip -o bundle.zip -d deploy","bash deploy/server-setup.sh 2>&1 | tee -a /var/log/stratum-setup.log"]' \
  --query Command.CommandId --output text)
echo "running on $INSTANCE (command $CMD) ..."
aws ssm wait command-executed --command-id "$CMD" --instance-id "$INSTANCE" || true
aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" --query "[Status,StandardOutputContent]" --output text | tail -25
