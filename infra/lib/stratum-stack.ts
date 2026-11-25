import * as path from "node:path";
import { fileURLToPath } from "node:url";
import * as cdk from "aws-cdk-lib";
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

const repoRoot = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

export interface StratumStackProps extends cdk.StackProps {
  domain: string;
  adminEmail: string;
  adminName: string;
  guestEmail: string;
  guestName: string;
  enableFable: boolean;
  monthlyBudgetUsd: number;
  dailyTokenLimit: number;
  instanceType: string;
}

/** Everything the demo needs: one small instance running the Docker Compose stack. */
export class StratumStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: StratumStackProps) {
    super(scope, id, props);
    const prefix = "/stratum";

    // ---- container images, built for ARM and pushed to ECR by `cdk deploy`
    const ragImage = new DockerImageAsset(this, "RagApiImage", {
      directory: repoRoot,
      file: "services/rag-api/Dockerfile",
      platform: Platform.LINUX_ARM64,
      exclude: [".git", ".venv", "infra", "node_modules", "docs", "deploy/.env", ".playwright-mcp", "**/__pycache__"],
    });
    const webImage = new DockerImageAsset(this, "WebUiImage", {
      directory: path.join(repoRoot, "services", "openwebui"),
      platform: Platform.LINUX_ARM64,
    });

    // ---- compose file, proxy config and the server script (never the local .env)
    const bundle = new Asset(this, "DeployBundle", {
      path: path.join(repoRoot, "deploy"),
      exclude: [".env", ".env.*", "!.env.example"],
    });

    // ---- storage for nightly database dumps
    const backups = new s3.Bucket(this, "Backups", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,   // demo: `cdk destroy` leaves nothing behind
      autoDeleteObjects: true,
    });

    // ---- network: default VPC, public subnet, web ports only (no SSH; use Session Manager)
    const vpc = ec2.Vpc.fromLookup(this, "DefaultVpc", { isDefault: true });
    const sg = new ec2.SecurityGroup(this, "WebSg", { vpc, description: "Stratum: HTTP/HTTPS only" });
    sg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "HTTPS");
    sg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), "HTTP (certificate issuance and redirect)");

    // ---- instance role: least privilege
    const role = new iam.Role(this, "InstanceRole", {
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSSMManagedInstanceCore")],
    });
    ragImage.repository.grantPull(role);
    webImage.repository.grantPull(role);
    bundle.grantRead(role);
    backups.grantReadWrite(role);

    const claude = ["claude-opus-5-5", "claude-fable-5-2", "claude-haiku-5-5"];
    const usRegions = ["us-east-1", "us-east-2", "us-west-2"];   // where the US inference profiles route
    role.addToPolicy(new iam.PolicyStatement({
      sid: "InvokeOnlyTheModelsThisDemoUses",
      actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      resources: [
        ...claude.map((m) => `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.${m}`),
        ...claude.flatMap((m) => usRegions.map((r) => `arn:aws:bedrock:${r}::foundation-model/anthropic.${m}`)),
        `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v2:0`,
        `arn:aws:bedrock:${this.region}::foundation-model/cohere.rerank-v3-5:0`,
      ],
    }));
    // The instance creates its own random secrets on first boot and stores them encrypted.
    role.addToPolicy(new iam.PolicyStatement({
      sid: "OwnParameters",
      actions: ["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter"],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${prefix}/*`],
    }));

    // ---- non-secret settings the server script reads on every run
    const config = new ssm.StringParameter(this, "Config", {
      parameterName: `${prefix}/config`,
      description: "Stratum: non-secret deployment settings",
      stringValue: [
        `RAG_API_IMAGE=${ragImage.imageUri}`,
        `WEBUI_IMAGE=${webImage.imageUri}`,
        `BUNDLE_S3_URL=${bundle.s3ObjectUrl}`,
        `BACKUP_BUCKET=${backups.bucketName}`,
        `SITE_DOMAIN=${props.domain}`,
        `WEBUI_URL=https://${props.domain}`,
        `ADMIN_EMAIL=${props.adminEmail}`,
        `ADMIN_NAME="${props.adminName}"`,
        `DEMO_EMAIL=${props.guestEmail}`,
        `DEMO_NAME="${props.guestName}"`,
        `ENABLE_FABLE=${props.enableFable}`,
        `DAILY_TOKEN_LIMIT=${props.dailyTokenLimit}`,
        `AWS_REGION=${this.region}`,
      ].join("\n"),
    });

    // ---- the instance
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      "set -euxo pipefail",
      "mkdir -p /opt/stratum && cd /opt/stratum",
      `aws ssm get-parameter --region ${this.region} --name ${prefix}/config --query Parameter.Value --output text > config.env`,
      "set -a && . ./config.env && set +a",
      "dnf install -y unzip",
      "aws s3 cp \"$BUNDLE_S3_URL\" bundle.zip && unzip -o bundle.zip -d deploy",
      "bash deploy/server-setup.sh 2>&1 | tee -a /var/log/stratum-setup.log",
    );
    const instance = new ec2.Instance(this, "Server", {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      instanceType: new ec2.InstanceType(props.instanceType),
      // Cached in cdk.context.json so a newer AMI on a later deploy does not replace the instance.
      machineImage: ec2.MachineImage.latestAmazonLinux2023({
        cpuType: ec2.AmazonLinuxCpuType.ARM_64,
        cachedInContext: true,
      }),
      securityGroup: sg,
      role,
      userData,
      requireImdsv2: true,
      httpPutResponseHopLimit: 2,        // containers reach the instance role through one extra hop
      userDataCausesReplacement: false,
      blockDevices: [{
        deviceName: "/dev/xvda",
        volume: ec2.BlockDeviceVolume.ebs(30, { volumeType: ec2.EbsDeviceVolumeType.GP3, encrypted: true }),
      }],
    });
    instance.node.addDependency(config);

    const eip = new ec2.CfnEIP(this, "Address", { domain: "vpc", tags: [{ key: "Name", value: "stratum" }] });
    new ec2.CfnEIPAssociation(this, "AddressAssociation", {
      allocationId: eip.attrAllocationId,
      instanceId: instance.instanceId,
    });

    // ---- spend alert: covers the whole account, Bedrock tokens included
    new budgets.CfnBudget(this, "MonthlyBudget", {
      budget: {
        budgetName: "stratum-monthly",
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: { amount: props.monthlyBudgetUsd, unit: "USD" },
      },
      notificationsWithSubscribers: [50, 100].map((threshold) => ({
        notification: {
          notificationType: "ACTUAL",
          comparisonOperator: "GREATER_THAN",
          threshold,
          thresholdType: "PERCENTAGE",
        },
        subscribers: [{ subscriptionType: "EMAIL", address: props.adminEmail }],
      })),
    });

    new cdk.CfnOutput(this, "PublicIp", { value: eip.attrPublicIp, description: "Point the domain's A record here" });
    new cdk.CfnOutput(this, "Url", { value: `https://${props.domain}` });
    new cdk.CfnOutput(this, "InstanceId", { value: instance.instanceId });
    new cdk.CfnOutput(this, "ReadPasswords", {
      value: `aws ssm get-parameters --names ${prefix}/admin-password ${prefix}/guest-password --with-decryption --query "Parameters[].[Name,Value]" --output text`,
      description: "Shows the generated sign-in passwords",
    });
  }
}
