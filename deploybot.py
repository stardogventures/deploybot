import logging
import os
import time
import traceback
import socket
import sys
import yaml
import boto3
import json
import urllib
import requests

from slackclient import SlackClient

config_file_name = sys.argv[1] if len(sys.argv) > 1 else 'deploybot.yml'
with open(config_file_name, 'r') as infile:
    config = yaml.load(infile)

SLACK_TOKEN = config['slack_token']
SLACK_BOT_USER = config['slack_user']
SLACK_BOT_MENTION = '<@%s>' % SLACK_BOT_USER
SLACK_BOT_NAME = config['slack_user']
SLACK_CHANNEL = config['slack_channel']

JENKINS_URL = config['jenkins_url']
JENKINS_TOKEN = config['jenkins_token']
JENKINS_DEPLOYS = config['jenkins_deploys']

SQS_AUTOSCALING_QUEUE_URL = config['sqs_autoscaling_queue_url'] if 'sqs_autoscaling_queue_url' in config else None
ROUTE53_ZONE_NAME = config['route53_zone_name'] if 'route53_zone_name' in config else None
AUTOSCALING_DELAY = config['autoscaling_delay']

PORT = config['port']

# bind to port (simple way of ensuring only one instance runs)
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(('', PORT))
except socket.error as msg:
    print 'Another instance of deploybot is already listening on port ' + str(PORT)
    sys.exit()

slack_client = SlackClient(SLACK_TOKEN)

scheduled_deploys = []

# given an ec2 instance, return its private ip address
def get_ec2_private_ip(instance_id):
    client = boto3.client('ec2')
    response = client.describe_instances(InstanceIds=[instance_id])
    return response['Reservations'][0]['Instances'][0]['PrivateIpAddress']

# given a route53 zone and a base name, return the next available name in the sequence
# for example, if api-1 and api-2 already exist, it will determine the next name needed is api-3
def get_route53_next_name(zone, basename, private_ip=None):
    client = boto3.client('route53')
    response = client.list_hosted_zones_by_name(DNSName=zone)
    id = response['HostedZones'][0]['Id']

    biggest = 0
    response = client.list_resource_record_sets(HostedZoneId=id)
    for set in response['ResourceRecordSets']:
        name = set['Name']
        if name.startswith(basename + '-'):
            base = name.split('.')[0]
            num = int(base[len(basename)+1:])
            biggest = max(biggest, num)
            if set['ResourceRecords'][0]['Value'] == private_ip:
                return {'name':name.split('.')[0], 'create': False}
    return {'name': basename + '-' + str(biggest+1), 'create': True}

# given an instance id, a route53 zone, and a base name, assign the instance if it is not already
# assigned
def assign_route53_name(zone, basename, instance_id):
    client = boto3.client('route53')

    private_ip = get_ec2_private_ip(instance_id)

    response = client.list_hosted_zones_by_name(DNSName=zone)
    zone_id = response['HostedZones'][0]['Id']

    next_name = get_route53_next_name(zone, basename, private_ip)
    if next_name['create']:
        change = {
            'Action': 'CREATE',
            'ResourceRecordSet': {
                'Name': next_name['name'] + '.' + zone + '.',
                'Type': 'A',
                'ResourceRecords': [
                    { 'Value': private_ip }
                ],
                'TTL': 300
            }
        }
        client.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={'Changes':[change]})

    return next_name

def assign_ec2_name_tag(instance_id, name):
    client = boto3.client('ec2')
    client.create_tags(Resources=[instance_id], Tags=[{'Key':'Name','Value':name}])

# run a jenkins deploy job based on the configuration
def deploy(module, target='prod', branch='master', username='deploybot'):
    job = JENKINS_DEPLOYS[module]['job']
    jenkins_url = JENKINS_URL + '/buildByToken/buildWithParameters?token=' + JENKINS_TOKEN + '&job=' + job + '&target=' + target + '&branch=' + branch + '&username=username'
    if 'params' in JENKINS_DEPLOYS[module]:
        for k in JENKINS_DEPLOYS[module]['params']:
            jenkins_url += '&' + k + '=' + JENKINS_DEPLOYS[module]['params'][k]
    requests.post(jenkins_url)

