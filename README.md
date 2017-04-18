# deploybot
Slack deploybot for Jenkins with support for EC2 autoscaling
by Ian White and Stardog Ventures

This bot was originally based on example bot code published by Adrien Chauve of Serenytics at https://tech-blog.serenytics.com/deploy-your-saas-with-a-slack-bot-f6d1fc764658

## Overview

This Slack bot can deploy with a simple `deploybot deploy <module>` command in Slack. It runs parameterized Jenkins jobs, which are configurable.

The author normally sets this up to run some Ansible playbooks and has Jenkins report progress into the Slack channel, but you can hook it into whatever you want.

### Autoscaling

You can optionally use this bot with EC2 autoscaling to automatically configure and deploy to newly launched instances.

The bot can be configured to listen for EC2 autoscaling notifications via an SNS-to-SQS queue. It will announce all autoscaling actions in the channel. When it detects that a new instance has launched, it will run a deploy to the new instance. It assumes that the deploy type is named the same as the autoscaling group.

Optionally, the bot can be configured to add A records with the new instance's private IP into a Route 53 zone, to give your EC2 instances friendly names. The new instance will automatically be assigned the name `<groupname>-<number>`. For example, a newly launched server in the `api` autoscaling group might receive the name `api-5`.

## Dependencies

Ensure that you have the following libraries pip installed:

```
pip install boto3 slackclient requests
```

## Configuration

The bot relies on a .yml config file. By default it expects the file to be named `deploybot.yml` and living in the same folder, but you can pass the path as a command-line argument.

See the example YML file for example configuration, and fill in with your own values.

## Jenkins Setup

Install the `Build Token Root Plugin`: https://wiki.jenkins-ci.org/display/JENKINS/Build+Token+Root+Plugin

For each of the jobs that you want the bot to run, check the `Trigger builds remotely` box and fill in an Authentication Token. Then put that token as `jenkins_token` in the config file.

Set up your jobs or jobs as parameterized builds with the following parameters:
  - `target` - should describe the environment, group, or individual server hostname you wish to deploy to. Default value is `prod`
  - `branch` - should be the branch name you intend to deploy. Default value is `master`
  - `username` - should be the Slack username of the person who is deploying. Default value is `deploybot`

Each type of deploy you want to support should be represented in the YML file in the jenkins_deploys setting. For example:

```
jenkins_deploys:
  api:
    job: deploy
    params:
      playbook: api
  ui:
    job: deploy-ui
    params:
      playbook: ui
      anotherParam: here
```

The above represents two different deployable modules, `api` and `ui`. `api` will run the `deploy` jenkins job, while `ui` will run the `deploy-ui` jenkins job. Each module will pass in an additional `playbook` parameter in addition to the standard `target/branch/username` params.

You will also want to specify the URL to your Jenkins instance in `jenkins_url`.

## Slack Setup

Add a Slack bot here: https://slack.com/apps/A0F7YS25R-bots

You will need the token and bot name, enter these in the YML file as `slack_user` and `slack_token`. Specify the channel name in `slack_channel`.

## AWS Setup (only if you are using autoscaling)

Your instance or profile will need the following IAM policy (you can adjust the Resource wildcards to fit your specific needs):

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "sqs:DeleteMessage",
                "sqs:ReceiveMessage"
            ],
            "Resource": [
                "*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "ec2:CreateTags",
                "ec2:DescribeInstances"
            ],
            "Resource": [
                "*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "route53:ChangeResourceRecordSets",
                "route53:ListHostedZonesByName",
                "route53:ListResourceRecordSets"
            ],
            "Resource": [
                "*"
            ]
        }
    ]
}
```

Configure your autoscaling group to send a notification to an SNS topic. You can do this from the `Notifications` tab in the [Auto Scaling Groups](https://console.aws.amazon.com/ec2/autoscaling/home) section in the AWS console.

Create an SQS queue and have it subscribe to the SNS topic. You can do this from the [SQS section](https://console.aws.amazon.com/sqs/home) of the AWS console.

Specify the SQS queue URL in `sqs_autoscaling_queue_url` in the YML file.

If you're using the Route 53 naming feature, specify the `route53_zone_name` in the YML file.