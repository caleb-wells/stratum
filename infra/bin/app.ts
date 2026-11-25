#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { StratumStack } from "../lib/stratum-stack.js";

const app = new cdk.App();

// Deployment settings are passed on the command line so nothing personal lives in the repo:
//   cdk deploy -c domain=stratum.example.com -c adminEmail=me@example.com -c guestEmail=guest@example.com
const required = (key: string): string => {
  const value = app.node.tryGetContext(key);
  if (!value) throw new Error(`Missing context value: pass -c ${key}=...`);
  return String(value);
};

new StratumStack(app, "Stratum", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION ?? "us-west-2" },
  description: "Stratum demo (single instance)",
  domain: required("domain"),
  adminEmail: required("adminEmail"),
  adminName: String(app.node.tryGetContext("adminName") ?? "Admin"),
  guestEmail: required("guestEmail"),
  guestName: String(app.node.tryGetContext("guestName") ?? "Guest"),
  enableFable: String(app.node.tryGetContext("enableFable") ?? "false") === "true",
  monthlyBudgetUsd: Number(app.node.tryGetContext("monthlyBudgetUsd") ?? 50),
  dailyTokenLimit: Number(app.node.tryGetContext("dailyTokenLimit") ?? 3_000_000),
  instanceType: String(app.node.tryGetContext("instanceType") ?? "t4g.medium"),
});