# listen for autoscaling events and automatically assign route 53 name and deploy when a launch occurs
def check_sqs_autoscaling_queue():
    client = boto3.client('sqs')
    response = client.receive_message(QueueUrl=SQS_AUTOSCALING_QUEUE_URL)
    if 'Messages' in response:
        for m in response['Messages']:
            notification = json.loads(m['Body'])
            subject = notification['Subject']
            message = json.loads(notification['Message'])

            instance_id = message['EC2InstanceId']
            event = message['Event']
            cause = message['Cause']
            group_name = message['AutoScalingGroupName']

            send_message('*' + subject + '*: ' + cause)

            if event == 'autoscaling:EC2_INSTANCE_LAUNCH':
                if ROUTE53_ZONE_NAME != None:
                    assigned = assign_route53_name(ROUTE53_ZONE_NAME, group_name, instance_id)
                    assign_ec2_name_tag(instance_id, assigned['name'])
                    send_message('Assigned name `' + assigned['name'] + '` to new instance `' + instance_id + '`. Deploying in ' + str(AUTOSCALING_DELAY) + ' seconds.')
                    target = assigned['name']
                else:
                    private_ip = get_ec2_private_ip(instance_id)
                    send_message('Deploying to private IP `' + private_ip + '` in ' + str(AUTOSCALING_DELAY) + ' seconds.')
                    target = private_ip
                scheduled_deploys.append({
                    'time': time.time() + AUTOSCALING_DELAY,
                    'group': group_name,
                    'target': target,
                })

            client.delete_message(QueueUrl=SQS_AUTOSCALING_QUEUE_URL, ReceiptHandle=m['ReceiptHandle'])

def check_scheduled_deploys():
    global scheduled_deploys
    scheduled_deploys_filtered = []
    for dep in scheduled_deploys:
        if dep['time'] <= time.time():
            deploy(dep['group'], dep['target'])
        else:
            scheduled_deploys_filtered.append(dep)
    scheduled_deploys = scheduled_deploys_filtered

def send_message(text):
    slack_client.rtm_send_message(channel=SLACK_CHANNEL, message=text)

def get_username(userid):
    result = slack_client.api_call('users.info', user=userid)
    return result['user']['name']

def process_help(cmd, event):
    modules = ''
    for module in JENKINS_DEPLOYS:
        modules += ' `' + module + '`'
    send_message('To deploy, tell me `' + SLACK_BOT_USER + ' deploy <module> <target> <branch>`. I understand the following modules:' + modules)

def process_deploy(cmd, event):
    parts = cmd.split(' ')
    module = parts[1] if len(parts) > 1 else 'deploy'
    target = parts[2] if len(parts) > 2 else 'prod'
    branch = parts[3] if len(parts) > 3 else 'master'
    username = get_username(event['user'])
    send_message('Roger, <@' + username + '>. Deploying `' + module + '` to `' + target + '`, using branch `' + branch + '`.')
    deploy(module, target, branch, username)

def process_test(cmd, event):
    username = get_username(event['user'])
    send_message('Hi there, <@' + username + '>!')
    pass

def process_event(event):
    # filter out slack events that are not for us
    text = event.get('text')
    if text is None or not text.startswith((SLACK_BOT_NAME, SLACK_BOT_MENTION)):
        return

    # make sure our bot is only called for a specified channel
    channel = event.get('channel')
    if channel is None:
        return
    if channel != slack_client.server.channels.find(SLACK_CHANNEL).id:
        send_message('<@{user}> I only run tasks asked from `{channel}` channel'.format(user=event['user'],
                                                                                        channel=SLACK_CHANNEL))
        return

    # remove bot name and extract command
    if text.startswith(SLACK_BOT_MENTION):
        cmd = text.split('%s' % SLACK_BOT_MENTION)[1]
        if cmd.startswith(':'):
            cmd = cmd[2:]
        cmd = cmd.strip()
    else:
        cmd = text.split('%s ' % SLACK_BOT_NAME)[1]

    # process command
    try:
        if cmd.startswith('help'):
            process_help(cmd, event)
        elif cmd.startswith('test'):
            process_test(cmd, event)
        elif cmd.startswith('deploy'):
            process_deploy(cmd, event)
        else:
            send_message("I don't know how to do that: `%s`" % cmd)
            process_help()
    except Exception:
        return process_help()


def process_events(events):
    for event in events:
        try:
            process_event(event)
        except Exception as e:
            logging.exception(e)
            msg = '%s: %s\n%s' % (e.__class__.__name__, e, traceback.format_exc())
            send_message(msg)


def main():
    last_queue_check = 0
    if slack_client.rtm_connect():
        send_message('_starting..._')
        while True:
            try:
                events = slack_client.rtm_read()
                if events:
                    logging.info(events)
                    process_events(events)

                if SQS_AUTOSCALING_QUEUE_URL != None and last_queue_check < time.time() - 60:
                    check_sqs_autoscaling_queue()
                    last_queue_check = time.time()

                check_scheduled_deploys()

            except Exception as e:
                logging.exception(e)

            time.sleep(0.1)
    else:
        logging.error('Connection Failed, invalid token?')


if __name__ == '__main__':
    main()