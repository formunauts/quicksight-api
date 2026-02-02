'''
This script updates an AWS security group to allow inbound TCP traffic on port 5432
from a specified production CIDR range.
'''
import boto3
import sys
import os
from dotenv import load_dotenv

load_dotenv()

# configuration
REGION = os.getenv('QS_AWS_REGION')
SECURITY_GROUP_ID = os.getenv('QS_SECURITY_GROUP_ID')
PROD_CIDR = os.getenv('QS_PROD_CIDR')

def add_firewall_rule():
    print('Updating security group ingress rule')
    print(f'Target security group: {SECURITY_GROUP_ID}')
    print(f'Allowing TCP port 5432 from {PROD_CIDR}')

    ec2 = boto3.client('ec2', region_name=REGION)

    try:
        ec2.authorize_security_group_ingress(
            GroupId=SECURITY_GROUP_ID,
            IpPermissions=[
                {
                    'IpProtocol': 'tcp',
                    'FromPort': 5432,
                    'ToPort': 5432,
                    'IpRanges': [
                        {
                            'CidrIp': PROD_CIDR,
                            'Description': 'Allow QuickSight prod access'
                        }
                    ]
                }
            ]
        )
        print('Ingress rule added successfully')

    except ec2.exceptions.ClientError as e:
        error_code = e.response['Error']['Code']

        if error_code == 'InvalidPermission.Duplicate':
            print('Ingress rule already exists')
        elif error_code in ('UnauthorizedOperation', 'AccessDenied'):
            print('Access denied while modifying security group')
        else:
            print(f'Failed to add ingress rule: {e}')

if __name__ == '__main__':
    add_firewall_rule()
